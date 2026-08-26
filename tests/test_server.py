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


def test_티켓이_오면_approval_payload와_pending이_생긴다(client):
    client.post("/session")
    call = ToolCallPart(tool_name="delete_resource",
                        args={"kind": "deployment", "name": "nginx", "namespace": "study"},
                        tool_call_id="c1")
    ticket = DeferredToolRequests(approvals=[call], metadata={"c1": {"plan_id": "ap-x"}})
    payload = server._to_payload(server._session, _fake_result(ticket))
    assert payload["kind"] == "approval"
    assert payload["approvals"][0]["call_id"] == "c1"
    assert payload["approvals"][0]["tool"] == "delete_resource"
    assert payload["approvals"][0]["plan"] is None          # 계획서 파일이 없으면 None (뼈대)
    assert server._session.pending == ["c1"]
    assert server._session.history == ["기록"]


def test_승인_대기_중에는_채팅이_409로_막힌다(client):
    client.post("/session")
    server._session.pending = ["c1"]
    assert client.post("/chat", json={"text": "딴 얘기"}).status_code == 409


def test_대기_중이_아닌_call_id로_승인하면_409(client):
    client.post("/session")
    assert client.post("/approve", json={"call_id": "없음", "approved": True}).status_code == 409


def test_승인은_재개_run을_돌리고_답변이_오면_잠금이_풀린다(client, monkeypatch):
    client.post("/session")
    server._session.pending = ["c1"]
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
    assert server._session.pending == []                              # 잠금 해제


def test_승인_call_id는_재개_시작_전에_예약된다(client, monkeypatch):
    """run 이 도는 동안 같은 call_id 의 두 번째 /approve 가 검사를 통과하면 같은 승인이
    두 번 재개된다 (CodeRabbit 지적). 예약(제거)이 run 전에 일어나야 둘째가 409 로 걸린다."""
    client.post("/session")
    server._session.pending = ["c1"]

    async def fake_run(session, **kwargs):
        assert "c1" not in session.pending   # run 시작 시점에 이미 예약(제거)되어 있어야 한다
        from kukie.skills.base import KukieResponse
        return _fake_result(KukieResponse(narration="ok"))

    monkeypatch.setattr(server, "_run_agent", fake_run)
    assert client.post("/approve", json={"call_id": "c1", "approved": True}).status_code == 200
    # 소비된 call_id 로 다시 오면 (동시든 재전송이든) 409
    assert client.post("/approve", json={"call_id": "c1", "approved": True}).status_code == 409


def test_재개가_실패하면_예약이_복원되어_재시도할_수_있다(client, monkeypatch):
    client.post("/session")
    server._session.pending = ["c1"]

    async def boom(session, **kwargs):
        raise RuntimeError("LLM 연결 실패")

    monkeypatch.setattr(server, "_run_agent", boom)
    with pytest.raises(RuntimeError):
        client.post("/approve", json={"call_id": "c1", "approved": True})
    assert server._session.pending == ["c1"]   # 예약 반환 — 승인 건이 증발하지 않는다
