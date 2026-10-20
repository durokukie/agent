"""대화(채팅방) 단위 엔드포인트 — Kukie API 서버 기록·멱등성·잠금·복원·권한 (DURO-113).

LLM 은 TestModel / _run_agent 바꿔치기, kubectl 은 가짜, Kukie API 서버는 메모리 가짜(tests/fake_server.py).
채팅방 만들기·목록·조회는 server 공개 API 라 여기 없다 — 방은 가짜 server 에 직접 놓고, 기록은 가짜 server 에서 읽는다.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from helpers import OTHER, USER, answer_model, chat_model, new_room, scale_call, sync, waiting_plan
from kukie_agent import conversations, server, server_api
from kukie_agent.agent import agent
from kukie_agent.auth import User
from kukie_agent.server_api import ServerError
from kukie_agent.skills.base import KukieResponse


@pytest.fixture
def client(dev_auth, fake_server, kubectl_ok, plan_dir):
    return TestClient(server.app)


def _say(client, room: str, text: str, headers=USER, **body):
    return client.post(f"/conversations/{room}/chat", json={"text": text, **body}, headers=headers)


def _reopen(room: str, user: str = "u-1"):
    """프로세스 재시작 흉내 — 메모리를 비우고 Kukie API 서버의 컨텍스트로 다시 복원한다."""
    conversations.registry.clear()

    async def go():
        context = await server_api.conversation_context(room, User(id=user), purpose="chat")
        conversation, _ = await conversations.registry.get_or_restore(context)
        return conversation

    return sync(go())


def _fake(output, new_messages=()):
    msgs = list(new_messages)
    return SimpleNamespace(output=output, all_messages=lambda: msgs, new_messages=lambda: msgs)


def _card(fake, *call_ids: str, new_messages=()):
    """승인 카드 — 가짜 _run_agent 가 불릴 때(= run 이 생긴 뒤) 그 run 에 대기 중인 Plan 을 server 에 놓는다."""
    def produce(session):
        calls = [scale_call(cid) for cid in call_ids]
        metadata = {cid: {"plan_id": waiting_plan(fake, session.deps.run_id, cid)} for cid in call_ids}
        return _fake(DeferredToolRequests(approvals=calls, metadata=metadata), new_messages)
    return produce


def _swap_run_agent(outputs):
    """_run_agent 를 outputs 순서대로 돌려주는 가짜로. 반환은 원복 함수. 함수면 세션을 받아 결과를 만든다."""
    original = server._run_agent
    queue = list(outputs)

    async def fake(session, **kw):
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value(session) if callable(value) else value

    server._run_agent = fake
    return lambda: setattr(server, "_run_agent", original)


def _break_update_run(monkeypatch, when):
    """server_api.update_run 이 when(fields) 가 참일 때 실패한다 (저장 장애 흉내)."""
    original = server_api.update_run

    async def flaky(run_id, **fields):
        if when(fields):
            raise ServerError(503, "SERVER_UNAVAILABLE", "server down")
        return await original(run_id, **fields)

    monkeypatch.setattr(server_api, "update_run", flaky)


def _statuses(fake, room):
    return [t["status"] for t in fake.turns(room)]


# ── 인증 · 권한 ───────────────────────────────────────────

def test_인증_없으면_401(client, fake_server):
    room = new_room(fake_server)
    r = client.post(f"/conversations/{room}/chat", json={"text": "x"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "UNAUTHORIZED"


def test_server가_설정되면_개발용_헤더와_변수는_무시된다(client, fake_server, monkeypatch):
    monkeypatch.setenv("KUKIE_SERVER_URL", "http://server.invalid")
    monkeypatch.setenv("KUKIE_DEV_USER", "dev")
    r = _say(client, new_room(fake_server), "x")       # X-User 만 있고 Bearer 없음
    assert r.status_code == 401 and r.json()["detail"]["code"] == "UNAUTHORIZED"


def test_server도_개발_모드도_없으면_503_AUTH_NOT_CONFIGURED(client, fake_server, monkeypatch):
    monkeypatch.delenv("KUKIE_DEV_AUTH")
    r = _say(client, new_room(fake_server), "x")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "AUTH_NOT_CONFIGURED"


def test_server_설정이_없으면_대화_경로는_503으로_닫힌다(dev_auth, kubectl_ok, plan_dir):
    """agent 는 DB 가 없다 — server 없이 대화를 돌리면 어디에도 남지 않으므로 fail-closed (가짜 server 없이)."""
    conversations.registry.clear()
    r = TestClient(server.app).post("/conversations/c-1/chat", json={"text": "x"}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SERVER_NOT_CONFIGURED"


def test_남의_private_방은_server가_404로_답하고_그대로_나간다(client, fake_server):
    room = new_room(fake_server, user="u-2")
    r = _say(client, room, "x")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"
    assert fake_server.runs_of(room) == []


def test_server에_닿지_못하면_503_SERVER_UNAVAILABLE(client, fake_server, monkeypatch):
    async def down(*a, **k):
        raise ServerError(503, "SERVER_UNAVAILABLE", "Kukie API 서버에 연결할 수 없다")

    monkeypatch.setattr(server_api, "conversation_context", down)
    r = _say(client, new_room(fake_server), "x")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SERVER_UNAVAILABLE"


def test_팀_shared_방은_구성원이_입력하고_승인은_Admin이나_주인만이다(client, fake_server):
    """권한 판단은 server 몫이다 — agent 는 context(purpose=approve) 의 403 을 그대로 준다."""
    fake_server.add_member("t-1", "u-1", "MEMBER")
    fake_server.add_member("t-1", "u-2", "MEMBER")
    cluster = fake_server.add_cluster(owner="u-2", team_id="t-1")
    room = new_room(fake_server, user="u-2", shared=True, cluster_id=cluster)
    restore = _swap_run_agent([_card(fake_server, "call-s"), _fake(KukieResponse(narration="주인이 승인해서 적용"))])
    try:
        r = _say(client, room, "늘려")                                         # 주인 아님 — 입력은 된다
        assert r.status_code == 200 and r.json()["kind"] == "approval"
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-s", "approved": True}, headers=USER)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_TEAM_ADMIN"
        assert client.post(f"/conversations/{room}/resume", headers=USER).status_code == 403
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-s", "approved": True}, headers=OTHER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "주인이 승인해서 적용"
    finally:
        restore()


def test_팀에서_나가면_내_방은_열려도_그_클러스터로_실행은_못_하고_run도_안_남는다(client, fake_server):
    """읽기는 열고 실행은 닫는다 — 클러스터 권한은 연결 정보 요청에서 server 가 본다 (잠금·run 시작보다 먼저)."""
    cluster = fake_server.add_cluster(owner="u-1", team_id="t-1")       # u-1 은 t-1 구성원이 아니다
    room = new_room(fake_server, cluster_id=cluster)
    r = _say(client, room, "파드", request_id="req-1")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"
    assert fake_server.runs_of(room) == []                                # run 이 안 남는다
    again = _say(client, room, "파드", request_id="req-1")
    assert again.status_code == 404                                       # 재시도도 같은 안내 (409 INTERRUPTED 가 아니다)


def test_클러스터의_접속_대상이_바뀌면_CLUSTER_CHANGED(client, fake_server):
    """방이 만들 때 복사한 지문을 함께 보내 server 가 대조한다 — "승인한 대상 = 실행 대상"."""
    room = new_room(fake_server)
    fake_server.clusters[fake_server.conversation(room)["clusterId"]]["fingerprint"] = "다른-지문"
    r = _say(client, room, "파드")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CLUSTER_CHANGED"
    assert fake_server.runs_of(room) == []


def test_등록된_클러스터로_대화하면_임시_kubeconfig로_실행하고_끝나면_지운다(client, fake_server, monkeypatch):
    from kukie_agent.tools import read as read_tools
    seen = {}

    def run(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        from kukie_agent.kubectl import KubectlResult
        seen["context"], seen["path"] = context, kubeconfig
        seen["body"] = kubeconfig.read_text() if kubeconfig else None
        return KubectlResult(command="kubectl get pods", stdout="nginx Running", stderr="", success=True)

    monkeypatch.setattr(read_tools, "run_kubectl", run)
    cluster = fake_server.add_cluster(context="kind-team", credential={"token": "secret-token"})
    room = new_room(fake_server, cluster_id=cluster)
    with agent.override(model=chat_model()):
        assert _say(client, room, "파드").status_code == 200
    assert seen["context"] == "kind-team"
    assert "secret-token" in seen["body"] and "https://cluster.example" in seen["body"]
    assert not seen["path"].exists()                                         # 블록을 벗어나면 사라진다
    assert conversations.registry.get(room).session.deps.kubeconfig is None
    call = next(c for name, c in fake_server.calls if name == "cluster_connection")
    assert call["expectedFingerprint"] == fake_server.conversation(room)["clusterFingerprint"]


def test_스킬은_방의_공개_범위로만_정한다(fake_server):
    """DURO-100: 모드는 없다. private 방은 Private, shared 방은 Shared 로 연다."""
    assert _reopen(new_room(fake_server)).session.skill.name == "Private"
    assert _reopen(new_room(fake_server, shared=True)).session.skill.name == "Shared"


def test_private_방은_승인이_막힌다(client, fake_server):
    """기획 05 §3: Private 는 조회만, 실제 변경은 불가. 변경 툴이 아예 없고, 승인·재개도 server 가 403 으로 막는다."""
    room = new_room(fake_server)                                            # shared=False
    r = client.post(f"/conversations/{room}/approve", json={"call_id": "c", "approved": True}, headers=USER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "PRIVATE_SESSION"
    assert client.post(f"/conversations/{room}/resume", headers=USER).status_code == 403


def _capture_turns(seen: list[dict]):
    """모델이 받은 툴 목록·지시문·응답 스키마를 턴마다 적는 FunctionModel."""
    def capture(messages, info):
        seen.append({
            "tools": [(t.name, t.parameters_json_schema) for t in info.function_tools],
            "output": [(t.name, t.parameters_json_schema) for t in info.output_tools],
            # @agent.instructions 의 결과는 ModelRequest.instructions 로 온다 (parts 가 아니다)
            "instructions": messages[-1].instructions,
        })
        return ModelResponse(parts=[ToolCallPart(
            tool_name=info.output_tools[0].name,
            args={"narration": "답변입니다.", "suggested_next_action": None},
        )])
    return FunctionModel(capture)


@pytest.mark.parametrize("shared,expected", [
    (False, set()),
    (True, {"apply_manifest", "scale_resource", "rollout_restart", "delete_resource"}),
])
def test_방의_공개_범위가_노출되는_툴을_정한다(client, fake_server, shared, expected):
    """Private 는 읽기 5종만, Shared 는 읽기 + 변경 4종 (DURO-100)."""
    seen: list[dict] = []
    room = new_room(fake_server, shared=shared)
    with agent.override(model=_capture_turns(seen)):
        assert _say(client, room, "파드 보여줘").status_code == 200
    read = {"list_resources", "explain_command", "describe_resource", "get_events", "get_logs"}
    assert {name for name, _ in seen[-1]["tools"]} == read | expected


@pytest.mark.parametrize("shared", [False, True])
def test_한_대화의_툴_목록과_지시문은_턴마다_같다(client, fake_server, shared):
    """프롬프트 캐시가 맞으려면 대화 안에서 앞부분(툴·지시문·응답 스키마)이 그대로여야 한다 (DURO-100)."""
    seen: list[dict] = []
    room = new_room(fake_server, shared=shared)
    with agent.override(model=_capture_turns(seen)):
        _say(client, room, "안녕")
        _say(client, room, "/mode 실습")                    # 옛 앱의 모드 버튼 — 아무것도 바꾸지 않는다
        _say(client, room, "nginx 를 늘려줘")
    assert len(seen) == 2 and seen[0] == seen[1]
    assert seen[0]["instructions"]


# ── 채팅 · run 기록 ────────────────────────────────────────

def test_채팅은_answer를_주고_run으로_남는다(client, fake_server):
    room = new_room(fake_server)
    with agent.override(model=chat_model("파드 하나")):
        r = _say(client, room, "파드 보여줘")
    assert r.status_code == 200 and r.json()["kind"] == "answer"

    turns = fake_server.turns(room)
    assert len(turns) == 1
    turn = turns[0]
    assert turn["seq"] == 1 and turn["kind"] == "chat" and turn["status"] == "completed"
    assert turn["input_text"] == "파드 보여줘"
    assert turn["payload"]["response"]["narration"] == "파드 하나"
    assert turn["finished_at"] is not None
    assert fake_server.runs_of(room)[0]["agentMessages"]                  # 복원에 쓸 이 run 의 메시지


def test_같은_request_id_재전송은_실행_없이_저장된_응답을_준다(client, fake_server):
    room = new_room(fake_server)
    calls = 0

    async def fake_run(session, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(output=KukieResponse(narration="한 번만"), all_messages=lambda: [],
                               new_messages=lambda: [])

    original = server._run_agent
    server._run_agent = fake_run
    try:
        first = _say(client, room, "파드", request_id="req-1").json()
        second = _say(client, room, "파드", request_id="req-1").json()
    finally:
        server._run_agent = original
    assert first == second and calls == 1
    assert len(fake_server.turns(room)) == 1


def test_같은_request_id에_다른_입력은_409_REQUEST_MISMATCH(client, fake_server):
    room = new_room(fake_server)
    with agent.override(model=chat_model()):
        _say(client, room, "하나", request_id="r")
        r = _say(client, room, "둘", request_id="r")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "REQUEST_MISMATCH"


def test_mode_입력은_LLM_없이_run으로만_남고_스킬은_그대로다(client, fake_server, monkeypatch):
    """모드는 없어졌다 (DURO-100). 옛 앱이 보내는 "/mode …" 는 known=False 로만 답하고 run 종류는 예전대로 남긴다."""
    room = new_room(fake_server)

    async def no_llm(*args, **kwargs):
        raise AssertionError("/mode 는 LLM 을 부르지 않는다")
    monkeypatch.setattr(server, "_run_agent", no_llm)
    r = _say(client, room, "/mode 실습")                     # private 방에서도 403 이 아니다 — 바꾸는 게 없다
    assert r.status_code == 200
    assert r.json() == {"kind": "mode", "skill": "Private", "known": False}
    assert conversations.registry.get(room).session.skill.name == "Private"
    turn = fake_server.turns(room)[0]
    assert turn["kind"] == "mode_change" and turn["payload"]["kind"] == "mode"


def test_모델_예외는_run을_failed로_남기고_500_RUN_FAILED(client, fake_server):
    room = new_room(fake_server)

    async def boom(session, **kwargs):
        raise RuntimeError("모델 죽음")

    original = server._run_agent
    server._run_agent = boom
    try:
        r = _say(client, room, "파드")
    finally:
        server._run_agent = original
    assert r.status_code == 500 and r.json()["detail"]["code"] == "RUN_FAILED"
    turn = fake_server.turns(room)[0]
    assert turn["status"] == "failed" and turn["error"]["code"] == "RUN_FAILED"
    # 세션은 살아 있다 — 다음 채팅이 된다
    with agent.override(model=chat_model("다시")):
        assert _say(client, room, "다시").status_code == 200


def test_실패한_요청을_같은_request_id로_다시_보내면_저장된_실패를_준다(client, fake_server):
    room = new_room(fake_server)
    calls = 0

    async def boom(session, **kw):
        nonlocal calls
        calls += 1
        raise RuntimeError("모델 죽음")

    original = server._run_agent
    server._run_agent = boom
    try:
        first = _say(client, room, "파드", request_id="req-fail")
        second = _say(client, room, "파드", request_id="req-fail")
    finally:
        server._run_agent = original
    assert first.status_code == 500 and second.status_code == 500       # BUSY 가 아니라 같은 실패
    assert second.json()["detail"]["code"] == "RUN_FAILED" and calls == 1
    assert "모델 죽음" not in first.json()["detail"]["message"]         # 예외 문자열은 응답에 안 싣는다
    # 새 request_id 면 다시 실행한다
    with agent.override(model=chat_model("살아남")):
        assert _say(client, room, "파드").status_code == 200


def test_chat은_첫_마디를_제목_후보로_보내고_mode_입력은_보내지_않는다(client, fake_server):
    """제목을 지을지(기본 제목인지, 첫 마디인지, 30자)는 server 가 정한다 — agent 는 원문을 run 기록과 함께 보낸다 (#59)."""
    room = new_room(fake_server, shared=True)
    _say(client, room, "/mode 실습")
    with agent.override(model=answer_model("답변입니다.")):
        _say(client, room, "nginx 를 4개로 늘려줘")
        _say(client, room, "두 번째 질문")
    sent = [c.get("title_from") for name, c in fake_server.calls if name == "update_run"]
    assert sent == [None, "nginx 를 4개로 늘려줘", "두 번째 질문"]
    assert fake_server.conversation(room)["title"] == "nginx 를 4개로 늘려줘"   # 한 번만 정해진다


# ── 채팅방 독립성 · 복원 ───────────────────────────────────

def test_채팅방끼리_기록과_스킬이_섞이지_않는다(client, fake_server):
    a, b = new_room(fake_server, shared=True), new_room(fake_server)
    with agent.override(model=chat_model()):
        _say(client, a, "파드")
        _say(client, b, "파드")
    assert conversations.registry.get(a).session.skill.name == "Shared"
    assert conversations.registry.get(b).session.skill.name == "Private"
    assert len(fake_server.turns(a)) == 1 and len(fake_server.turns(b)) == 1


def test_서버가_다시_떠도_history가_복원된다(client, fake_server):
    room = new_room(fake_server)
    with agent.override(model=chat_model("첫 답")):
        _say(client, room, "첫 질문")
    live_history = len(conversations.registry.get(room).session.history)
    assert live_history > 0

    restored = _reopen(room)                         # 프로세스 재시작 흉내 — 메모리만 비운다
    assert len(restored.session.history) == live_history
    assert restored.session.skill.name == "Private" and restored.session.pending is None


def test_복원_한도는_UTF8_바이트_기준이고_최신_run_하나가_넘어도_복원하지_않는다(client, fake_server, monkeypatch):
    """팀원 리뷰 5: 문자 수로 재면 한글이 작게 잡히고, 첫 run 은 검사를 건너뛰었다."""
    room = new_room(fake_server)
    text = "한글" * 50
    restore = _swap_run_agent([_fake(KukieResponse(narration="ok"), [ModelRequest(parts=[UserPromptPart(content=text)])])])
    try:
        _say(client, room, "질문")
    finally:
        restore()
    assert len(_reopen(room).session.history) == 1
    raw = fake_server.runs_of(room)[0]["agentMessages"]
    chars, size = len(json.dumps(raw, ensure_ascii=False)), len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    assert size > chars                                                   # 한글은 바이트가 더 크다
    monkeypatch.setattr(conversations, "HISTORY_MAX_BYTES", size - 1)   # 문자 수로 재면 통과했을 값
    assert _reopen(room).session.history == []


def test_실행_중_죽은_run은_처음_열_때_interrupted가_된다(client, fake_server):
    """기동 때 interrupt-orphaned 가 놓쳤어도(실패·경합) 방을 열 때 닫는다."""
    room = new_room(fake_server)
    fake_server.put_run(room, request_id="crash", input_text="죽기 직전")   # 끝내지 않음
    _reopen(room)
    assert _statuses(fake_server, room) == ["interrupted"]
    assert fake_server.running(room) is False
    with agent.override(model=chat_model("살아남")):
        assert _say(client, room, "다시").status_code == 200


def test_기동하면_전_프로세스가_돌리던_run을_닫는다(fake_server, monkeypatch):
    """lifespan → POST /internal/runs/interrupt-orphaned. 승인 대기는 그대로 둔다 (방을 열 때 카드를 되살린다)."""
    monkeypatch.setenv("KUKIE_SERVER_URL", "http://server.test")
    monkeypatch.setenv("KUKIE_INTERNAL_TOKEN", "internal")
    room = new_room(fake_server, shared=True)
    fake_server.put_run(room, request_id="crash")
    other = new_room(fake_server, shared=True)
    fake_server.put_run(other, request_id="card", status="awaiting_approval")
    with TestClient(server.app):
        pass
    assert _statuses(fake_server, room) == ["interrupted"]
    assert _statuses(fake_server, other) == ["awaiting_approval"]


def test_기동_정리가_실패해도_서버는_뜬다(fake_server, monkeypatch, caplog):
    monkeypatch.setenv("KUKIE_SERVER_URL", "http://server.test")
    monkeypatch.setenv("KUKIE_INTERNAL_TOKEN", "internal")

    async def down(message):
        raise ServerError(503, "SERVER_UNAVAILABLE", "down")

    monkeypatch.setattr(server_api, "interrupt_orphaned", down)
    with TestClient(server.app) as c:
        assert c.get("/health").status_code == 200
    assert "interrupt-orphaned" in caplog.text


# ── 저장 실패 ──────────────────────────────────────────────

def test_결과_저장이_실패해도_방이_영구_BUSY로_남지_않는다(client, fake_server, monkeypatch):
    """팀원 리뷰 3: 실행 뒤 저장이 실패하면 server 의 run 이 running 으로 남아 다음 요청이 계속 409 였다."""
    failed = {"n": 0}

    def once(fields):
        if fields.get("status") == "completed" and failed["n"] == 0:
            failed["n"] += 1
            return True
        return False

    _break_update_run(monkeypatch, once)
    room = new_room(fake_server)
    with agent.override(model=chat_model("첫 답")):
        r = _say(client, room, "파드")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
    assert "새 요청" in r.json()["detail"]["message"]                             # 같은 request_id 로는 같은 실패가 재생된다
    assert _statuses(fake_server, room) == ["failed"]
    with agent.override(model=chat_model("둘째 답")):
        r = _say(client, room, "파드")
    assert r.status_code == 200 and r.json()["response"]["narration"] == "둘째 답"


def test_저장_실패한_run을_같은_request_id로_재전송해도_영원히_BUSY가_아니다(client, fake_server):
    """자동 리뷰 6차: 재전송 판정이 밀린 run 정리보다 앞이라, 같은 request_id 재시도가 저장된 실패·BUSY 를 영원히 재생했다."""
    room = new_room(fake_server)
    _reopen(room)                                                         # 이 프로세스가 연 방이다 (재시작 복원이 아니다)
    fake_server.put_run(room, request_id="r-stuck", input_text="파드")   # 대체 쓰기까지 실패해 running 으로 남은 run
    r = _say(client, room, "파드", request_id="r-stuck")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INTERRUPTED"      # BUSY 가 아니라 "새 요청으로"
    assert "새 요청" in r.json()["detail"]["message"]
    with agent.override(model=chat_model("살아남")):
        assert _say(client, room, "파드").status_code == 200


def test_재전송이_남은_run을_닫는_동안은_방을_잠근다(client, fake_server, monkeypatch):
    """interrupt 는 "이 방의 활성 run" 을 닫는다 — 잠금 없이 부르면 그 사이 새 chat 이 연 run 을 늦게 도착한 interrupt 가 닫는다."""
    room = new_room(fake_server)
    _reopen(room)
    fake_server.put_run(room, request_id="r-stuck", input_text="파드")
    original = server_api.interrupt
    held: list[bool] = []

    async def watching(conversation_id, message):
        held.append(conversations.registry.get(conversation_id).lock.locked())
        return await original(conversation_id, message)

    monkeypatch.setattr(server_api, "interrupt", watching)
    r = _say(client, room, "파드", request_id="r-stuck")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INTERRUPTED"
    assert held == [True]


def test_실패_표시도_저장_못_한_running_run은_다음_chat이_닫고_진행한다(client, fake_server):
    """저장이 완전히 죽었다 살아난 경우: 잠금을 쥔 chat 이 server 의 running 을 중단으로 닫는다."""
    room = new_room(fake_server)
    with agent.override(model=chat_model("먼저")):
        _say(client, room, "방을 연다")                                   # 메모리에 방이 떠 있는 상태에서
    fake_server.put_run(room, request_id="orphan", input_text="저장 실패")    # running 으로 방치
    with agent.override(model=chat_model("살아남")):
        r = _say(client, room, "다시")
    assert r.status_code == 200
    assert _statuses(fake_server, room) == ["completed", "interrupted", "completed"]


def test_승인_뒤_저장이_죽으면_recovery_required와_plan_ids로_남고_다음_chat이_방을_풀어준다(client, fake_server, monkeypatch):
    """자동 리뷰 6차: kubectl 이 이미 돈 run 을 failed 로 적고 "다시 보내라" 하면 같은 변경을 또 시킬 수 있다.
    저장이 두 번 다 죽어 awaiting_approval 이 남아도 다음 chat 이 닫아야 한다."""
    dead = {"on": False}
    _break_update_run(monkeypatch, lambda fields: dead["on"])
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "call-st"), _fake(KukieResponse(narration="적용됨"))])
    try:
        assert _say(client, room, "늘려").json()["kind"] == "approval"
        dead["on"] = True                                                         # 승인 결과 저장부터 server 가 죽는다
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-st", "approved": True}, headers=USER)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
        assert r.json()["detail"]["plan_ids"] == [fake_server.plan_by_call("call-st")["id"]]   # 확인할 Plan 을 알려 준다
        assert "다시 보내지 말고" in r.json()["detail"]["message"]
        dead["on"] = False                                                        # 복구. run 은 awaiting_approval 그대로, 메모리 티켓은 없음
        assert fake_server.active_run_of(room)["status"] == "awaiting_approval"
    finally:
        restore()
    with agent.override(model=chat_model("풀렸다")):
        r = _say(client, room, "다음")
    assert r.status_code == 200 and r.json()["response"]["narration"] == "풀렸다"
    assert _statuses(fake_server, room) == ["interrupted", "completed"]


