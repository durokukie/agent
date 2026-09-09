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


def test_shared_방은_남도_읽고_입력하지만_승인은_아직_주인만이다(client):
    """기획 05 §4 는 팀원 공동 승인인데, 팀·Operator 정보가 오기 전까지는 만든 사람만 승인한다 (팀원 리뷰 6)."""
    shared = client.post("/conversations", json={"shared": True}, headers=OTHER).json()["conversation"]["id"]
    assert client.get(f"/conversations/{shared}", headers=USER).json()["conversation"]["user_id"] == "u-2"
    _, ticket = _pending_ticket_for_server("call-s")
    restore = _swap_run_agent([_fake(ticket), _fake(KukieResponse(narration="주인이 승인해서 적용"))])
    try:
        r = client.post(f"/conversations/{shared}/chat", json={"text": "늘려"}, headers=USER)     # 주인 아님 — 입력은 된다
        assert r.status_code == 200 and r.json()["kind"] == "approval"
        r = client.post(f"/conversations/{shared}/approve", json={"call_id": "call-s", "approved": True}, headers=USER)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"
        assert client.post(f"/conversations/{shared}/resume", headers=USER).status_code == 403
        r = client.post(f"/conversations/{shared}/approve", json={"call_id": "call-s", "approved": True}, headers=OTHER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "주인이 승인해서 적용"
    finally:
        restore()


def test_목록에_남이_만든_shared_방이_나온다(client):
    """팀원 리뷰 7: 읽고 입력할 수 있는 방을 목록에서 찾을 수 있어야 참여할 수 있다."""
    mine = _new(client)
    shared = client.post("/conversations", json={"shared": True}, headers=OTHER).json()["conversation"]["id"]
    private = client.post("/conversations", json={}, headers=OTHER).json()["conversation"]["id"]
    ids = {c["id"] for c in client.get("/conversations", headers=USER).json()}
    assert mine in ids and shared in ids and private not in ids


def test_빈_context는_kubeconfig의_실제_이름으로_확정한다(client):
    """팀원 리뷰 1: `kubectl --context ''` 는 현재 context 를 따라가서 나중에 방의 대상이 바뀔 수 있다."""
    r = client.post("/conversations", json={"context": "", "namespace": "  "}, headers=USER)
    assert r.status_code == 200
    assert r.json()["session"] == {"context": "kind-dev", "namespace": "study", "skill": "학습", "pending": []}


def test_private_방은_DB에_실습_모드가_남아_있어도_변경_모드로_복원하지_않는다(client):
    """자동 리뷰 5차: 게이트 전 커밋으로 남은 current_mode=실습 private 행을 그대로 열면 카드는 뜨고 승인은 403 이라 방이 막힌다."""
    from kukie.store import get_store
    cid = _new(client)
    get_store().update_session(cid, current_mode="실습")
    conversations.registry.clear()
    assert client.get(f"/conversations/{cid}", headers=USER).json()["session"]["skill"] == "학습"


def test_재개_뒤_새_카드가_나와도_그_사이_메시지가_run에_쌓인다(client):
    """팀원 리뷰 2: 첫 승인 → 실행 → 둘째 카드 → 승인 → 완료. 복원하면 첫 작업 결과와 둘째 호출이 빠지면 안 된다."""
    cid = _new(client, shared=True)
    _, first = _pending_ticket_for_server("c1")
    _, second = _pending_ticket_for_server("c2")
    m = lambda text: [ModelRequest(parts=[UserPromptPart(content=text)])]   # noqa: E731
    restore = _swap_run_agent([_fake(first, m("카드1")), _fake(second, m("결과1+호출2")),
                               _fake(KukieResponse(narration="끝"), m("결과2+답"))])
    try:
        assert client.post(f"/conversations/{cid}/chat", json={"text": "둘"}, headers=USER).json()["kind"] == "approval"
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "c1", "approved": True}, headers=USER)
        assert r.json()["kind"] == "approval"                                # 실제 재개가 돌아 새 카드가 나왔다
        assert [c["tool_call_id"] for c in r.json()["approvals"]] == ["c2"]
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "c2", "approved": True}, headers=USER)
        assert r.json()["response"]["narration"] == "끝"
    finally:
        restore()
    turns = client.get(f"/conversations/{cid}", headers=USER).json()["turns"]
    assert [t["status"] for t in turns] == ["completed"]
    conversations.registry.clear()
    client.get(f"/conversations/{cid}", headers=USER)
    history = conversations.registry.get(cid).session.history
    assert [msg.parts[0].content for msg in history] == ["카드1", "결과1+호출2", "결과2+답"]


