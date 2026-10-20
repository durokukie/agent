"""대화 → 승인 카드 → 결정 → 실행이 Kukie API 서버의 Action Plan 에 어떻게 남는가 — issue #58, DURO-113.

여기서는 가드레일 훅을 **진짜로** 돌린다 (모델만 FunctionModel/TestModel 로 고정). 그래야 훅이 server 의
plan 동작(create·dry-run·offer·decision·start·result·unknown)을 어떤 순서로 부르는지, exactly-once 가
지켜지는지 확인할 수 있다. kubectl 은 가짜, server 는 메모리 가짜(tests/fake_server.py).
목록·상세(GET /action-plans)와 표의 제약은 server 공개 API 라 kukie-server 가 시험한다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel

from helpers import GUIDANCE, USER, answer_model, new_room
from kukie_agent import conversations, server, server_api
from kukie_agent.agent import agent
from kukie_agent.guardrail import hook
from kukie_agent.kubectl import KubectlResult
from kukie_agent.server_api import ServerError
from kukie_agent.tools import mutate

SCALE_ARGS = {
    "kind": "deployment",
    "name": "nginx",
    "replicas": 3,
    "namespace": "study",
    "intent": "nginx 레플리카를 3개로 늘린다.",
    "expected_effects": ["레플리카가 3개가 된다."],
    "side_effects": ["추가 노드 자원을 사용한다."],
}


def _ok(stdout="ok\n", success=True, exit_code=0, stderr=""):
    return KubectlResult(command="kubectl …", stdout=stdout, stderr=stderr,
                         success=success, exit_code=exit_code)


@pytest.fixture
def executed(monkeypatch):
    """변경 툴 본체의 kubectl — 몇 번 불렸는지 센다 (exactly-once 확인)."""
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(list(args))
        return _ok("scaled\n")

    monkeypatch.setattr(mutate, "run_kubectl", run)
    return calls


@pytest.fixture
def client(dev_auth, fake_server, kubectl_ok, hook_passes, executed):
    return TestClient(server.app)


def _mutation_model(call_id="call-1", args=None, tool="scale_resource"):
    """첫 호출에 변경 툴을 부르는 모델 — 훅이 가로채 승인 카드를 만든다."""
    def model_call(messages, info):
        return ModelResponse(parts=[ToolCallPart(
            tool_name=tool, args=dict(args or SCALE_ARGS), tool_call_id=call_id,
        )])
    return FunctionModel(model_call)


def _room(fake) -> str:
    return new_room(fake, shared=True)          # 변경 작업은 shared 방에서만 (기획 05 §3)


def _card(client, room, *, call_id="call-1", args=None, tool="scale_resource"):
    with agent.override(model=_mutation_model(call_id, args, tool)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "approval"
    return r.json()


def _approve(client, room, call_id="call-1", approved=True):
    with agent.override(model=answer_model()):
        return client.post(f"/conversations/{room}/approve",
                           json={"call_id": call_id, "approved": approved}, headers=USER)


def _actions(fake, plan_id):
    return [c["action"] for name, c in fake.calls if name == "plan_action" and c["id"] == plan_id]


# ── server 에 남는가 ──────────────────────────────────────

def test_승인_카드가_뜨면_계획이_server에_WAITING_APPROVAL로_남는다(client, fake_server):
    room = _room(fake_server)
    card = _card(client, room)

    plans = fake_server.plans_of(room)
    assert len(plans) == 1
    plan = plans[0]
    assert plan["status"] == "WAITING_APPROVAL"
    assert plan["toolName"] == "scale_resource" and plan["toolCallId"] == "call-1"
    assert plan["risk"] == "caution"
    assert plan["decision"] is None and plan["executionResult"] is None and plan["appliedAt"] is None
    assert plan["payload"]["intent"] == SCALE_ARGS["intent"]
    assert plan["payload"]["dry_run_result"]["status"] == "succeeded"
    assert plan["id"] == card["approvals"][0]["plan_id"]       # 카드의 plan_id 는 server 의 UUID 다
    assert plan["code"].startswith("ap-")                       # 화면 표시용 id 는 따로
    assert _actions(fake_server, plan["id"]) == ["dry-run", "offer"]
    # 대역이 훅의 인자를 못 받으면 TypeError 가 훅의 except 에 먹혀 "guidance unavailable" 이 된다.
    # 그 조용한 실패를 여기서 잡는다 (#50 리뷰).
    assert card["approvals"][0]["decision_guidance"] == GUIDANCE
    assert plan["payload"]["decision_guidance"] == GUIDANCE


def test_승인하면_APPLIED와_적용시각_결정자가_남고_kubectl은_한_번(client, fake_server, executed):
    room = _room(fake_server)
    _card(client, room)
    r = _approve(client, room)
    assert r.status_code == 200 and r.json()["kind"] == "answer"

    plan = fake_server.plans_of(room)[0]
    assert plan["status"] == "APPLIED"
    assert plan["decision"]["approved"] is True and plan["decision"]["user_id"] == "u-1"
    assert plan["executionResult"]["success"] is True and plan["executionResult"]["exit_code"] == 0
    assert plan["appliedAt"] is not None and plan["failureReason"] is None
    assert _actions(fake_server, plan["id"]) == ["dry-run", "offer", "decision", "start", "result"]
    assert len(executed) == 1


def test_거절하면_REJECTED로_남고_실행_기록은_없다(client, fake_server, executed):
    room = _room(fake_server)
    _card(client, room)
    assert _approve(client, room, approved=False).status_code == 200

    plan = fake_server.plans_of(room)[0]
    assert plan["status"] == "REJECTED"
    assert plan["decision"] == {"approved": False, "user_id": "u-1", "at": plan["decision"]["at"]}
    assert plan["executionResult"] is None and plan["appliedAt"] is None
    assert executed == []


def test_kubectl이_실패하면_FAILED와_EXECUTION_FAILED가_남는다(client, fake_server, monkeypatch):
    room = _room(fake_server)
    _card(client, room)
    monkeypatch.setattr(mutate, "run_kubectl",
                        lambda *a, **k: _ok("", success=False, exit_code=1, stderr="not found"))
    _approve(client, room)

    plan = fake_server.plans_of(room)[0]
    assert plan["status"] == "FAILED" and plan["failureReason"] == "EXECUTION_FAILED"
    assert plan["appliedAt"] is None          # 적용되지 않았으므로 시각도 없다
    assert plan["executionResult"]["success"] is False


def test_dry_run이_떨어지면_FAILED와_DRY_RUN_FAILED가_남는다(client, fake_server, monkeypatch):
    room = _room(fake_server)
    monkeypatch.setattr(hook, "run_kubectl",
                        lambda *a, **k: _ok("", success=False, exit_code=1, stderr="quota exceeded"))
    with agent.override(model=_mutation_model()):
        client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=USER)

    plan = fake_server.plans_of(room)[0]
    assert plan["status"] == "FAILED" and plan["failureReason"] == "DRY_RUN_FAILED"
    assert plan["decision"] is None
    assert _actions(fake_server, plan["id"])[0] == "dry-run"     # 실패 처리는 server 가 dry-run 에서 한다
    assert "offer" not in _actions(fake_server, plan["id"])      # 카드로 나가지 않는다


def test_run이_없는_flat_엔드포인트의_계획은_server에_들어가지_않는다(client, fake_server, fake_kubeconfig, plan_dir):
    """flat /chat 은 run 을 만들지 않는다 — 계획은 .md 로만 남는다 (issue #58 댓글 4번)."""
    client.post("/session")
    try:
        with agent.override(model=_mutation_model("call-flat")):
            r = client.post("/chat", json={"text": "늘려줘"})
    finally:
        server._session = None
    assert r.status_code == 200 and r.json()["kind"] == "approval"
    assert fake_server.plans == {}
    assert [p.name for p in plan_dir.glob("*.md")] == [f"{r.json()['approvals'][0]['plan_id']}.md"]


# ── exactly-once ──────────────────────────────────────────

def test_kubectl_전에_server에_닿지_못하면_실행하지_않고_재시도할_수_있다(client, fake_server, monkeypatch, executed):
    """decision 기록이 server 에 못 닿으면 kubectl 을 돌리지 않는다 — 기록 없이 돈 변경은 재시도에서 두 번 적용될 수 있다."""
    room = _room(fake_server)
    _card(client, room)
    original = server_api.plan_action
    down = {"on": True}

    async def flaky(plan_id, action, body=None):
        if down["on"] and action == "decision":
            raise ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await original(plan_id, action, body)

    monkeypatch.setattr(server_api, "plan_action", flaky)
    r = _approve(client, room)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "RESUME_RETRYABLE"
    assert executed == []
    assert fake_server.plans_of(room)[0]["status"] == "WAITING_APPROVAL"

    down["on"] = False
    with agent.override(model=answer_model()):
        r = client.post(f"/conversations/{room}/resume", headers=USER)
    assert r.status_code == 200 and len(executed) == 1
    assert fake_server.plans_of(room)[0]["status"] == "APPLIED"


def test_실행_뒤_응답이_실패해_재개를_다시_보내도_kubectl은_한_번이고_저장된_결과를_쓴다(client, fake_server, executed):
    """DURO-66: kubectl 이 돈 뒤 모델 재개가 실패하면 /resume 으로 같은 결정을 다시 보낸다 — 결과는 Plan 에 있다."""
    room = _room(fake_server)
    _card(client, room)

    def boom(messages, info):
        raise RuntimeError("모델 실패")

    with agent.override(model=FunctionModel(boom)):
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "RESUME_RETRYABLE"
    assert len(executed) == 1 and fake_server.plans_of(room)[0]["status"] == "APPLIED"

    with agent.override(model=answer_model()):
        r = client.post(f"/conversations/{room}/resume", headers=USER)
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    assert len(executed) == 1                                   # 두 번 돌지 않았다


def test_실행_기록이_없는_승인을_다시_재개하면_돌리지_않고_UNKNOWN으로_닫는다(client, fake_server, monkeypatch, executed):
    """kubectl 은 돌았는데 result 기록이 server 에 못 닿았고 모델 재개도 실패했다. /resume 이 같은 결정을 다시 보내도
    돌았는지 모르는 변경은 다시 돌리지 않는다 — 승인은 있고 결과는 없으니 UNKNOWN (DURO-83)."""
    room = _room(fake_server)
    _card(client, room)
    original = server_api.plan_action

    async def flaky(plan_id, action, body=None):
        if action == "result":
            raise ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await original(plan_id, action, body)

    def boom(messages, info):
        raise RuntimeError("모델 실패")

    monkeypatch.setattr(server_api, "plan_action", flaky)
    with agent.override(model=FunctionModel(boom)):
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "RESUME_RETRYABLE"
    plan = fake_server.plans_of(room)[0]
    assert len(executed) == 1 and plan["status"] == "EXECUTING"

    with agent.override(model=answer_model()):
        r = client.post(f"/conversations/{room}/resume", headers=USER)
    assert r.status_code == 200                                 # 모델은 ToolFailed 를 받고 답한다
    assert len(executed) == 1                                   # 다시 돌리지 않았다
    assert plan["status"] == "UNKNOWN"
    assert _actions(fake_server, plan["id"])[-1] == "unknown"


def test_승인한_호출과_다른_호출은_server가_거절하고_실행하지_않는다(client, fake_server, executed):
    """PLAN_CALL_MISMATCH — 비교는 server 가 한다. 훅은 ToolFailed 로 바꿔 kubectl 을 부르지 않는다."""
    room = _room(fake_server)
    _card(client, room)
    plan = fake_server.plans_of(room)[0]
    plan["payload"]["args"] = {**plan["payload"]["args"], "replicas": 5}     # 승인 카드 뒤에 plan 이 달라졌다

    r = _approve(client, room)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "APPROVAL_MISMATCH"   # 카드부터 다르다
    assert executed == []


def test_실행_기록_저장이_실패해도_경고를_붙이고_run이_닫힐_때_UNKNOWN이_된다(client, fake_server, monkeypatch, executed):
    """kubectl 이 돈 뒤 result 기록이 실패하면 훅은 경고만 붙이고 결과를 정상 반환한다.
    plan 은 EXECUTING 에 머물렀다가 run 이 completed 로 닫힐 때 server 가 UNKNOWN 으로 닫는다."""
    room = _room(fake_server)
    _card(client, room)
    original = server_api.plan_action

    async def flaky(plan_id, action, body=None):
        if action == "result":
            raise ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await original(plan_id, action, body)

    monkeypatch.setattr(server_api, "plan_action", flaky)
    r = _approve(client, room)

    assert r.status_code == 200 and r.json()["kind"] == "answer"
    assert len(executed) == 1
    assert fake_server.turns(room)[-1]["status"] == "completed"
    assert fake_server.plans_of(room)[0]["status"] == "UNKNOWN"     # 승인은 됐는데 결과를 모른다


# ── 재시작 ────────────────────────────────────────────────

def test_재시작해도_결정_전_카드는_살아남아_이어서_승인할_수_있다(client, fake_server, executed):
    """#58 의 목표 — 승인 대기 정보가 server 에 있으므로 agent 가 다시 떠도 카드가 만료되지 않는다."""
    room = _room(fake_server)
    _card(client, room)

    conversations.registry.clear()          # agent 재시작과 같다 (메모리 세션이 사라진다)
    assert fake_server.pending(room) == ["call-1"]

    r = _approve(client, room)              # 승인 요청이 방을 다시 열면서 카드를 되살린다
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    assert fake_server.plans_of(room)[0]["status"] == "APPLIED"
    assert len(executed) == 1


def test_결정_전_server_장애는_run을_건드리지_않아_재시작해도_카드가_살아남는다(client, fake_server, monkeypatch, executed):
    """자동 리뷰: 승인 요청이 카드의 계획을 찾다 server 장애(5xx)를 만났다. 결정도 kubectl 도 없었는데 5xx 라고
    recovery_required 로 덮으면 카드 payload 가 사라지고, 재시작 뒤 복원이 그 run 을 닫아 카드가 EXPIRED 가 된다.
    recovery_required 는 재개가 시작된 뒤의 실패(RESUME_RETRYABLE)만이다."""
    room = _room(fake_server)
    _card(client, room)
    original = server_api.run_plans
    blips = {"left": 1}

    async def flaky(run_id, *, tool_call_id=None):
        if blips["left"]:
            blips["left"] -= 1
            raise ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await original(run_id, tool_call_id=tool_call_id)

    monkeypatch.setattr(server_api, "run_plans", flaky)
    r = _approve(client, room)

    assert r.status_code == 503 and r.json()["detail"]["code"] == "SERVER_UNAVAILABLE"
    run = fake_server.turns(room)[-1]
    assert run["status"] == "awaiting_approval" and run["payload"]["kind"] == "approval"    # 카드가 그대로다
    assert fake_server.plans_of(room)[0]["status"] == "WAITING_APPROVAL" and executed == []

    conversations.registry.clear()          # 재시작해도 그 카드를 되살려 이어서 승인한다
    r = _approve(client, room)
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    assert fake_server.plans_of(room)[0]["status"] == "APPLIED"
    assert len(executed) == 1


def test_거절_기록이_server_장애로_실패해도_run은_카드_대기_그대로다(client, fake_server, monkeypatch, executed):
    """거절 기록 실패(REJECT_FAILED)도 실행 전이다 — 안내대로 같은 카드를 다시 결정할 수 있어야 한다."""
    room = _room(fake_server)
    _card(client, room)
    original = server_api.plan_action
    blips = {"left": 1}

    async def flaky(plan_id, action, body=None):
        if action == "decision" and blips["left"]:
            blips["left"] -= 1
            raise ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await original(plan_id, action, body)

    monkeypatch.setattr(server_api, "plan_action", flaky)
    r = _approve(client, room, approved=False)

    assert r.status_code == 503 and r.json()["detail"]["code"] == "REJECT_FAILED"
    run = fake_server.turns(room)[-1]
    assert run["status"] == "awaiting_approval" and run["payload"]["kind"] == "approval"
    assert fake_server.plans_of(room)[0]["status"] == "WAITING_APPROVAL"

    r = _approve(client, room, approved=False)
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    assert fake_server.plans_of(room)[0]["status"] == "REJECTED"
    assert executed == []


def test_승인까지_갔다가_죽은_계획은_되살리지_않고_UNKNOWN으로_닫는다(client, fake_server, executed):
    """kubectl 이 돌았는지 모르는 계획은 카드를 다시 내밀면 안 된다 — 두 번 적용될 수 있다."""
    room = _room(fake_server)
    _card(client, room)
    plan = fake_server.plans_of(room)[0]
    plan.update(status="APPROVED", decision={"approved": True, "user_id": "u-1", "at": "2026-10-14T00:00:00Z"})

    conversations.registry.clear()
    r = _approve(client, room)

    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"
    assert fake_server.turns(room)[-1]["status"] == "interrupted"
    assert plan["status"] == "UNKNOWN"
    assert executed == []


def test_한_장을_거절한_뒤_재시작하면_되살리지_않는다(client, fake_server):
    """🔴 자동 리뷰: 답 없는 tool call 이 대기 카드보다 많으면 재개가 영원히 실패한다.

    카드 두 장 중 하나를 거절하면 그 계획은 닫히지만(REJECTED) tool call 은 기록에 답 없이
    남는다. 남은 한 장만 되살려 재개하면 그 call 이 답 없이 모델에 가서 그 방은 갇힌다.
    """
    room = _room(fake_server)

    def two_cards(messages, info):
        return ModelResponse(parts=[
            ToolCallPart(tool_name="scale_resource", args=dict(SCALE_ARGS), tool_call_id="call-1"),
            ToolCallPart(tool_name="scale_resource", args={**SCALE_ARGS, "name": "api"}, tool_call_id="call-2"),
        ])

    with agent.override(model=FunctionModel(two_cards)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "둘 다 늘려줘"}, headers=USER)
    assert r.status_code == 200 and len(r.json()["approvals"]) == 2

    # 한 장만 거절 — 남은 카드가 있으니 run 은 계속 열려 있다
    r = _approve(client, room, "call-1", approved=False)
    assert r.json()["kind"] == "approval"
    assert fake_server.plan_by_call("call-1")["status"] == "REJECTED"

    conversations.registry.clear()          # 재시작
    r = _approve(client, room, "call-2")

    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"    # 되살리지 않는다
    assert fake_server.turns(room)[-1]["status"] == "interrupted"
    assert fake_server.plan_by_call("call-2")["status"] == "EXPIRED"