def test_남은_카드_재전송에서_저장이_죽어도_확인_필요가_아니라_카드_대기_그대로다(client, fake_server, monkeypatch):
    """자동 리뷰 7차: 아무것도 실행하지 않은 경로(result 없음)에 recovery_required + "변경 적용됐을 수 있다" 를 적으면 사실 칸 오염."""
    dead = {"on": False}

    def once(fields):
        if dead["on"]:
            dead["on"] = False                                                   # 한 번만 실패 — 대체 쓰기는 성공
            return True
        return False

    _break_update_run(monkeypatch, once)
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "d1", "d2"), _fake(KukieResponse(narration="둘 다 적용"))])
    try:
        _say(client, room, "둘")
        dead["on"] = True                                                        # 본 쓰기만 죽고 대체 쓰기는 산다
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "d1", "approved": True}, headers=USER)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
        assert "남은 카드를 계속 결정" in r.json()["detail"]["message"]          # "변경 적용됐을 수 있다" 가 아니다
        assert "plan_ids" not in r.json()["detail"]
        assert fake_server.active_run_of(room)["status"] == "awaiting_approval"   # 확인 필요가 아니라 카드 대기 그대로
        assert fake_server.turns(room)[0]["payload"]["kind"] == "approval"       # 계속할 재료(카드)가 기록에 남는다 (8차)
        # 티켓은 살아 있다 — 남은 카드를 결정하면 재개된다. 마지막 재개의 저장이 죽으면 plan_ids 에 둘 다
        dead["on"] = True
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "d2", "approved": True}, headers=USER)
        assert r.status_code == 503 and sorted(r.json()["detail"]["plan_ids"]) == sorted(
            [fake_server.plan_by_call("d1")["id"], fake_server.plan_by_call("d2")["id"]])
    finally:
        restore()
    assert fake_server.active_run_of(room)["status"] == "recovery_required"