def test_결과_저장이_실패해도_방이_영구_BUSY로_남지_않는다(client, monkeypatch):
    """팀원 리뷰 3: 실행 뒤 저장이 실패하면 DB 의 run 이 running 으로 남아 다음 요청이 계속 409 였다."""
    from kukie.store import get_store
    store = get_store()
    original = store.update_run
    calls = {"failed": 0}

    def flaky(run_id, **fields):
        if fields.get("status") == "completed" and calls["failed"] == 0:
            calls["failed"] += 1
            raise RuntimeError("disk full")
        return original(run_id, **fields)

    monkeypatch.setattr(store, "update_run", flaky)
    cid = _new(client)
    with agent.override(model=_model("첫 답")):
        r = client.post(f"/conversations/{cid}/chat", json={"text": "파드"}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
    assert "새 요청" in r.json()["detail"]["message"]                             # 같은 request_id 로는 같은 실패가 재생된다
    assert client.get(f"/conversations/{cid}", headers=USER).json()["turns"][0]["status"] == "failed"
    with agent.override(model=_model("둘째 답")):
        r = client.post(f"/conversations/{cid}/chat", json={"text": "파드"}, headers=USER)
    assert r.status_code == 200 and r.json()["response"]["narration"] == "둘째 답"


def test_저장_실패한_run을_같은_request_id로_재전송해도_영원히_BUSY가_아니다(client):
    """자동 리뷰 6차: 재전송 판정이 밀린 run 정리보다 앞이라, 같은 request_id 재시도가 저장된 실패·BUSY 를 영원히 재생했다."""
    from kukie.store import get_store
    cid = _new(client)
    # 대체 쓰기까지 실패해 running 으로 남은 run 을 흉내낸다
    get_store().start_run(cid, request_id="r-stuck", kind="chat", mode="학습", input_text="파드")
    r = client.post(f"/conversations/{cid}/chat", json={"text": "파드", "request_id": "r-stuck"}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INTERRUPTED"      # BUSY 가 아니라 "새 요청으로"
    assert "새 요청" in r.json()["detail"]["message"]
    with agent.override(model=_model("살아남")):
        assert client.post(f"/conversations/{cid}/chat", json={"text": "파드"}, headers=USER).status_code == 200


def test_승인_뒤_저장이_죽으면_recovery_required와_plan_ids로_남고_다음_chat이_방을_풀어준다(client, monkeypatch):
    """자동 리뷰 6차: kubectl 이 이미 돈 run 을 failed 로 적고 "다시 보내라" 하면 같은 변경을 또 시킬 수 있다.
    저장이 두 번 다 죽어 awaiting_approval 이 남아도 다음 chat 이 닫아야 한다."""
    from kukie.store import get_store
    store = get_store()
    original = store.update_run
    dead = {"on": False}

    def flaky(run_id, **fields):
        if dead["on"]:
            raise RuntimeError("db down")
        return original(run_id, **fields)

    monkeypatch.setattr(store, "update_run", flaky)
    cid = _new(client, shared=True)
    plan, ticket = _pending_ticket_for_server("call-st")
    restore = _swap_run_agent([_fake(ticket), _fake(KukieResponse(narration="적용됨"))])
    try:
        assert client.post(f"/conversations/{cid}/chat", json={"text": "늘려"}, headers=USER).json()["kind"] == "approval"
        dead["on"] = True                                                         # 승인 결과 저장부터 DB 가 죽는다
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "call-st", "approved": True}, headers=USER)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
        assert r.json()["detail"]["plan_ids"] == [plan.id]                       # 확인할 Plan 을 알려 준다
        assert "다시 보내지 말고" in r.json()["detail"]["message"]
        dead["on"] = False                                                        # DB 복구. run 은 awaiting_approval 그대로, 메모리 티켓은 없음
        assert store.active_run(cid).status == "awaiting_approval"
    finally:
        restore()
    with agent.override(model=_model("풀렸다")):
        r = client.post(f"/conversations/{cid}/chat", json={"text": "다음"}, headers=USER)
    assert r.status_code == 200 and r.json()["response"]["narration"] == "풀렸다"
    statuses = [t["status"] for t in client.get(f"/conversations/{cid}", headers=USER).json()["turns"]]
    assert statuses == ["interrupted", "completed"]


