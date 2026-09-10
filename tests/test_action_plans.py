"""Action Plan 표(tbl_action_plan)와 GET /action-plans — issue #58.

여기서는 가드레일 훅을 **진짜로** 돌린다 (모델만 FunctionModel/TestModel 로 고정). 그래야 대화 →
승인 카드 → 결정 → 실행이 DB 표에 어떻게 남는지 확인할 수 있다. kubectl 은 가짜, DB 는 임시 SQLite.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from kukie import conversations, server
from kukie.clusters import crypto
from kukie.agent import agent
from kukie.guardrail import action_plan, hook
from kukie.kubectl import KubectlResult
from kukie.store import get_store, reset_store_for_tests
from kukie.tools import mutate
from kukie.tools import read as read_tools

USER = {"X-User": "u-1"}
OTHER = {"X-User": "u-2"}

GOOD_RESPONSE = {"narration": "적용했습니다.", "suggested_next_action": None}

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
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.delenv("KUKIE_DEV_USER", raising=False)
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")
    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())   # 클러스터 자격증명 암호화 (기획 04 §8)
    reset_store_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    conversations.registry.clear()
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(read_tools, "run_kubectl",
                        lambda *a, **k: _ok(stdout="nginx Running"))
    monkeypatch.setattr(hook, "run_kubectl", lambda *a, **k: _ok())          # dry-run 성공
    monkeypatch.setattr(mutate, "run_kubectl", lambda *a, **k: _ok("scaled\n"))  # 실제 실행

    async def guidance(plan):
        return "현재 replica 와 가용 자원을 확인한다."

    monkeypatch.setattr(hook, "generate_decision_guidance", guidance)
    return TestClient(server.app)


def _mutation_model(call_id="call-1", args=None, tool="scale_resource"):
    """첫 호출에 변경 툴을 부르는 모델 — 훅이 가로채 승인 카드를 만든다."""
    def model_call(messages, info):
        return ModelResponse(parts=[ToolCallPart(
            tool_name=tool, args=dict(args or SCALE_ARGS), tool_call_id=call_id,
        )])
    return FunctionModel(model_call)


def _answer_model():
    """재개용 — 툴을 더 부르지 않고 답만 낸다."""
    return TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)


def _room(client, headers=USER, **body) -> str:
    body.setdefault("shared", True)          # 변경 작업은 shared 방에서만 (기획 05 §3)
    r = client.post("/conversations", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["conversation"]["id"]



def _register_cluster(cluster_id_hint: str = "cl-1", *, user_id: str = "u-1") -> str:
    """등록된 클러스터 한 줄. 방의 cluster_id 는 이제 tbl_cluster 를 가리켜야 한다 (기획 04 §8)."""
    row = get_store().create_cluster(
        registered_by=user_id, name=cluster_id_hint, api_server="https://cluster.example",
        ca_data="QQ==", insecure=False, credential_encrypted=crypto.encrypt({"token": "t"}), context_name="kind-dev",
        default_namespace="study", fingerprint="f-" + cluster_id_hint,
    )
    return row.id


def _card(client, room, *, call_id="call-1", headers=USER, args=None, tool="scale_resource"):
    client.post(f"/conversations/{room}/chat", json={"text": "/mode 실습"}, headers=headers)
    with agent.override(model=_mutation_model(call_id, args, tool)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "approval"
    return r.json()


def _plans(room=None):
    store = get_store()
    runs = store.list_runs(room) if room else []
    return [p for run in runs for p in store.list_plans_for_run(run.id)]


# ── 표에 남는가 ────────────────────────────────────────────

def test_승인_카드가_뜨면_계획이_표에_WAITING_APPROVAL로_남는다(client):
    room = _room(client)
    card = _card(client, room)

    plans = _plans(room)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.status == "WAITING_APPROVAL"
    assert plan.tool_name == "scale_resource" and plan.tool_call_id == "call-1"
    assert plan.risk == "caution"
    assert plan.decision is None and plan.execution_result is None and plan.applied_at is None
    assert plan.plan_payload["intent"] == SCALE_ARGS["intent"]
    assert plan.id == card["approvals"][0]["plan_id"]        # 카드의 plan_id 와 같은 행이다


def test_승인하면_APPLIED와_적용시각_결정자가_남는다(client):
    room = _room(client)
    _card(client, room)
    with agent.override(model=_answer_model()):
        r = client.post(f"/conversations/{room}/approve",
                        json={"call_id": "call-1", "approved": True}, headers=USER)
    assert r.status_code == 200 and r.json()["kind"] == "answer"

    plan = _plans(room)[0]
    assert plan.status == "APPLIED"
    assert plan.decision["approved"] is True and plan.decision["user_id"] == "u-1"
    assert plan.execution_result["success"] is True and plan.execution_result["exit_code"] == 0
    assert plan.applied_at is not None
    assert plan.failure_reason is None


def test_거절하면_REJECTED로_남고_실행_기록은_없다(client):
    room = _room(client)
    _card(client, room)
    with agent.override(model=_answer_model()):
        r = client.post(f"/conversations/{room}/approve",
                        json={"call_id": "call-1", "approved": False}, headers=USER)
    assert r.status_code == 200

    plan = _plans(room)[0]
    assert plan.status == "REJECTED"
    assert plan.decision == {"approved": False, "user_id": "u-1", "at": plan.decision["at"]}
    assert plan.execution_result is None and plan.applied_at is None


def test_kubectl이_실패하면_FAILED와_EXECUTION_FAILED가_남는다(client, monkeypatch):
    room = _room(client)
    _card(client, room)
    monkeypatch.setattr(mutate, "run_kubectl",
                        lambda *a, **k: _ok("", success=False, exit_code=1, stderr="not found"))
    with agent.override(model=_answer_model()):
        client.post(f"/conversations/{room}/approve",
                    json={"call_id": "call-1", "approved": True}, headers=USER)

    plan = _plans(room)[0]
    assert plan.status == "FAILED" and plan.failure_reason == "EXECUTION_FAILED"
    assert plan.applied_at is None          # 적용되지 않았으므로 시각도 없다
    assert plan.execution_result["success"] is False


def test_dry_run이_떨어지면_FAILED와_DRY_RUN_FAILED가_남는다(client, monkeypatch):
    room = _room(client)
    monkeypatch.setattr(hook, "run_kubectl",
                        lambda *a, **k: _ok("", success=False, exit_code=1, stderr="quota exceeded"))
    client.post(f"/conversations/{room}/chat", json={"text": "/mode 실습"}, headers=USER)
    with agent.override(model=_mutation_model()):
        client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=USER)

    plan = _plans(room)[0]
    assert plan.status == "FAILED" and plan.failure_reason == "DRY_RUN_FAILED"
    assert plan.decision is None


def test_run이_없는_flat_엔드포인트의_계획은_표에_들어가지_않는다(client):
    """flat /chat 은 run 을 만들지 않는다 — 계획은 .md 로만 남는다 (issue #58 댓글 4번)."""
    client.post("/session")
    server._session.deps = server.dataclasses.replace(
        server._session.deps, skill=server.SKILLS["실습"])
    with agent.override(model=_mutation_model("call-flat")):
        r = client.post("/chat", json={"text": "늘려줘"})
    assert r.status_code == 200 and r.json()["kind"] == "approval"
    assert get_store().find_plan_by_call_id("call-flat") is None


# ── GET /action-plans ─────────────────────────────────────

def test_목록은_내_방과_shared_방의_계획을_최신순으로_준다(client):
    cluster = _register_cluster()
    room = _room(client, cluster_id=cluster)
    card = _card(client, room)
    rows = client.get("/action-plans", headers=USER).json()

    assert len(rows) == 1
    assert rows[0] == {
        "id": card["approvals"][0]["plan_id"],
        "cluster_id": cluster,
        "title": SCALE_ARGS["intent"],          # 제목은 왜 하는지(intent)
        "status": "WAITING_APPROVAL",
        "running": False,
        "risk": "caution",
        "requested_by": "u-1",
        "updated_at": rows[0]["updated_at"],
        "approvals": 1,
    }


def test_남의_private_방_계획은_목록에도_상세에도_없다(client):
    room = _room(client, headers=OTHER, shared=False)
    # private 방은 실습 모드로 못 들어가므로 카드가 안 생긴다 — shared 방과 대비만 확인한다
    assert client.get("/action-plans", headers=USER).json() == []

    shared = _room(client, headers=OTHER)
    card = _card(client, shared, headers=OTHER)
    plan_id = card["approvals"][0]["plan_id"]
    assert [r["id"] for r in client.get("/action-plans", headers=USER).json()] == [plan_id]
    assert client.get(f"/action-plans/{plan_id}", headers=USER).status_code == 200
    assert client.get(f"/action-plans/{room}", headers=USER).status_code == 404


def test_cluster_id와_status로_거른다(client):
    cluster = _register_cluster()
    room = _room(client, cluster_id=cluster)
    _card(client, room)

    assert len(client.get(f"/action-plans?cluster_id={cluster}", headers=USER).json()) == 1
    assert client.get("/action-plans?cluster_id=cl-2", headers=USER).json() == []
    assert len(client.get("/action-plans?status=WAITING_APPROVAL", headers=USER).json()) == 1
    assert client.get("/action-plans?status=APPLIED", headers=USER).json() == []


def test_모르는_status는_400이다(client):
    r = client.get("/action-plans?status=draft", headers=USER)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "BAD_STATUS"


def test_상세는_결정과_실행_결과와_본문을_함께_준다(client):
    room = _room(client)
    card = _card(client, room)
    plan_id = card["approvals"][0]["plan_id"]
    with agent.override(model=_answer_model()):
        client.post(f"/conversations/{room}/approve",
                    json={"call_id": "call-1", "approved": True}, headers=USER)

    body = client.get(f"/action-plans/{plan_id}", headers=USER).json()
    assert body["status"] == "APPLIED"
    assert body["decision"]["user_id"] == "u-1"
    assert body["execution_result"]["stdout"] == "scaled\n"
    assert body["applied_at"] is not None
    assert "# Action Plan" in body["markdown"] and SCALE_ARGS["intent"] in body["markdown"]


def test_인증_없이는_목록을_못_본다(client):
    assert client.get("/action-plans").status_code == 401


# ── 재시작 ────────────────────────────────────────────────

def test_재시작해도_결정_전_카드는_살아남아_이어서_승인할_수_있다(client):
    """#58 의 목표 — 승인 대기 정보가 표에 있으므로 서버가 다시 떠도 카드가 만료되지 않는다."""
    room = _room(client)
    _card(client, room)

    conversations.registry.clear()          # 서버 재시작과 같다 (메모리 세션이 사라진다)

    body = client.get(f"/conversations/{room}", headers=USER).json()
    assert body["session"]["pending"] == ["call-1"]
    assert body["turns"][-1]["status"] == "awaiting_approval"
    assert _plans(room)[0].status == "WAITING_APPROVAL"

    with agent.override(model=_answer_model()):
        r = client.post(f"/conversations/{room}/approve",
                        json={"call_id": "call-1", "approved": True}, headers=USER)
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    assert _plans(room)[0].status == "APPLIED"


def test_승인까지_갔다가_죽은_계획은_되살리지_않고_UNKNOWN으로_닫는다(client):
    """kubectl 이 돌았는지 모르는 계획은 카드를 다시 내밀면 안 된다 — 두 번 적용될 수 있다."""
    room = _room(client)
    _card(client, room)
    store = get_store()
    plan = _plans(room)[0]
    store.update_plan(plan.id, status="APPROVED",
                      decision={"approved": True, "user_id": "u-1", "at": "2026-09-10T00:00:00+00:00"})

    conversations.registry.clear()
    body = client.get(f"/conversations/{room}", headers=USER).json()

    assert body["session"]["pending"] == []
    assert body["turns"][-1]["status"] == "interrupted"
    assert _plans(room)[0].status == "UNKNOWN"


def test_결정_전_카드도_다른_이유로_run이_닫히면_STALE이_된다(client):
    room = _room(client)
    _card(client, room)
    get_store().interrupt_active_runs(room, "중단")

    assert _plans(room)[0].status == "STALE"


def test_실패한_실행_결과로는_APPLIED가_될_수_없다(client):
    """DB 문서 5절 — APPLIED 는 success=true, exit_code=0 인 결과를 요구한다."""
    room = _room(client)
    _card(client, room)
    store = get_store()
    plan = _plans(room)[0]
    store.update_plan(plan.id, decision={"approved": True, "user_id": "u-1", "at": "2026-09-10T00:00:00+00:00"},
                      status="APPROVED")

    with pytest.raises(ValueError, match="성공한 실행 결과"):
        store.update_plan(plan.id, status="APPLIED",
                          execution_result={"success": False, "exit_code": 1, "stdout": "", "stderr": "x"})
    assert _plans(room)[0].status == "APPROVED"


# ── 자동 리뷰 반영 ─────────────────────────────────────────

def test_한_장을_거절한_뒤_재시작하면_되살리지_않는다(client):
    """🔴 자동 리뷰: 답 없는 tool call 이 대기 카드보다 많으면 재개가 영원히 실패한다.

    카드 두 장 중 하나를 거절하면 그 계획은 표에서 닫히지만(REJECTED) tool call 은 기록에 답 없이
    남는다. 남은 한 장만 되살려 재개하면 그 call 이 답 없이 모델에 가서 그 방은 갇힌다.
    """
    room = _room(client)
    client.post(f"/conversations/{room}/chat", json={"text": "/mode 실습"}, headers=USER)

    from pydantic_ai import ModelResponse
    from pydantic_ai.messages import ToolCallPart

    def two_cards(messages, info):
        return ModelResponse(parts=[
            ToolCallPart(tool_name="scale_resource", args=dict(SCALE_ARGS), tool_call_id="call-1"),
            ToolCallPart(tool_name="scale_resource",
                         args={**SCALE_ARGS, "name": "api"}, tool_call_id="call-2"),
        ])

    with agent.override(model=FunctionModel(two_cards)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "둘 다 늘려줘"}, headers=USER)
    assert r.status_code == 200 and len(r.json()["approvals"]) == 2

    # 한 장만 거절 — 남은 카드가 있으니 run 은 계속 열려 있다
    with agent.override(model=_answer_model()):
        r = client.post(f"/conversations/{room}/approve",
                        json={"call_id": "call-1", "approved": False}, headers=USER)
    assert r.json()["kind"] == "approval"
    assert {p.tool_call_id: p.status for p in _plans(room)}["call-1"] == "REJECTED"

    conversations.registry.clear()          # 재시작
    body = client.get(f"/conversations/{room}", headers=USER).json()

    assert body["session"]["pending"] == []          # 되살리지 않는다
    assert body["turns"][-1]["status"] == "interrupted"
    assert {p.tool_call_id: p.status for p in _plans(room)}["call-2"] == "STALE"


