"""로컬 서버 검증 (DURO-49) — TestModel + 가짜 kubectl + 가짜 kubeconfig.

세션 시작, 채팅(답변 경로), 모드 전환, 승인 대기 잠금, 승인 재개 뼈대.
변경 툴이 아직 미등록이라 승인 티켓은 에이전트로 못 만든다 — 티켓 분기는 가짜 결과로 검증한다.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from kukie import server
from kukie.agent import agent
from kukie.guardrail import action_plan
from kukie.guardrail.action_plan import ActionPlan
from kukie.kubectl import KubectlResult
from kukie.tools import read as read_tools

FAKE_COMMAND = "kubectl --context kind-dev get pods -n study -o wide"


@pytest.fixture
def client(monkeypatch, tmp_path):
    server._session = None
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    monkeypatch.setattr(read_tools, "run_kubectl",
                        lambda args, *, context, dry_run=False, stdin=None, timeout=30:
                        KubectlResult(command=FAKE_COMMAND, stdout="nginx Running", stderr="", success=True))
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)   # 홈의 실제 계획서를 읽지 않게
    return TestClient(server.app)


def _model(narration="답", call_tools=("list_resources",)):
    return TestModel(call_tools=list(call_tools), custom_output_args={"narration": narration})


# ── 세션 ─────────────────────────────────────────────────────

def test_세션_시작은_kubeconfig의_대상을_돌려준다(client):
    r = client.post("/session")
    assert r.status_code == 200
    assert r.json() == {"context": "kind-dev", "namespace": "study", "skill": "학습", "pending": []}
    assert client.get("/session").json()["skill"] == "학습"


def test_세션_없이_채팅하면_409(client):
    assert client.post("/chat", json={"text": "파드"}).status_code == 409
    assert client.get("/session").status_code == 409


def test_kubeconfig를_못_읽으면_503(client, monkeypatch):
    def boom():
        raise server.KubeconfigError("current-context 없음")
    monkeypatch.setattr(server, "read_kubeconfig", boom)
    assert client.post("/session").status_code == 503


@pytest.mark.parametrize("raised", [
    FileNotFoundError("kubectl 없음"),                      # 미설치 (OSError 계열)
    __import__("subprocess").TimeoutExpired("kubectl", 10),  # 응답 없음
])
def test_kubectl_실행_자체가_실패해도_KubeconfigError로_잡힌다(monkeypatch, raised):
    """returncode 검사를 못 가보는 예외(미설치·타임아웃)도 503 경로에 태운다 — 500 으로 새지 않게."""
    from kukie.kubectl import config as kubeconfig

    def boom(*a, **kw):
        raise raised
    monkeypatch.setattr(kubeconfig.subprocess, "run", boom)
    with pytest.raises(kubeconfig.KubeconfigError):
        kubeconfig.read_kubeconfig()


# ── 채팅: 답변 경로 ─────────────────────────────────────────

def test_채팅은_answer와_조립된_KukieResponse를_돌려준다(client):
    client.post("/session")
    with agent.override(model=_model("파드 하나 떠 있어요")):
        r = client.post("/chat", json={"text": "파드 보여줘"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "answer" and body["skill"] == "학습"
    assert body["response"]["narration"] == "파드 하나 떠 있어요"
    assert body["response"]["steps"][0]["command"] == FAKE_COMMAND   # 사실 칸 = 실행 기록 (DURO-44)


def test_대화_기록은_턴_사이에_이어진다(client):
    client.post("/session")
    with agent.override(model=_model()):
        client.post("/chat", json={"text": "첫 질문"})
        n_after_first = len(server._session.history)
        client.post("/chat", json={"text": "둘째 질문"})
    assert len(server._session.history) > n_after_first


# ── 모드 전환 ────────────────────────────────────────────────

def test_mode_명령은_LLM_없이_스킬만_바꾼다(client):
    client.post("/session")
    with agent.override(model=TestModel()) as _:
        r = client.post("/chat", json={"text": "/mode 진단"})
    assert r.json() == {"kind": "mode", "skill": "진단", "known": True}
    assert client.get("/session").json()["skill"] == "진단"
    assert server._session.history == []            # 에이전트 호출 없음


def test_모르는_모드는_현재_스킬_유지(client):
    client.post("/session")
    r = client.post("/chat", json={"text": "/mode 없는모드"})
    assert r.json()["skill"] == "학습" and r.json()["known"] is False


# ── 승인 대기: 잠금과 티켓 분기 ──────────────────────────────

def _fake_result(output):
    return SimpleNamespace(output=output, all_messages=lambda: ["기록"])


def _ready_plan_for_server(call_id: str) -> ActionPlan:
    plan = ActionPlan.create_draft(
        call_id=call_id,
        tool="scale_resource",
        args={
            "kind": "deployment",
            "name": "nginx",
            "replicas": 3,
            "namespace": "study",
        },
        command=["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        risk="caution",
        skill="실습",
        target={
            "context": "kind-dev",
            "namespace": "study",
            "kind": "deployment",
            "name": "nginx",
        },
        intent="nginx 레플리카를 늘린다.",
        expected_effects=["레플리카가 3개가 된다."],
        side_effects=["추가 노드 자원을 사용한다."],
    )
    plan.record_dry_run("succeeded", "deployment.apps/nginx configured\n", "")
    plan.record_decision_guidance("현재 replica와 가용 자원을 확인한다.")
    return plan


def _pending_ticket_for_server(
    call_id: str,
) -> tuple[ActionPlan, DeferredToolRequests]:
    plan = _ready_plan_for_server(call_id)
    call = ToolCallPart(
        tool_name="scale_resource",
        args={
            "kind": "deployment",
            "name": "nginx",
            "replicas": 3,
            "namespace": "study",
            "intent": plan.intent,
            "expected_effects": plan.expected_effects,
            "side_effects": plan.side_effects,
        },
        tool_call_id=call_id,
    )
    return plan, DeferredToolRequests(
        approvals=[call],
        metadata={call_id: {"plan_id": plan.id}},
    )


@pytest.mark.asyncio
async def test_서버_run은_스킬응답과_Deferred출력을_모두_유지한다(client, monkeypatch):
    client.post("/session")
    seen = {}

    async def fake_agent_run(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(server.agent, "run", fake_agent_run)

    await server._run_agent(server._session, user_prompt="nginx를 늘려줘")

    assert seen["output_type"] == [
        server._session.skill.output_fn,
        DeferredToolRequests,
    ]


def test_티켓은_Plan_DTO와_원본_Deferred요청을_보관한다(client):
    client.post("/session")
    plan, ticket = _pending_ticket_for_server("c1")

    payload = server._to_payload(server._session, _fake_result(ticket))

    assert payload["kind"] == "approval"
    approval = payload["approvals"][0]
    assert approval["tool_call_id"] == "c1"
    assert approval["plan_id"] == plan.id
    assert approval["tool"] == "scale_resource"
    assert "args" not in approval
    assert server._session.pending is ticket
    assert server._session.history == ["기록"]
    assert client.get("/session").json()["pending"] == ["c1"]


def test_renderer는_call_id와_결정외_필드를_제출할수없다(client):
    client.post("/session")
    response = client.post(
        "/approve",
        json={
            "call_id": "c1",
            "approved": True,
            "tool": "delete_resource",
            "args": {"name": "other"},
        },
    )
    assert response.status_code == 422


def test_Plan과_다른_pending_args는_승인카드로_내보내지않는다(client):
    client.post("/session")
    plan = _ready_plan_for_server("c1")
    call = ToolCallPart(
        tool_name="scale_resource",
        args={
            "kind": "deployment",
            "name": "nginx",
            "replicas": 99,
            "namespace": "study",
            "intent": plan.intent,
            "expected_effects": plan.expected_effects,
            "side_effects": plan.side_effects,
        },
        tool_call_id="c1",
    )
    ticket = DeferredToolRequests(
        approvals=[call],
        metadata={"c1": {"plan_id": plan.id}},
    )

    with pytest.raises(ValueError, match="pending approval mismatch"):
        server._to_payload(server._session, _fake_result(ticket))


def test_승인_대기_중에는_채팅이_409로_막힌다(client):
    client.post("/session")
    _, server._session.pending = _pending_ticket_for_server("c1")
    assert client.post("/chat", json={"text": "딴 얘기"}).status_code == 409


def test_승인_처리_중에도_채팅이_409로_막힌다(client):
    client.post("/session")
    server._session.processing = True
    assert client.post("/chat", json={"text": "딴 얘기"}).status_code == 409


def test_대기_중이_아닌_call_id로_승인하면_409(client):
    client.post("/session")
    assert client.post("/approve", json={"call_id": "없음", "approved": True}).status_code == 409


def test_승인은_재개_run을_돌리고_답변이_오면_잠금이_풀린다(client, monkeypatch):
    client.post("/session")
    _, server._session.pending = _pending_ticket_for_server("c1")
    seen = {}

    async def fake_run(session, **kwargs):
        seen.update(kwargs)
        from kukie.skills.base import KukieResponse
        return _fake_result(KukieResponse(narration="삭제했어요"))

    monkeypatch.setattr(server, "_run_agent", fake_run)
    r = client.post("/approve", json={"call_id": "c1", "approved": True})
    assert r.json()["kind"] == "answer"
    assert r.json()["response"]["narration"] == "삭제했어요"
    assert seen["deferred_tool_results"].approvals == {"c1": True}   # 번호 + O/X 만 전달
    assert server._session.pending is None                            # 잠금 해제
    assert server._session.processing is False


def test_승인_call_id는_재개_시작_전에_예약된다(client, monkeypatch):
    """run 이 도는 동안 같은 call_id 의 두 번째 /approve 가 검사를 통과하면 같은 승인이
    두 번 재개된다 (CodeRabbit 지적). 원본 티켓은 유지하되 처리 표시를 run 전에 예약한다."""
    client.post("/session")
    _, ticket = _pending_ticket_for_server("c1")
    server._session.pending = ticket

    async def fake_run(session, **kwargs):
        assert session.pending is ticket
        assert session.processing is True
        from kukie.skills.base import KukieResponse
        return _fake_result(KukieResponse(narration="ok"))

    monkeypatch.setattr(server, "_run_agent", fake_run)
    assert client.post("/approve", json={"call_id": "c1", "approved": True}).status_code == 200
    # 소비된 call_id 로 다시 오면 (동시든 재전송이든) 409
    assert client.post("/approve", json={"call_id": "c1", "approved": True}).status_code == 409


def test_재개가_실패하면_예약이_복원되어_재시도할_수_있다(client, monkeypatch):
    client.post("/session")
    _, ticket = _pending_ticket_for_server("c1")
    server._session.pending = ticket

    async def boom(session, **kwargs):
        raise RuntimeError("LLM 연결 실패")

    monkeypatch.setattr(server, "_run_agent", boom)
    with pytest.raises(RuntimeError):
        client.post("/approve", json={"call_id": "c1", "approved": True})
    assert server._session.pending is ticket
    assert server._session.processing is False


# ── 6. 실행 잠금 — run 이 도는 동안 새 요청은 409 ─────────────

def _ok_result():
    from kukie.skills.base import KukieResponse
    return _fake_result(KukieResponse(narration="ok"))


def test_승인_재개_중에는_채팅이_409로_막힌다(client, monkeypatch):
    """승인 재개가 도는 동안은 processing 잠금이 다른 run을 막아야 한다."""
    from fastapi import HTTPException

    client.post("/session")
    _, ticket = _pending_ticket_for_server("c1")
    server._session.pending = ticket
    seen = {}

    async def fake_run(session, **kwargs):
        assert session.pending is ticket
        assert session.processing is True
        with pytest.raises(HTTPException) as exc:          # 재개 도중 /chat 끼어들기
            await server.chat(server.ChatIn(text="딴 얘기"))
        seen["chat_blocked"] = exc.value.status_code
        return _ok_result()

    monkeypatch.setattr(server, "_run_agent", fake_run)
    assert client.post("/approve", json={"call_id": "c1", "approved": True}).status_code == 200
    assert seen["chat_blocked"] == 409
    assert server._session.processing is False             # 끝나면 잠금 해제


def test_채팅_실행_중_두번째_채팅은_409(client, monkeypatch):
    from fastapi import HTTPException

    client.post("/session")
    seen = {}

    async def fake_run(session, **kwargs):
        with pytest.raises(HTTPException) as exc:          # 첫 run 도중 둘째 /chat
            await server.chat(server.ChatIn(text="동시 요청"))
        seen["second_blocked"] = exc.value.status_code
        return _ok_result()

    monkeypatch.setattr(server, "_run_agent", fake_run)
    assert client.post("/chat", json={"text": "첫 요청"}).status_code == 200
    assert seen["second_blocked"] == 409
    assert server._session.processing is False


def test_실행_중에는_세션_교체도_409(client, monkeypatch):
    """run 도중 /session 이 세션을 갈아치우면 진행 중 run 의 결과가 새 세션에 섞인다."""
    from fastapi import HTTPException

    client.post("/session")
    seen = {}

    async def fake_run(session, **kwargs):
        with pytest.raises(HTTPException) as exc:
            await server.start_session()   # async def — 루프에서 돌므로 이 직접 호출이 실전과 동일 경로
        seen["session_blocked"] = exc.value.status_code
        return _ok_result()

    monkeypatch.setattr(server, "_run_agent", fake_run)
    assert client.post("/chat", json={"text": "안녕"}).status_code == 200
    assert seen["session_blocked"] == 409