def test_남은_카드_재전송에서_저장이_죽어도_확인_필요가_아니라_카드_대기_그대로다(client, monkeypatch):
    """자동 리뷰 7차: 아무것도 실행하지 않은 경로(result 없음)에 recovery_required + "변경 적용됐을 수 있다" 를 적으면 사실 칸 오염."""
    from kukie.store import get_store
    from pydantic_ai.tools import DeferredToolRequests
    store = get_store()
    original = store.update_run
    dead = {"on": False}

    def flaky(run_id, **fields):
        if dead["on"]:
            dead["on"] = False                                                   # 한 번만 실패 — 대체 쓰기는 성공
            raise RuntimeError("db down")
        return original(run_id, **fields)

    monkeypatch.setattr(store, "update_run", flaky)
    cid = _new(client, shared=True)
    pa, first = _pending_ticket_for_server("d1")
    pb, second = _pending_ticket_for_server("d2")
    batch = DeferredToolRequests(approvals=[first.approvals[0], second.approvals[0]],
                                 metadata={**first.metadata, **second.metadata})
    restore = _swap_run_agent([_fake(batch), _fake(KukieResponse(narration="둘 다 적용"))])
    try:
        client.post(f"/conversations/{cid}/chat", json={"text": "둘"}, headers=USER)
        dead["on"] = True                                                        # 본 쓰기만 죽고 대체 쓰기는 산다
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "d1", "approved": True}, headers=USER)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
        assert "남은 카드를 계속 결정" in r.json()["detail"]["message"]          # "변경 적용됐을 수 있다" 가 아니다
        assert "plan_ids" not in r.json()["detail"]
        assert store.active_run(cid).status == "awaiting_approval"            # 확인 필요가 아니라 카드 대기 그대로
        # 티켓은 살아 있다 — 남은 카드를 결정하면 재개된다. 마지막 재개의 저장이 죽으면 plan_ids 에 A·B 둘 다
        dead["on"] = True
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "d2", "approved": True}, headers=USER)
        assert r.status_code == 503 and sorted(r.json()["detail"]["plan_ids"]) == sorted([pa.id, pb.id])
    finally:
        restore()
    assert store.active_run(cid).status == "recovery_required"