def test_거절한_카드의_Plan은_확인_목록에_들어가지_않는다(client, fake_server, monkeypatch):
    """자동 리뷰 8차: 티켓 전체에서 뽑으면 거절해서 실행된 적 없는 Plan 까지 "적용됐을 수 있다" 목록에 섞인다."""
    dead = {"on": False}

    def once(fields):
        if dead["on"]:
            dead["on"] = False
            return True
        return False

    _break_update_run(monkeypatch, once)
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "k1", "k2"), _fake(KukieResponse(narration="하나만 적용"))])
    try:
        _say(client, room, "둘")
        client.post(f"/conversations/{room}/approve", json={"call_id": "k2", "approved": False}, headers=USER)   # B 거절
        assert fake_server.plan_by_call("k2")["status"] == "REJECTED"                                          # server 에 기록
        dead["on"] = True
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "k1", "approved": True}, headers=USER)  # A 승인 → 재개
    finally:
        restore()
    assert r.status_code == 503 and r.json()["detail"]["plan_ids"] == [fake_server.plan_by_call("k1")["id"]]


def test_재개_재시도_경로에서도_저장_실패_안내에_plan_ids가_실린다(client, fake_server, monkeypatch):
    """자동 리뷰 7차: 앞선 RESUME_RETRYABLE 이 카드 payload 를 error 로 덮은 뒤라 payload 에서 뽑으면 빈 배열이었다."""
    dead = {"on": False}
    _break_update_run(monkeypatch, lambda fields: dead["on"])
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "rr"), RuntimeError("네트워크"), _fake(KukieResponse(narration="이번엔 됨"))])
    try:
        _say(client, room, "늘려")
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "rr", "approved": True}, headers=USER)
        assert r.json()["detail"]["code"] == "RESUME_RETRYABLE"                 # 카드 payload 가 error 로 덮인다
        dead["on"] = True
        r = client.post(f"/conversations/{room}/resume", headers=USER)           # 재개는 되는데 저장이 죽는다
        assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
        assert r.json()["detail"]["plan_ids"] == [fake_server.plan_by_call("rr")["id"]]
        dead["on"] = False
    finally:
        restore()