def test_없는_계획을_고치려_하면_조용히_넘어가지_않는다(client):
    """계획은 표가 원본이라 "없으면 그만" 이 아니다 — 조용히 성공하면 .md 와 표가 갈린다."""
    from kukie.store.chat_store import PlanMissing

    with pytest.raises(PlanMissing):
        get_store().update_plan("없는-계획", status="STALE")


def test_run_을_닫으면_계획도_같은_트랜잭션에서_닫힌다(client):
    """따로 커밋하면 그 사이에 죽었을 때 계획이 열린 채 영원히 남는다."""
    room = _room(client)
    _card(client, room)
    runs = get_store().list_runs(room)

    get_store().interrupt_active_runs(room, "중단")

    assert all(r.status == "interrupted" for r in get_store().list_runs(room) if r.id == runs[-1].id)
    assert _plans(room)[0].status == "STALE"


def test_저장_실패로_run_이_failed_가_돼도_계획이_닫힌다(client, monkeypatch):
    """세 번째 failed 문 — _save_or_fail 의 대체 쓰기 경로 (자동 리뷰 지적).

    여기서 안 닫으면 run 은 종료 상태인데 계획은 열린 채 남고, active_run 이 그 run 을 더 이상
    주지 않아 다시 지나가는 경로가 없다.
    """
    room = _room(client)
    client.post(f"/conversations/{room}/chat", json={"text": "/mode 실습"}, headers=USER)

    store = get_store()
    real = store.update_run
    calls = {"n": 0}

    def flaky(run_id, **fields):
        calls["n"] += 1
        # 카드가 뜬 뒤의 첫 저장만 실패시킨다 (대체 쓰기는 통과)
        if fields.get("status") == "awaiting_approval":
            raise RuntimeError("저장 실패")
        return real(run_id, **fields)

    monkeypatch.setattr(store, "update_run", flaky)
    with agent.override(model=_mutation_model()):
        r = client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=USER)

    assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
    monkeypatch.undo()
    assert get_store().list_runs(room)[-1].status == "failed"
    assert _plans(room)[0].status == "STALE"        # 열린 채 남지 않는다