def test_재개_재시도_경로에서도_저장_실패_안내에_plan_ids가_실린다(client, monkeypatch):
    """자동 리뷰 7차: 앞선 RESUME_RETRYABLE 이 카드 payload 를 error 로 덮은 뒤라 payload 에서 뽑으면 빈 배열이었다."""
    from kukie.store import get_store
    store = get_store()
    original = store.update_run
    dead = {"on": False}

    def flaky(run_id, **fields):
        if dead["on"]:
            raise RuntimeError("db down")
        return original(run_id, **fields)

    monkeypatch.setattr(store, "update_run", flaky)
    cid = _new(client, shared=True)
    plan, ticket = _pending_ticket_for_server("rr")
    restore = _swap_run_agent([_fake(ticket), RuntimeError("네트워크"), _fake(KukieResponse(narration="이번엔 됨"))])
    try:
        client.post(f"/conversations/{cid}/chat", json={"text": "늘려"}, headers=USER)
        r = client.post(f"/conversations/{cid}/approve", json={"call_id": "rr", "approved": True}, headers=USER)
        assert r.json()["detail"]["code"] == "RESUME_RETRYABLE"                 # 카드 payload 가 error 로 덮인다
        dead["on"] = True
        r = client.post(f"/conversations/{cid}/resume", headers=USER)           # 재개는 되는데 저장이 죽는다
        assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
        assert r.json()["detail"]["plan_ids"] == [plan.id]
        dead["on"] = False
    finally:
        restore()


def test_실패_표시도_저장_못_한_running_run은_다음_chat이_닫고_진행한다(client):
    """저장이 완전히 죽었다 살아난 경우: 잠금을 쥔 chat 이 DB 의 running 을 중단으로 닫는다."""
    from kukie.store import get_store
    cid = _new(client)
    get_store().start_run(cid, request_id="orphan", kind="chat", mode="학습", input_text="저장 실패")   # running 으로 방치
    with agent.override(model=_model("살아남")):
        r = client.post(f"/conversations/{cid}/chat", json={"text": "다시"}, headers=USER)
    assert r.status_code == 200
    statuses = [t["status"] for t in client.get(f"/conversations/{cid}", headers=USER).json()["turns"]]
    assert statuses == ["interrupted", "completed"]


def test_복원_한도는_UTF8_바이트_기준이고_최신_run_하나가_넘어도_복원하지_않는다(client, monkeypatch):
    """팀원 리뷰 5: 문자 수로 재면 한글이 작게 잡히고, 첫 run 은 검사를 건너뛰었다."""
    import json
    cid = _new(client)
    text = "한글" * 50
    restore = _swap_run_agent([_fake(KukieResponse(narration="ok"), [ModelRequest(parts=[UserPromptPart(content=text)])])])
    try:
        client.post(f"/conversations/{cid}/chat", json={"text": "질문"}, headers=USER)
    finally:
        restore()
    stored = client.get(f"/conversations/{cid}", headers=USER)
    conversations.registry.clear()
    client.get(f"/conversations/{cid}", headers=USER)
    messages = conversations.registry.get(cid).session.history
    assert len(messages) == 1
    from kukie.store import get_store
    raw = get_store().list_runs(cid)[0].agent_messages
    chars, size = len(json.dumps(raw, ensure_ascii=False)), len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    assert size > chars                                                   # 한글은 바이트가 더 크다
    monkeypatch.setattr(conversations, "HISTORY_MAX_BYTES", size - 1)   # 문자 수로 재면 통과했을 값
    conversations.registry.clear()
    client.get(f"/conversations/{cid}", headers=USER)
    assert conversations.registry.get(cid).session.history == []
    assert stored.status_code == 200


def test_첫_요청_여럿이_동시에_와도_DB_초기화가_충돌하지_않는다(tmp_path, monkeypatch):
    """팀원 리뷰 4: 동기 dependency 는 스레드풀에서 돌아 create_all 이 동시에 들어올 수 있다."""
    import threading
    from kukie.store import db as store_db
    monkeypatch.setenv("KUKIE_DATABASE_URL", f"sqlite:///{tmp_path / 'race.db'}")
    monkeypatch.setattr(store_db, "_engine", None)
    monkeypatch.setattr(store_db, "_factory", None)
    monkeypatch.setattr(store_db, "_store", None)
    stores, errors = [], []

    def call():
        try:
            stores.append(store_db.get_store())
        except Exception as exc:                                          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len({id(s) for s in stores}) == 1
    reset_store_for_tests(f"sqlite:///{tmp_path / 'after.db'}")           # 다른 테스트가 쓰는 전역을 되돌린다


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