def test_최초_승인_요청_저장만_실패해도_방을_계속_쓸_수_있다(client, fake_server, monkeypatch):
    """팀원 리뷰: 카드를 만든 직후 awaiting_approval 저장만 실패하면 메모리와 server 가 갈린다.

    카드가 기록에 안 남았으므로 그 승인은 이어갈 수 없다. 티켓을 버려 server 와 맞추고, 안내대로 새 요청을 받는다.
    """
    dead = {"on": False}
    _break_update_run(monkeypatch, lambda fields: dead["on"] and fields.get("status") == "awaiting_approval")
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "first"), _fake(KukieResponse(narration="다시 됨"))])
    try:
        dead["on"] = True
        r = _say(client, room, "늘려")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "STORE_FAILED"
        dead["on"] = False
        assert _statuses(fake_server, room) == ["failed"]                     # server 는 닫혔고
        assert fake_server.plan_by_call("first")["status"] == "EXPIRED"       # 계획도 같이 닫혔고
        assert conversations.registry.get(room).session.pending is None      # 메모리도 같이 비었다

        r = _say(client, room, "다시")                                         # 안내대로 새 요청을 보내면 방이 그대로 산다
        assert r.status_code == 200, r.json()
        assert r.json()["response"]["narration"] == "다시 됨"
    finally:
        restore()
    assert _statuses(fake_server, room) == ["failed", "completed"]