# ── 저장 실패 ──────────────────────────────────────────────

def test_저장_실패로_run이_failed가_돼도_계획이_닫힌다(client, fake_server, monkeypatch):
    """_save_or_fail 의 대체 쓰기 경로 — 종료 상태(failed)로 닫으면 server 가 같은 트랜잭션에서 계획을 닫는다."""
    room = _room(fake_server)
    original = server_api.update_run

    async def flaky(run_id, **fields):
        if fields.get("status") == "awaiting_approval":           # 카드가 뜬 뒤의 첫 저장만 실패 (대체 쓰기는 통과)
            raise ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await original(run_id, **fields)

    monkeypatch.setattr(server_api, "update_run", flaky)
    with agent.override(model=_mutation_model()):
        r = client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=USER)

    assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
    assert fake_server.turns(room)[-1]["status"] == "failed"
    assert fake_server.plans_of(room)[0]["status"] == "EXPIRED"        # 열린 채 남지 않는다


def test_실패_기록마저_실패해도_원래_오류를_돌려준다(client, fake_server, monkeypatch):
    """모델 실패 뒤 failed 기록이 server 에 못 닿아도 코드 없는 500 이 아니라 RUN_FAILED 가 나간다.
    running 으로 남은 run 은 다음 chat 이 닫는다."""
    room = _room(fake_server)

    async def down(run_id, **fields):
        raise ServerError(503, "SERVER_UNAVAILABLE", "down")

    def boom(messages, info):
        raise RuntimeError("모델 실패")

    original = server_api.update_run
    monkeypatch.setattr(server_api, "update_run", down)
    with agent.override(model=FunctionModel(boom)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=USER)
    assert r.status_code == 500 and r.json()["detail"]["code"] == "RUN_FAILED"
    assert fake_server.turns(room)[-1]["status"] == "running"

    monkeypatch.setattr(server_api, "update_run", original)
    with agent.override(model=answer_model("다시")):
        r = client.post(f"/conversations/{room}/chat", json={"text": "다시"}, headers=USER)
    assert r.status_code == 200
    assert [t["status"] for t in fake_server.turns(room)] == ["interrupted", "completed"]


def test_저장에_실패해_남은_run을_같은_request_id로_치우면_계획도_닫힌다(client, fake_server):
    """재전송이 결과 저장에 실패한 run 을 치우는 문 — interrupt 가 계획까지 EXPIRED 로 닫는다."""
    room = _room(fake_server)
    _card(client, room)
    run = fake_server.runs_of(room)[-1]
    run["status"] = "running"                       # 결과를 저장하지 못하고 활성으로 남은 run
    conversations.registry.get(room).session.pending = None
    r = client.post(f"/conversations/{room}/chat",
                    json={"text": "늘려줘", "request_id": run["requestId"]}, headers=USER)

    # 치운 run 을 그대로 재생하므로 "중단됐다, 새 요청으로 보내라" 가 나간다
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INTERRUPTED"
    assert fake_server.plans_of(room)[0]["status"] == "EXPIRED"
