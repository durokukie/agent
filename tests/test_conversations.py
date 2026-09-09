"""대화(채팅방) 단위 엔드포인트 — DB 기록·멱등성·잠금·복원·권한.

flat 엔드포인트 테스트(test_server.py)의 헬퍼를 그대로 빌려 승인 티켓을 만든다.
LLM 은 TestModel / _run_agent 바꿔치기, kubectl 은 가짜, DB 는 임시 SQLite.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from kukie import conversations, server
from kukie.agent import agent
from kukie.guardrail import action_plan
from kukie.kubectl import KubectlResult
from kukie.skills.base import KukieResponse
from kukie.store import reset_store_for_tests
from kukie.tools import read as read_tools
from test_server import FAKE_COMMAND, _model, _pending_ticket_for_server

USER = {"X-User": "u-1"}
OTHER = {"X-User": "u-2"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.delenv("KUKIE_DEV_USER", raising=False)
    reset_store_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    conversations.registry.clear()
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    monkeypatch.setattr(read_tools, "run_kubectl",
                        lambda args, *, context, dry_run=False, stdin=None, timeout=30:
                        KubectlResult(command=FAKE_COMMAND, stdout="nginx Running", stderr="", success=True))
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    return TestClient(server.app)


def _new(client, **body) -> str:
    r = client.post("/conversations", json=body, headers=USER)
    assert r.status_code == 200, r.text
    return r.json()["conversation"]["id"]


# ── 만들기 · 목록 · 권한 ───────────────────────────────────

def test_채팅방을_만들면_kubeconfig_대상으로_세션이_열린다(client):
    r = client.post("/conversations", json={}, headers=USER)
    body = r.json()
    assert body["conversation"]["title"] == "새 대화" and body["conversation"]["running"] is False
    assert body["session"] == {"context": "kind-dev", "namespace": "study", "skill": "학습", "pending": []}


def test_context를_직접_주면_kubeconfig를_읽지_않는다(client, monkeypatch):
    monkeypatch.setattr(server, "read_kubeconfig", lambda: (_ for _ in ()).throw(AssertionError("읽으면 안 됨")))
    r = client.post("/conversations", json={"context": "prod", "namespace": "web", "cluster_id": "cl-1"},
                    headers=USER)
    assert r.json()["session"]["context"] == "prod"
    assert r.json()["conversation"]["cluster_id"] == "cl-1"


def test_kubeconfig를_못_읽으면_503_KUBECONFIG(client, monkeypatch):
    from kukie.kubectl.config import KubeconfigError
    monkeypatch.setattr(server, "read_kubeconfig", lambda: (_ for _ in ()).throw(KubeconfigError("없음")))
    r = client.post("/conversations", json={}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "KUBECONFIG"


def test_목록은_내_채팅방만_최신순(client):
    a = _new(client, title="첫째")
    b = _new(client, title="둘째")
    client.post("/conversations", json={"title": "남의 것"}, headers=OTHER)
    ids = [c["id"] for c in client.get("/conversations", headers=USER).json()]
    assert ids == [b, a]


def test_남의_채팅방은_404_공유면_보인다(client):
    mine = _new(client)
    shared = client.post("/conversations", json={"shared": True}, headers=OTHER).json()["conversation"]["id"]
    assert client.get(f"/conversations/{mine}", headers=OTHER).status_code == 404
    assert client.get(f"/conversations/{shared}", headers=USER).status_code == 200


def test_인증_없으면_401(client):
    r = client.post("/conversations", json={})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "UNAUTHORIZED"


# ── 채팅 · run 기록 ────────────────────────────────────────

def test_채팅은_answer를_주고_turn으로_남는다(client):
    cid = _new(client)
    with agent.override(model=_model("파드 하나")):
        r = client.post(f"/conversations/{cid}/chat", json={"text": "파드 보여줘"}, headers=USER)
    assert r.status_code == 200 and r.json()["kind"] == "answer"

    turns = client.get(f"/conversations/{cid}", headers=USER).json()["turns"]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["seq"] == 1 and turn["kind"] == "chat" and turn["status"] == "completed"
    assert turn["input_text"] == "파드 보여줘"
    assert turn["payload"]["response"]["narration"] == "파드 하나"
    assert turn["finished_at"] is not None


def test_같은_request_id_재전송은_실행_없이_저장된_응답을_준다(client):
    cid = _new(client)
    calls = 0

    async def fake_run(session, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(output=KukieResponse(narration="한 번만"), all_messages=lambda: [],
                               new_messages=lambda: [])

    import kukie.server as srv
    original = srv._run_agent
    srv._run_agent = fake_run
    try:
        body = {"text": "파드", "request_id": "req-1"}
        first = client.post(f"/conversations/{cid}/chat", json=body, headers=USER).json()
        second = client.post(f"/conversations/{cid}/chat", json=body, headers=USER).json()
    finally:
        srv._run_agent = original
    assert first == second and calls == 1
    assert len(client.get(f"/conversations/{cid}", headers=USER).json()["turns"]) == 1


def test_같은_request_id에_다른_입력은_409_REQUEST_MISMATCH(client):
    cid = _new(client)
    with agent.override(model=_model()):
        client.post(f"/conversations/{cid}/chat", json={"text": "하나", "request_id": "r"}, headers=USER)
        r = client.post(f"/conversations/{cid}/chat", json={"text": "둘", "request_id": "r"}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "REQUEST_MISMATCH"


def test_모드_전환은_LLM_없이_run으로_남고_채팅방_모드가_바뀐다(client):
    cid = _new(client)
    r = client.post(f"/conversations/{cid}/chat", json={"text": "/mode 진단"}, headers=USER)
    assert r.json() == {"kind": "mode", "skill": "진단", "known": True}
    detail = client.get(f"/conversations/{cid}", headers=USER).json()
    assert detail["session"]["skill"] == "진단"
    assert detail["turns"][0]["kind"] == "mode_change" and detail["turns"][0]["payload"]["kind"] == "mode"


def test_모델_예외는_run을_failed로_남기고_500_RUN_FAILED(client):
    cid = _new(client)

    async def boom(session, **kwargs):
        raise RuntimeError("모델 죽음")

    import kukie.server as srv
    original = srv._run_agent
    srv._run_agent = boom
    try:
        r = client.post(f"/conversations/{cid}/chat", json={"text": "파드"}, headers=USER)
    finally:
        srv._run_agent = original
    assert r.status_code == 500 and r.json()["detail"]["code"] == "RUN_FAILED"
    turn = client.get(f"/conversations/{cid}", headers=USER).json()["turns"][0]
    assert turn["status"] == "failed" and turn["error"]["code"] == "RUN_FAILED"
    # 세션은 살아 있다 — 다음 채팅이 된다
    with agent.override(model=_model("다시")):
        assert client.post(f"/conversations/{cid}/chat", json={"text": "다시"}, headers=USER).status_code == 200


# ── 채팅방 독립성 · 복원 ───────────────────────────────────

def test_채팅방끼리_기록과_모드가_섞이지_않는다(client):
    a, b = _new(client), _new(client)
    client.post(f"/conversations/{a}/chat", json={"text": "/mode 진단"}, headers=USER)
    with agent.override(model=_model()):
        client.post(f"/conversations/{b}/chat", json={"text": "파드"}, headers=USER)
    assert client.get(f"/conversations/{a}", headers=USER).json()["session"]["skill"] == "진단"
    assert client.get(f"/conversations/{b}", headers=USER).json()["session"]["skill"] == "학습"
    assert len(client.get(f"/conversations/{a}", headers=USER).json()["turns"]) == 1
    assert len(client.get(f"/conversations/{b}", headers=USER).json()["turns"]) == 1


def test_서버가_다시_떠도_기록과_history가_복원된다(client):
    cid = _new(client)
    with agent.override(model=_model("첫 답")):
        client.post(f"/conversations/{cid}/chat", json={"text": "첫 질문"}, headers=USER)
    live_history = len(conversations.registry.get(cid).session.history)
    assert live_history > 0

    conversations.registry.clear()               # 프로세스 재시작 흉내 — 메모리만 비운다
    detail = client.get(f"/conversations/{cid}", headers=USER).json()
    assert detail["turns"][0]["payload"]["response"]["narration"] == "첫 답"
    restored = conversations.registry.get(cid)
    assert restored is not None and len(restored.session.history) == live_history
    assert restored.session.skill.name == "학습" and restored.session.pending is None


# ── 승인 · 재개 ────────────────────────────────────────────

def _fake(output):
    return SimpleNamespace(output=output, all_messages=lambda: [], new_messages=lambda: [])


def test_승인은_채팅방_단위로_run을_남기고_잠금은_방마다다(client):
    a, b = _new(client), _new(client)
    plan, ticket = _pending_ticket_for_server("call-a")
    conversations.registry.get(a).session.pending = ticket

    # a 는 승인 대기 → chat 막힘, b 는 자유
    r = client.post(f"/conversations/{a}/chat", json={"text": "x"}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PENDING_APPROVAL"
    with agent.override(model=_model()):
        assert client.post(f"/conversations/{b}/chat", json={"text": "x"}, headers=USER).status_code == 200

    import kukie.server as srv
    original = srv._run_agent
    srv._run_agent = lambda session, **kw: _async(_fake(KukieResponse(narration="적용됨")))
    try:
        r = client.post(f"/conversations/{a}/approve", json={"call_id": "call-a", "approved": True}, headers=USER)
    finally:
        srv._run_agent = original
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    turns = client.get(f"/conversations/{a}", headers=USER).json()["turns"]
    assert [t["kind"] for t in turns] == ["approve"] and turns[0]["status"] == "completed"
    assert conversations.registry.get(a).session.pending is None


def test_모르는_call_id는_409_NOT_PENDING(client):
    cid = _new(client)
    r = client.post(f"/conversations/{cid}/approve", json={"call_id": "없음", "approved": True}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


def test_재개_실패는_503_RESUME_RETRYABLE이고_resume으로_이어간다(client):
    cid = _new(client)
    _, ticket = _pending_ticket_for_server("call-r")
    conversations.registry.get(cid).session.pending = ticket

    import kukie.server as srv
    original = srv._run_agent

    async def fail(session, **kw):
        raise RuntimeError("네트워크")

    srv._run_agent = fail
    try:
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "call-r", "approved": True}, headers=USER)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "RESUME_RETRYABLE"
        assert conversations.registry.get(cid).session.pending is ticket      # 티켓 유지
        srv._run_agent = lambda session, **kw: _async(_fake(KukieResponse(narration="이번엔 됨")))
        r = client.post(f"/conversations/{cid}/resume", headers=USER)
    finally:
        srv._run_agent = original
    assert r.status_code == 200 and r.json()["response"]["narration"] == "이번엔 됨"
    kinds = [t["kind"] for t in client.get(f"/conversations/{cid}", headers=USER).json()["turns"]]
    assert kinds == ["resume"]


def test_재개할_티켓이_없으면_409_NOT_PENDING(client):
    cid = _new(client)
    r = client.post(f"/conversations/{cid}/resume", headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


async def _async(value):
    return value