def test_저장_실패로_닫힌_카드는_approve_도_resume_도_받지_않는다(client, fake_server, monkeypatch):
    """티켓을 버렸으니 둘 다 "대기 중인 승인이 없다" 가 나와야 한다 — 서로 다른 409 로 엇갈리면 안 된다."""
    dead = {"on": False}
    _break_update_run(monkeypatch, lambda fields: dead["on"] and fields.get("status") == "awaiting_approval")
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "gone")])
    try:
        dead["on"] = True
        _say(client, room, "늘려")
        dead["on"] = False
    finally:
        restore()
    r = client.post(f"/conversations/{room}/approve", json={"call_id": "gone", "approved": True}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"
    r = client.post(f"/conversations/{room}/resume", headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


def test_저장_실패로_버린_카드의_기록도_history에서_뺀다(client, fake_server, monkeypatch):
    """자동 리뷰 10차: 티켓만 버리고 history 를 두면 결과 없는 tool call 로 끝난 기록이 남는다."""
    dead = {"on": False}
    _break_update_run(monkeypatch, lambda fields: dead["on"] and fields.get("status") == "awaiting_approval")
    room = new_room(fake_server, shared=True)
    첫마디 = [ModelRequest(parts=[UserPromptPart(content="nginx 3개로")])]
    restore = _swap_run_agent([_card(fake_server, "hist", new_messages=첫마디),
                               _fake(KukieResponse(narration="다시 됨"), 첫마디)])
    try:
        dead["on"] = True
        _say(client, room, "nginx 3개로")
        dead["on"] = False
        assert conversations.registry.get(room).session.history == []    # 버린 턴의 메시지도 버린다
        r = _say(client, room, "다시")
        assert r.status_code == 200 and r.json()["response"]["narration"] == "다시 됨"
    finally:
        restore()


# ── 승인 · 재개 ────────────────────────────────────────────

def test_chat이_승인_카드를_주면_approve가_같은_run을_이어서_끝내고_다음_chat이_된다(client, fake_server):
    """DB 문서 7절: 승인만으로 새 run 을 만들지 않는다 — 카드 run(awaiting_approval)을 이어서 completed 로."""
    room = new_room(fake_server, shared=True)
    card_msgs = [ModelRequest(parts=[UserPromptPart(content="nginx 3개로")])]
    resume_msgs = [ModelRequest(parts=[UserPromptPart(content="(tool 결과)")])]
    restore = _swap_run_agent([_card(fake_server, "call-f", new_messages=card_msgs),
                               _fake(KukieResponse(narration="적용됨"), resume_msgs),
                               _fake(KukieResponse(narration="다음 턴"))])
    try:
        r = _say(client, room, "nginx 3개로")
        assert r.status_code == 200 and r.json()["kind"] == "approval"
        assert r.json()["approvals"][0]["plan_id"] == fake_server.plan_by_call("call-f")["id"]   # server 의 plan id
        turn = fake_server.turns(room)[0]
        assert turn["status"] == "awaiting_approval" and turn["finished_at"] is None
        assert fake_server.running(room) is False                       # 대기는 "실행 중" 이 아니다
        assert fake_server.pending(room) == ["call-f"]
        r = _say(client, room, "x")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PENDING_APPROVAL"   # 활성 run 하나

        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-f", "approved": True}, headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "적용됨"
        turns = fake_server.turns(room)
        assert [(t["kind"], t["status"]) for t in turns] == [("chat", "completed")]   # run 은 여전히 하나
        assert turns[0]["payload"]["kind"] == "answer" and turns[0]["finished_at"] is not None
        assert conversations.registry.get(room).session.pending is None

        # 재시작해도 카드 메시지 + 재개 메시지가 한 run 에 이어져 있어 history 가 온전하다
        assert len(_reopen(room).session.history) == 2

        r = _say(client, room, "다음")
        assert r.status_code == 200 and r.json()["response"]["narration"] == "다음 턴"
        assert len(fake_server.turns(room)) == 2
    finally:
        restore()


def test_재개_뒤_새_카드가_나와도_그_사이_메시지가_run에_쌓인다(client, fake_server):
    """팀원 리뷰 2: 첫 승인 → 실행 → 둘째 카드 → 승인 → 완료. 복원하면 첫 작업 결과와 둘째 호출이 빠지면 안 된다."""
    room = new_room(fake_server, shared=True)
    m = lambda text: [ModelRequest(parts=[UserPromptPart(content=text)])]   # noqa: E731
    restore = _swap_run_agent([_card(fake_server, "c1", new_messages=m("카드1")),
                               _card(fake_server, "c2", new_messages=m("결과1+호출2")),
                               _fake(KukieResponse(narration="끝"), m("결과2+답"))])
    try:
        assert _say(client, room, "둘").json()["kind"] == "approval"
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "c1", "approved": True}, headers=USER)
        assert r.json()["kind"] == "approval"                                # 실제 재개가 돌아 새 카드가 나왔다
        assert [c["tool_call_id"] for c in r.json()["approvals"]] == ["c2"]
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "c2", "approved": True}, headers=USER)
        assert r.json()["response"]["narration"] == "끝"
    finally:
        restore()
    assert _statuses(fake_server, room) == ["completed"]
    history = _reopen(room).session.history
    assert [msg.parts[0].content for msg in history] == ["카드1", "결과1+호출2", "결과2+답"]


