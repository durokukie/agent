"""대화(채팅방) 단위 엔드포인트 — DB 기록·멱등성·잠금·복원·권한.

flat 엔드포인트 테스트(test_server.py)의 헬퍼를 그대로 빌려 승인 티켓을 만든다.
LLM 은 TestModel / _run_agent 바꿔치기, kubectl 은 가짜, DB 는 임시 SQLite.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, UserPromptPart
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
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")            # Spring 없이 X-User 헤더로 사용자 구분
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


def test_shared_방은_남도_같이_입력하고_승인한다(client):
    """기획 05 §4: 여러 팀원이 하나의 Shared Session 에 참여하고 메시지를 보낼 수 있다."""
    shared = client.post("/conversations", json={"shared": True}, headers=OTHER).json()["conversation"]["id"]
    assert client.get(f"/conversations/{shared}", headers=USER).json()["conversation"]["user_id"] == "u-2"
    _, ticket = _pending_ticket_for_server("call-s")
    restore = _swap_run_agent([_fake(ticket), _fake(KukieResponse(narration="남이 승인해서 적용"))])
    try:
        r = client.post(f"/conversations/{shared}/chat", json={"text": "늘려"}, headers=USER)     # 주인 아님
        assert r.status_code == 200 and r.json()["kind"] == "approval"
        r = client.post(f"/conversations/{shared}/approve", json={"call_id": "call-s", "approved": True}, headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "남이 승인해서 적용"
    finally:
        restore()


def test_private_방은_실습_모드와_승인이_막힌다(client):
    """기획 05 §3: Private 는 조회·진단만, 실제 변경은 불가. 변경이 필요하면 Shared 를 새로 만든다."""
    cid = _new(client)                                                   # shared=False
    r = client.post(f"/conversations/{cid}/chat", json={"text": "/mode 실습"}, headers=USER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "PRIVATE_SESSION"
    assert client.get(f"/conversations/{cid}", headers=USER).json()["session"]["skill"] == "학습"   # 안 바뀜
    assert client.get(f"/conversations/{cid}", headers=USER).json()["turns"] == []             # run 도 안 남음
    assert client.post(f"/conversations/{cid}/chat", json={"text": "/mode 진단"}, headers=USER).status_code == 200
    r = client.post(f"/conversations/{cid}/approve", json={"call_id": "c", "approved": True}, headers=USER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "PRIVATE_SESSION"
    assert client.post(f"/conversations/{cid}/resume", headers=USER).status_code == 403
    assert client.post(f"/conversations/{_new(client, shared=True)}/chat", json={"text": "/mode 실습"},
                       headers=USER).json() == {"kind": "mode", "skill": "실습", "known": True}


def test_회원_서버가_설정되면_개발용_헤더와_변수는_무시된다(client, monkeypatch):
    monkeypatch.setenv("KUKIE_MEMBER_URL", "http://member.invalid")
    monkeypatch.setenv("KUKIE_DEV_USER", "dev")
    r = client.post("/conversations", json={}, headers=USER)       # X-User 만 있고 Bearer 없음
    assert r.status_code == 401 and r.json()["detail"]["code"] == "UNAUTHORIZED"


def test_회원_서버도_개발_모드도_없으면_503_AUTH_NOT_CONFIGURED(client, monkeypatch):
    monkeypatch.delenv("KUKIE_DEV_AUTH")
    r = client.post("/conversations", json={}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "AUTH_NOT_CONFIGURED"


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


def test_실패한_요청을_같은_request_id로_다시_보내면_저장된_실패를_준다(client):
    cid = _new(client)
    calls = 0

    async def boom(session, **kw):
        nonlocal calls
        calls += 1
        raise RuntimeError("모델 죽음")

    import kukie.server as srv
    original = srv._run_agent
    srv._run_agent = boom
    try:
        body = {"text": "파드", "request_id": "req-fail"}
        first = client.post(f"/conversations/{cid}/chat", json=body, headers=USER)
        second = client.post(f"/conversations/{cid}/chat", json=body, headers=USER)
    finally:
        srv._run_agent = original
    assert first.status_code == 500 and second.status_code == 500       # BUSY 가 아니라 같은 실패
    assert second.json()["detail"]["code"] == "RUN_FAILED" and calls == 1
    assert "모델 죽음" not in first.json()["detail"]["message"]         # 예외 문자열은 응답에 안 싣는다
    # 새 request_id 면 다시 실행한다
    with agent.override(model=_model("살아남")):
        assert client.post(f"/conversations/{cid}/chat", json={"text": "파드"}, headers=USER).status_code == 200


# ── 승인 · 재개 ────────────────────────────────────────────

def _fake(output, new_messages=()):
    msgs = list(new_messages)
    return SimpleNamespace(output=output, all_messages=lambda: msgs, new_messages=lambda: msgs)


def _swap_run_agent(outputs):
    """_run_agent 를 outputs 순서대로 돌려주는 가짜로. 반환은 원복 함수."""
    import kukie.server as srv
    original = srv._run_agent
    queue = list(outputs)

    async def fake(session, **kw):
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    srv._run_agent = fake
    return lambda: setattr(srv, "_run_agent", original)


def test_chat이_승인_카드를_주면_approve가_같은_run을_이어서_끝내고_다음_chat이_된다(client):
    """DB 문서 7절: 승인만으로 새 run 을 만들지 않는다 — 카드 run(awaiting_approval)을 이어서 completed 로."""
    cid = _new(client, shared=True)
    _, ticket = _pending_ticket_for_server("call-f")
    card_msgs = [ModelRequest(parts=[UserPromptPart(content="nginx 3개로")])]
    resume_msgs = [ModelRequest(parts=[UserPromptPart(content="(tool 결과)")])]
    restore = _swap_run_agent([_fake(ticket, card_msgs), _fake(KukieResponse(narration="적용됨"), resume_msgs),
                               _fake(KukieResponse(narration="다음 턴"))])
    try:
        r = client.post(f"/conversations/{cid}/chat", json={"text": "nginx 3개로"}, headers=USER)
        assert r.status_code == 200 and r.json()["kind"] == "approval"
        detail = client.get(f"/conversations/{cid}", headers=USER).json()
        assert detail["turns"][0]["status"] == "awaiting_approval" and detail["turns"][0]["finished_at"] is None
        assert detail["conversation"]["running"] is False                # 대기는 "실행 중" 이 아니다
        r = client.post(f"/conversations/{cid}/chat", json={"text": "x"}, headers=USER)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PENDING_APPROVAL"   # 활성 run 하나

        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "call-f", "approved": True}, headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "적용됨"
        turns = client.get(f"/conversations/{cid}", headers=USER).json()["turns"]
        assert [(t["kind"], t["status"]) for t in turns] == [("chat", "completed")]   # run 은 여전히 하나
        assert turns[0]["payload"]["kind"] == "answer" and turns[0]["finished_at"] is not None
        assert conversations.registry.get(cid).session.pending is None

        # 재시작해도 카드 메시지 + 재개 메시지가 한 run 에 이어져 있어 history 가 온전하다
        conversations.registry.clear()
        client.get(f"/conversations/{cid}", headers=USER)
        assert len(conversations.registry.get(cid).session.history) == 2

        r = client.post(f"/conversations/{cid}/chat", json={"text": "다음"}, headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "다음 턴"
        assert len(client.get(f"/conversations/{cid}", headers=USER).json()["turns"]) == 2
    finally:
        restore()


def test_승인_대기_중_재시작하면_카드는_만료되고_그_턴의_기록은_history에서_뺀다(client):
    """리뷰 P1/🟡: 재시작 뒤 활성 run 이 남아 영구 BUSY, 결과 없는 tool call 이 history 에 남는 문제."""
    cid = _new(client, shared=True)
    _, ticket = _pending_ticket_for_server("call-x")
    dangling = [ModelRequest(parts=[UserPromptPart(content="nginx 3개로")])]
    restore = _swap_run_agent([_fake(ticket, dangling), _fake(KukieResponse(narration="재시작 뒤"))])
    try:
        assert client.post(f"/conversations/{cid}/chat", json={"text": "nginx 3개로"}, headers=USER).json()["kind"] == "approval"
        conversations.registry.clear()                                   # 프로세스 재시작 흉내

        detail = client.get(f"/conversations/{cid}", headers=USER).json()
        assert detail["turns"][0]["status"] == "interrupted" and detail["turns"][0]["error"]["code"] == "INTERRUPTED"
        assert detail["turns"][0]["payload"] is None                     # 만료된 카드는 다시 그리지 않는다
        assert detail["session"]["pending"] == [] and detail["conversation"]["running"] is False
        assert conversations.registry.get(cid).session.history == []   # 만료된 턴의 메시지는 버린다

        r = client.post(f"/conversations/{cid}/chat", json={"text": "다시"}, headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "재시작 뒤"
    finally:
        restore()


def test_승인_대기는_그_방만_막고_다른_방은_자유다(client):
    a, b = _new(client, shared=True), _new(client)
    _, ticket = _pending_ticket_for_server("call-a")
    restore = _swap_run_agent([_fake(ticket), _fake(KukieResponse(narration="b 답")),
                               _fake(KukieResponse(narration="적용됨"))])
    try:
        assert client.post(f"/conversations/{a}/chat", json={"text": "늘려"}, headers=USER).json()["kind"] == "approval"
        # a 는 승인 대기 → chat 막힘, b 는 자유
        r = client.post(f"/conversations/{a}/chat", json={"text": "x"}, headers=USER)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PENDING_APPROVAL"
        r = client.post(f"/conversations/{b}/chat", json={"text": "x"}, headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "b 답"

        r = client.post(f"/conversations/{a}/approve", json={"call_id": "call-a", "approved": True}, headers=USER)
        assert r.status_code == 200 and r.json()["kind"] == "answer"
    finally:
        restore()
    turns = client.get(f"/conversations/{a}", headers=USER).json()["turns"]
    assert [(t["kind"], t["status"]) for t in turns] == [("chat", "completed")]
    assert conversations.registry.get(a).session.pending is None


def test_카드가_여러_장이면_마지막_결정까지_run은_열려_있고_payload는_남은_카드다(client):
    """문서 7절: 계획이 여러 개면 결정이 모일 때까지 기다린다."""
    from pydantic_ai.tools import DeferredToolRequests
    cid = _new(client, shared=True)
    _, first = _pending_ticket_for_server("c1")
    _, second = _pending_ticket_for_server("c2")
    batch = DeferredToolRequests(approvals=[first.approvals[0], second.approvals[0]],
                                 metadata={**first.metadata, **second.metadata})
    restore = _swap_run_agent([_fake(batch), _fake(KukieResponse(narration="둘 다 적용"))])
    try:
        assert client.post(f"/conversations/{cid}/chat", json={"text": "두 개"}, headers=USER).json()["kind"] == "approval"
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "c1", "approved": True}, headers=USER)
        assert r.json()["kind"] == "approval"                            # 카드 한 장 남음
        turns = client.get(f"/conversations/{cid}", headers=USER).json()["turns"]
        assert [t["status"] for t in turns] == ["awaiting_approval"]
        assert [c["tool_call_id"] for c in turns[0]["payload"]["approvals"]] == ["c2"]
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "c2", "approved": True}, headers=USER)
        assert r.json()["response"]["narration"] == "둘 다 적용"
        turns = client.get(f"/conversations/{cid}", headers=USER).json()["turns"]
        assert [t["status"] for t in turns] == ["completed"]
    finally:
        restore()


def test_실행_중_죽은_run은_처음_열_때_interrupted가_되고_running도_같이_꺼진다(client):
    """리뷰 🟡: 복원 전에 읽은 row 의 running 이 낡은 값으로 나가던 문제."""
    from kukie.store import get_store
    cid = _new(client)
    get_store().start_run(cid, request_id="crash", kind="chat", mode="학습", input_text="죽기 직전")  # 끝내지 않음
    conversations.registry.clear()
    detail = client.get(f"/conversations/{cid}", headers=USER).json()
    assert detail["conversation"]["running"] is False
    assert detail["turns"][0]["status"] == "interrupted"
    assert client.get("/conversations", headers=USER).json()[0]["running"] is False
    with agent.override(model=_model("살아남")):
        assert client.post(f"/conversations/{cid}/chat", json={"text": "다시"}, headers=USER).status_code == 200


def test_모르는_call_id는_409_NOT_PENDING(client):
    cid = _new(client, shared=True)
    r = client.post(f"/conversations/{cid}/approve", json={"call_id": "없음", "approved": True}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


def test_재개_실패는_run을_recovery_required로_남기고_resume이_같은_run을_끝낸다(client):
    """문서 4절: recovery_required = 실제 변경 결과가 불명확해 확인 필요. DURO-66 의 RESUME_RETRYABLE 과 같다."""
    cid = _new(client, shared=True)
    _, ticket = _pending_ticket_for_server("call-r")
    restore = _swap_run_agent([_fake(ticket), RuntimeError("네트워크"), _fake(KukieResponse(narration="이번엔 됨"))])
    try:
        assert client.post(f"/conversations/{cid}/chat", json={"text": "늘려"}, headers=USER).json()["kind"] == "approval"
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "call-r", "approved": True}, headers=USER)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "RESUME_RETRYABLE"
        assert conversations.registry.get(cid).session.pending is not None       # 티켓 유지
        turns = client.get(f"/conversations/{cid}", headers=USER).json()["turns"]
        assert [(t["kind"], t["status"]) for t in turns] == [("chat", "recovery_required")]
        assert turns[0]["error"]["code"] == "RESUME_RETRYABLE" and turns[0]["finished_at"] is None
        r = client.post(f"/conversations/{cid}/chat", json={"text": "x"}, headers=USER)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "RECOVERY_REQUIRED"

        r = client.post(f"/conversations/{cid}/resume", headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "이번엔 됨"
    finally:
        restore()
    turns = client.get(f"/conversations/{cid}", headers=USER).json()["turns"]
    assert [(t["kind"], t["status"]) for t in turns] == [("chat", "completed")]
    assert turns[0]["error"] is None and turns[0]["payload"]["kind"] == "answer"


def test_recovery_required_중_재시작하면_interrupted지만_확인_필요_코드는_남는다(client):
    cid = _new(client, shared=True)
    _, ticket = _pending_ticket_for_server("call-k")
    restore = _swap_run_agent([_fake(ticket), RuntimeError("네트워크")])
    try:
        client.post(f"/conversations/{cid}/chat", json={"text": "늘려"}, headers=USER)
        client.post(f"/conversations/{cid}/approve", json={"call_id": "call-k", "approved": True}, headers=USER)
    finally:
        restore()
    conversations.registry.clear()
    turn = client.get(f"/conversations/{cid}", headers=USER).json()["turns"][0]
    assert turn["status"] == "interrupted" and turn["error"]["code"] == "RESUME_RETRYABLE"
    assert turn["finished_at"] is not None


def test_응답이_유실된_뒤_같은_request_id로_재전송하면_승인_대기_중이라도_카드를_다시_준다(client):
    cid = _new(client, shared=True)
    _, ticket = _pending_ticket_for_server("call-l")
    restore = _swap_run_agent([_fake(ticket)])
    try:
        body = {"text": "늘려", "request_id": "lost"}
        first = client.post(f"/conversations/{cid}/chat", json=body, headers=USER).json()
        again = client.post(f"/conversations/{cid}/chat", json=body, headers=USER)
    finally:
        restore()
    assert again.status_code == 200 and again.json() == first                   # PENDING_APPROVAL 이 아니라 재생
    conversations.registry.clear()                                               # 재시작 뒤엔 만료된 카드 대신 오류
    again = client.post(f"/conversations/{cid}/chat", json=body, headers=USER)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "INTERRUPTED"


def test_재개할_티켓이_없으면_409_NOT_PENDING(client):
    cid = _new(client, shared=True)
    r = client.post(f"/conversations/{cid}/resume", headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


async def _async(value):
    return value