def test_계획_닫기가_실패해도_원래_오류를_돌려준다(client, monkeypatch):
    """계획 닫기에서 터지면 코드 없는 500 이 나가고 계획도 못 닫았다."""
    room = _room(client)
    client.post(f"/conversations/{room}/chat", json={"text": "/mode 실습"}, headers=USER)
    monkeypatch.setattr(get_store(), "expire_open_plans",
                        lambda run_id: (_ for _ in ()).throw(RuntimeError("DB 넘어짐")))

    def boom(messages, info):
        raise RuntimeError("모델 실패")

    with agent.override(model=FunctionModel(boom)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"}, headers=USER)

    assert r.status_code == 500 and r.json()["detail"]["code"] == "RUN_FAILED"


def test_실행_기록_저장이_실패해도_계획이_열린_채_남지_않는다(client, monkeypatch):
    """네 번째 문 — completed (자동 리뷰 지적).

    kubectl 이 돈 뒤 record_execution 이 실패하면 훅은 경고만 붙이고 결과를 정상 반환한다.
    모델이 최종 답변을 내고 run 은 completed 로 닫히는데, 그 경로에 계획 닫기가 없었다.
    """
    room = _room(client)
    _card(client, room)

    real = get_store().update_plan

    def flaky(plan_id, **fields):
        if fields.get("status") in {"APPLIED", "FAILED"}:
            raise RuntimeError("실행 기록 저장 실패")
        return real(plan_id, **fields)

    monkeypatch.setattr(get_store(), "update_plan", flaky)
    with agent.override(model=_answer_model()):
        r = client.post(f"/conversations/{room}/approve",
                        json={"call_id": "call-1", "approved": True}, headers=USER)

    assert r.status_code == 200 and r.json()["kind"] == "answer"
    monkeypatch.undo()
    assert get_store().list_runs(room)[-1].status == "completed"
    assert _plans(room)[0].status == "UNKNOWN"     # 승인은 됐는데 결과를 모른다