def test_승인_대기_중_재시작하면_되살릴_수_없는_카드는_만료되고_그_턴의_기록은_history에서_뺀다(client, fake_server):
    """리뷰 P1/🟡: 재시작 뒤 활성 run 이 남아 영구 BUSY, 결과 없는 tool call 이 history 에 남는 문제.
    기록의 답 없는 tool call 과 대기 카드가 어긋나면(여기서는 기록에 tool call 이 없다) 되살리지 않는다."""
    room = new_room(fake_server, shared=True)
    dangling = [ModelRequest(parts=[UserPromptPart(content="nginx 3개로")])]
    restore = _swap_run_agent([_card(fake_server, "call-x", new_messages=dangling),
                               _fake(KukieResponse(narration="재시작 뒤"))])
    try:
        assert _say(client, room, "nginx 3개로").json()["kind"] == "approval"
        restored = _reopen(room)                                         # 프로세스 재시작 흉내

        turn = fake_server.turns(room)[0]
        assert turn["status"] == "interrupted" and turn["error"]["code"] == "INTERRUPTED"
        assert turn["payload"] is None                                   # 만료된 카드는 다시 그리지 않는다
        assert fake_server.pending(room) == [] and fake_server.running(room) is False
        assert fake_server.plan_by_call("call-x")["status"] == "EXPIRED"
        assert restored.session.history == [] and restored.session.pending is None

        r = _say(client, room, "다시")
        assert r.status_code == 200 and r.json()["response"]["narration"] == "재시작 뒤"
    finally:
        restore()


def test_승인_대기는_그_방만_막고_다른_방은_자유다(client, fake_server):
    a, b = new_room(fake_server, shared=True), new_room(fake_server)
    restore = _swap_run_agent([_card(fake_server, "call-a"), _fake(KukieResponse(narration="b 답")),
                               _fake(KukieResponse(narration="적용됨"))])
    try:
        assert _say(client, a, "늘려").json()["kind"] == "approval"
        r = _say(client, a, "x")                                          # a 는 승인 대기 → chat 막힘, b 는 자유
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PENDING_APPROVAL"
        r = _say(client, b, "x")
        assert r.status_code == 200 and r.json()["response"]["narration"] == "b 답"

        r = client.post(f"/conversations/{a}/approve", json={"call_id": "call-a", "approved": True}, headers=USER)
        assert r.status_code == 200 and r.json()["kind"] == "answer"
    finally:
        restore()
    assert [(t["kind"], t["status"]) for t in fake_server.turns(a)] == [("chat", "completed")]
    assert conversations.registry.get(a).session.pending is None


def test_카드가_여러_장이면_마지막_결정까지_run은_열려_있고_payload는_남은_카드다(client, fake_server):
    """문서 7절: 계획이 여러 개면 결정이 모일 때까지 기다린다."""
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "c1", "c2"), _fake(KukieResponse(narration="둘 다 적용"))])
    try:
        assert _say(client, room, "두 개").json()["kind"] == "approval"
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "c1", "approved": True}, headers=USER)
        assert r.json()["kind"] == "approval"                            # 카드 한 장 남음
        turns = fake_server.turns(room)
        assert [t["status"] for t in turns] == ["awaiting_approval"]
        assert [c["tool_call_id"] for c in turns[0]["payload"]["approvals"]] == ["c2"]
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "c2", "approved": True}, headers=USER)
        assert r.json()["response"]["narration"] == "둘 다 적용"
        assert _statuses(fake_server, room) == ["completed"]
    finally:
        restore()


def test_모르는_call_id는_409_NOT_PENDING(client, fake_server):
    room = new_room(fake_server, shared=True)
    r = client.post(f"/conversations/{room}/approve", json={"call_id": "없음", "approved": True}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


def test_재개_실패는_run을_recovery_required로_남기고_resume이_같은_run을_끝낸다(client, fake_server):
    """문서 4절: recovery_required = 실제 변경 결과가 불명확해 확인 필요. DURO-66 의 RESUME_RETRYABLE 과 같다."""
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "call-r"), RuntimeError("네트워크"),
                               _fake(KukieResponse(narration="이번엔 됨"))])
    try:
        assert _say(client, room, "늘려").json()["kind"] == "approval"
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-r", "approved": True}, headers=USER)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "RESUME_RETRYABLE"
        assert conversations.registry.get(room).session.pending is not None       # 티켓 유지
        turns = fake_server.turns(room)
        assert [(t["kind"], t["status"]) for t in turns] == [("chat", "recovery_required")]
        assert turns[0]["error"]["code"] == "RESUME_RETRYABLE" and turns[0]["finished_at"] is None
        r = _say(client, room, "x")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "RECOVERY_REQUIRED"

        r = client.post(f"/conversations/{room}/resume", headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "이번엔 됨"
    finally:
        restore()
    turns = fake_server.turns(room)
    assert [(t["kind"], t["status"]) for t in turns] == [("chat", "completed")]
    assert turns[0]["error"] is None and turns[0]["payload"]["kind"] == "answer"


def test_recovery_required_중_재시작하면_interrupted지만_확인_필요_코드는_남는다(client, fake_server):
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "call-k"), RuntimeError("네트워크")])
    try:
        _say(client, room, "늘려")
        client.post(f"/conversations/{room}/approve", json={"call_id": "call-k", "approved": True}, headers=USER)
    finally:
        restore()
    _reopen(room)
    turn = fake_server.turns(room)[0]
    assert turn["status"] == "interrupted" and turn["error"]["code"] == "RESUME_RETRYABLE"
    assert turn["finished_at"] is not None


def test_응답이_유실된_뒤_같은_request_id로_재전송하면_승인_대기_중이라도_카드를_다시_준다(client, fake_server):
    room = new_room(fake_server, shared=True)
    restore = _swap_run_agent([_card(fake_server, "call-l")])
    try:
        first = _say(client, room, "늘려", request_id="lost").json()
        again = _say(client, room, "늘려", request_id="lost")
    finally:
        restore()
    assert again.status_code == 200 and again.json() == first                   # PENDING_APPROVAL 이 아니라 재생
    conversations.registry.clear()                                               # 재시작 뒤엔 만료된 카드 대신 오류
    again = _say(client, room, "늘려", request_id="lost")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "INTERRUPTED"


@pytest.mark.asyncio
async def test_재전송이_running을_읽은_사이_원래_요청이_카드로_끝나면_닫지_않고_카드를_재생한다(
    dev_auth, fake_server, kubectl_ok, plan_dir, monkeypatch,
):
    """자동 리뷰: 같은 request_id 재전송이 조회에서 running 을 읽고, 그 답을 기다리는 사이 원래 요청이 승인 카드로 끝났다.
    읽은 상태만 믿고 interrupt 하면 카드 run 이 닫혀(EXPIRED) 메모리 티켓만 남는다 — 재시작 전까지 approve 는
    NOT_PENDING, chat·resume 은 PENDING_APPROVAL 로 방이 갇힌다. 조회 뒤의 판단은 그때의 메모리(잠금·티켓)로 한다."""
    room = new_room(fake_server, shared=True)
    running, release, first_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    produce_card = _card(fake_server, "call-race")
    turns = 0

    async def fake_run(session, **kwargs):
        nonlocal turns
        turns += 1
        if turns == 1:
            running.set()
            await release.wait()                     # 재전송이 running 을 읽을 때까지 원래 요청을 붙잡는다
            return produce_card(session)
        return _fake(KukieResponse(narration="적용됨"))

    original_find = server_api.find_run
    lookups = 0

    async def lagging_find(conversation_id, request_id, user):
        nonlocal lookups
        lookups += 1
        stored = await original_find(conversation_id, request_id, user)   # server 는 지금 읽는다 (running)
        if lookups == 2:                             # 재전송의 조회 — 답이 오기 전에 원래 요청이 끝난다
            release.set()
            await first_done.wait()
        return stored

    monkeypatch.setattr(server, "_run_agent", fake_run)
    monkeypatch.setattr(server_api, "find_run", lagging_find)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://agent.test") as http:
        async def say():
            return await http.post(f"/conversations/{room}/chat",
                                   json={"text": "늘려", "request_id": "req-race"}, headers=USER)

        async def original():
            try:
                return await say()
            finally:
                first_done.set()

        first_task = asyncio.create_task(original())
        await asyncio.wait_for(running.wait(), timeout=5)
        again = await asyncio.wait_for(say(), timeout=5)
        first = await first_task

        assert first.status_code == 200 and first.json()["kind"] == "approval"
        assert again.status_code == 200 and again.json() == first.json()          # 닫지 않고 그 카드를 재생한다
        assert _statuses(fake_server, room) == ["awaiting_approval"]
        assert fake_server.plan_by_call("call-race")["status"] == "WAITING_APPROVAL"

        r = await http.post(f"/conversations/{room}/approve",
                            json={"call_id": "call-race", "approved": True}, headers=USER)
        assert r.status_code == 200 and r.json()["response"]["narration"] == "적용됨"
    assert _statuses(fake_server, room) == ["completed"]


def test_재개할_티켓이_없으면_409_NOT_PENDING(client, fake_server):
    room = new_room(fake_server, shared=True)
    r = client.post(f"/conversations/{room}/resume", headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


@pytest.mark.parametrize(("status", "code"), [
    ("awaiting_approval", "PENDING_APPROVAL"),
    ("recovery_required", "RECOVERY_REQUIRED"),
    ("running", "BUSY"),
])
def test_run_시작이_ACTIVE_RUN_EXISTS면_활성_run_상태로_앱이_아는_코드를_준다(client, fake_server, monkeypatch, status, code):
    """server 는 409 ACTIVE_RUN_EXISTS 하나만 준다 — 앱의 errors.ts 는 예전 코드 셋을 안다. 활성 run 을 다시 읽어 고른다.
    (컨텍스트를 읽은 뒤 run 시작 사이에 활성 run 이 생긴 경합 흉내)"""
    room = new_room(fake_server, shared=True)
    original = server_api.start_run

    async def racing(conversation_id, user, **kwargs):
        fake_server.put_run(conversation_id, request_id="other", status=status)
        return await original(conversation_id, user, **kwargs)

    monkeypatch.setattr(server_api, "start_run", racing)
    r = _say(client, room, "x")
    assert r.status_code == 409 and r.json()["detail"]["code"] == code


@pytest.mark.asyncio
async def test_잠금_검사와_획득_사이에_await_가_없다():
    """🔴 그 창에서 루프를 놓으면 두 요청이 나란히 통과해 같은 방에 run 이 둘 생긴다.

    소스에서 직접 확인한다 — 동시성 사고는 테스트로 재현하기 어렵고, 규칙은 "그 사이에 await 를
    두지 않는다" 하나다 (체크리스트 "잠금 틈"). server 에 묻는 일(컨텍스트·연결 정보)은 전부 그 앞에 끝낸다.
    """
    import inspect
    import re

    from kukie_agent import conversations_api

    source = inspect.getsource(conversations_api)
    blocks = re.findall(
        r"if conversation\.lock\.locked\(\):(.*?)async with conversation\.lock:", source, re.S
    )
    # 정규식이 하나도 안 걸리면 for 문은 검사 없이 통과한다 — 잠그는 자리 수와 맞는지부터 본다 (자동 리뷰 지적)
    taken = source.count("async with conversation.lock:")
    assert taken >= 3, f"잠그는 자리가 {taken} 곳뿐이다 — 감시 대상이 사라졌는지 본다"
    assert len(blocks) == taken, (
        f"잠그는 자리 {taken} 곳 중 {len(blocks)} 곳만 정규식에 걸렸다 — "
        "앞의 locked() 검사가 없어졌거나 사이에 다른 코드가 끼었다"
    )
    for block in blocks:
        assert "await " not in block, f"잠금 검사와 획득 사이에 await 가 있다:\n{block}"
