"""진행 상황 — run 이 도는 동안의 도구 호출을 방별 SSE 로 (progress.py, GET /conversations/{id}/progress)."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from helpers import OTHER, USER, chat_model, new_room, sync
from kukie_agent import progress, server
from kukie_agent.agent import agent


def _events(chunks: list[str]) -> list[dict]:
    return [json.loads(c.removeprefix("data: ")) for c in chunks if c.startswith("data: ")]


async def _collect(hub: progress.ProgressHub, key: str) -> list[str]:
    return [chunk async for chunk in hub.stream(key)]


# ── 표시용 문자열 ─────────────────────────────────────────────

def test_명령에서_임시_kubeconfig와_context를_뺀다():
    raw = "kubectl --kubeconfig /tmp/kukie-abc/config --context kind-team get pods -n study -o wide"
    assert progress.display_command(raw) == "kubectl get pods -n study -o wide"
    assert progress.display_command("kubectl --context=kind get ns") == "kubectl get ns"


def test_도구_이름을_화면_말로_바꾸고_빠진_인자는_비운다():
    assert progress.label("describe_resource", {"kind": "pod", "name": "api-1"}) == "pod/api-1 자세히 보기"
    assert progress.label("get_logs", {}) == "… 로그 보기"
    assert progress.label("unknown_tool", {"x": 1}) == "unknown_tool"


# ── 우체통 ───────────────────────────────────────────────────

def test_늦게_연결해도_지난_단계부터_받고_끝나면_done으로_닫힌다():
    hub = progress.ProgressHub()

    async def go():
        await hub.begin("room")
        await hub.publish("room", {"type": "step_start", "id": "c1", "label": "pod 목록 보기"})
        reader = asyncio.create_task(_collect(hub, "room"))
        await asyncio.sleep(0)
        await hub.publish("room", {"type": "step_end", "id": "c1", "command": "kubectl get pods", "ok": True})
        await hub.end("room")
        return await asyncio.wait_for(reader, 2)

    events = _events(sync(go()))
    assert [e["type"] for e in events] == ["step_start", "step_end", "done"]
    assert events[-1] == {"type": "done", "run": True}                 # 끝난 done 에는 run 키가 늘 있다


def test_run이_시작되기_전에_연결하면_시작을_기다렸다가_받는다():
    hub = progress.ProgressHub()

    async def go():
        reader = asyncio.create_task(_collect(hub, "room"))
        await asyncio.sleep(0.01)
        await hub.begin("room")
        await hub.publish("room", {"type": "step_start", "id": "c1", "label": "이벤트 보기"})
        await hub.end("room")
        return await asyncio.wait_for(reader, 2)

    got = _events(sync(go()))
    assert [e["type"] for e in got] == ["step_start", "done"] and got[-1]["run"] is True


def test_기다려도_run이_없으면_done만_보내고_닫는다(monkeypatch):
    monkeypatch.setattr(progress, "WAIT_FOR_RUN_SECONDS", 0.01)
    assert _events(sync(_collect(progress.ProgressHub(), "room"))) == [{"type": "done", "run": False}]


def test_조용하면_keep_alive_주석을_보낸다(monkeypatch):
    monkeypatch.setattr(progress, "HEARTBEAT_SECONDS", 0.01)
    hub = progress.ProgressHub()

    async def go():
        await hub.begin("room")
        reader = asyncio.create_task(_collect(hub, "room"))
        await asyncio.sleep(0.05)
        await hub.end("room")
        return await asyncio.wait_for(reader, 2)

    chunks = sync(go())
    assert ": keep-alive\n\n" in chunks and _events(chunks)[-1] == {"type": "done", "run": True}


# ── 실제 run 과 엔드포인트 ─────────────────────────────────────

@pytest.fixture
def client(dev_auth, fake_server, kubectl_ok, plan_dir):
    progress.hub = progress.ProgressHub()
    return TestClient(server.app)


def test_채팅의_도구_호출이_그_방의_단계로_올라간다(client, fake_server, monkeypatch):
    room = new_room(fake_server)
    seen: list[dict] = []
    publish = progress.hub.publish

    async def record(key, event):
        if key == room:
            seen.append(event)
        await publish(key, event)

    monkeypatch.setattr(progress.hub, "publish", record)
    with agent.override(model=chat_model()):
        assert client.post(f"/conversations/{room}/chat", json={"text": "파드"}, headers=USER).status_code == 200

    assert room not in progress.hub._runs                     # 끝난 run 은 들고 있지 않는다
    start, end = seen
    assert start["type"] == "step_start" and start["label"].endswith("목록 보기")
    assert end == {"type": "step_end", "id": start["id"], "command": end["command"], "ok": True}
    assert "--kubeconfig" not in (end["command"] or "")


def test_진행_상황은_대화를_읽을_수_있는_사람만_연다(client, fake_server, monkeypatch):
    monkeypatch.setattr(progress, "WAIT_FOR_RUN_SECONDS", 0.01)
    room = new_room(fake_server)

    r = client.get(f"/conversations/{room}/progress", headers=USER)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert _events(r.text.split("\n\n")) == [{"type": "done", "run": False}]

    assert client.get(f"/conversations/{room}/progress", headers=OTHER).status_code in (403, 404)


def test_근거_없는_결과는_성공이_아니라_불명이고_돌지_않은_도구는_지운다():
    from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
    from pydantic_ai.messages import ToolCallPart

    hub = progress.ProgressHub()
    cap = hub.capability("room")
    read = ToolCallPart(tool_name="list_resources", args={"kind": "pod"}, tool_call_id="r1")
    change = ToolCallPart(tool_name="scale_resource", args={"kind": "deployment", "name": "api"}, tool_call_id="m1")
    retry = ToolCallPart(tool_name="scale_resource", args={}, tool_call_id="m2")
    broken = ToolCallPart(tool_name="list_resources", args={"kind": "pod"}, tool_call_id="r2")

    async def go():
        await hub.begin("room")
        for call in (read, change, retry, broken):
            await cap.before_tool_execute(None, call=call, tool_def=None, args={})
        await cap.after_tool_execute(None, call=read, tool_def=None, args={}, result="문자열 결과")
        for call, error in ((change, ApprovalRequired()), (retry, ModelRetry("다시")), (broken, RuntimeError("x"))):
            with pytest.raises(type(error)):
                await cap.on_tool_execute_error(None, call=call, tool_def=None, args={}, error=error)
        return list(hub._runs["room"].events)

    events = sync(go())
    kept = [(e["type"], e["id"]) for e in events]
    assert ("step_drop", "m1") in kept and ("step_drop", "m2") in kept               # 미룸·재시도는 지우라고 알린다
    assert ("step_end", "m1") not in kept and ("step_end", "m2") not in kept         # 실패(✗)로는 안 닫는다
    assert [e for e in events if e["type"] == "step_end" and e["id"] == "r1"][0]["ok"] is None
    assert [e for e in events if e["type"] == "step_end" and e["id"] == "r2"][0]["ok"] is False   # 진짜 예외는 실패


# ── 승인 카드 경로 (가드레일이 변경 도구를 미룸) ─────────────────

def test_승인_카드_단계엔_단계가_없고_승인_뒤_실제로_돌_때_뜬다(
    dev_auth, fake_server, kubectl_ok, hook_passes, monkeypatch
):
    from test_plan_flow import _approve, _card, _ok, _room
    from kukie_agent.tools import mutate

    monkeypatch.setattr(mutate, "run_kubectl", lambda args, **kw: _ok("scaled\n"))

    progress.hub = progress.ProgressHub()
    client = TestClient(server.app)
    room = _room(fake_server)
    seen: list[dict] = []
    publish = progress.hub.publish

    async def record(key, event):
        if key == room:
            seen.append(event)
        await publish(key, event)

    monkeypatch.setattr(progress.hub, "publish", record)

    _card(client, room)
    # 가드레일이 우리 훅보다 먼저 승인으로 미룬다 — 카드 단계에는 아무것도 안 올라간다 (끝나지 않는 단계 없음).
    # 순서가 바뀌어 우리 시작이 먼저 올라가더라도 on_tool_execute_error 의 step_drop 이 지운다 (위 단위 테스트)
    assert [e["type"] for e in seen if e["type"] != "step_drop"] == []

    seen.clear()
    assert _approve(client, room).status_code == 200
    start, end = seen
    assert start["type"] == "step_start" and start["label"] == "deployment/nginx 개수 바꾸기"
    assert end["type"] == "step_end" and end["ok"] is True


def test_drop_은_덧붙이기만_해서_이미_본_연결도_받는다():
    """중간을 지우면 stream 의 커서가 어긋나 이미 시작을 본 화면이 drop 을 못 받는다 (리뷰)."""
    hub = progress.ProgressHub()

    async def go():
        await hub.begin("room")
        reader = asyncio.create_task(_collect(hub, "room"))
        await hub.publish("room", {"type": "step_start", "id": "m1", "label": "x"})
        await asyncio.sleep(0.01)                        # 화면이 시작을 받은 뒤
        await hub.publish("room", {"type": "step_drop", "id": "m1"})
        await hub.end("room")
        return await asyncio.wait_for(reader, 2)

    assert [e["type"] for e in _events(sync(go()))] == ["step_start", "step_drop", "done"]


# ── 답 미리보기 (KUKIE_STREAM_ANSWER) ─────────────────────────

def test_답_양식_조각에서_narration만_지금까지의_글로_흘린다(monkeypatch):
    from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, ToolCallPart, ToolCallPartDelta

    monkeypatch.setattr(progress, "ANSWER_INTERVAL_SECONDS", 0)
    hub = progress.ProgressHub()
    chunks = ['{"narr', 'ation": "nginx 파드', '를 2개로 늘렸', '습니다.", "finding": null}']

    async def events():
        yield PartStartEvent(index=0, part=ToolCallPart(tool_name="list_resources", args='{"kind": "pod"}'))
        yield PartStartEvent(index=1, part=ToolCallPart(tool_name="build_KukieResponse", args=chunks[0]))
        for c in chunks[1:]:
            yield PartDeltaEvent(index=1, delta=ToolCallPartDelta(args_delta=c))

    published: list[str] = []
    publish = hub.publish

    async def record(key, event):
        published.append(event["text"])
        await publish(key, event)

    hub.publish = record

    async def go():
        await hub.begin("room")
        await hub.answer_handler("room")(None, events())
        return hub._runs["room"]

    run = sync(go())
    assert published == ["nginx 파드", "nginx 파드를 2개로 늘렸", "nginx 파드를 2개로 늘렸습니다."]   # 도구 인자는 안 섞인다
    assert run.answer == published[-1] and run.events == []          # 미리보기는 events 에 안 쌓인다


def test_간격에_걸린_마지막_조각도_끝에_보낸다(monkeypatch):
    from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, ToolCallPart, ToolCallPartDelta

    monkeypatch.setattr(progress, "ANSWER_INTERVAL_SECONDS", 60)       # 첫 전송 뒤로는 전부 간격에 걸린다
    hub = progress.ProgressHub()

    async def events():
        yield PartStartEvent(index=0, part=ToolCallPart(tool_name="build_KukieResponse", args='{"narration": "가'))
        yield PartDeltaEvent(index=0, delta=ToolCallPartDelta(args_delta='나다"}'))

    async def go():
        await hub.begin("room")
        await hub.answer_handler("room")(None, events())
        return hub._runs["room"].answer

    assert sync(go()) == "가나다"


def test_dict_조각은_합치고_미리보기가_터져도_run_은_안_죽는다(monkeypatch):
    from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, ToolCallPart, ToolCallPartDelta

    monkeypatch.setattr(progress, "ANSWER_INTERVAL_SECONDS", 0)
    hub = progress.ProgressHub()

    async def events():
        yield PartStartEvent(index=0, part=ToolCallPart(tool_name="build_KukieResponse", args={"narration": "앞"}))
        yield PartDeltaEvent(index=0, delta=ToolCallPartDelta(args_delta={"finding": None}))   # narration 이 남아야 한다
        yield PartStartEvent(index=1, part=ToolCallPart(tool_name="x", args=[1, 2]))    # from_json 이 TypeError — 가드가 잡아야 한다

    async def go():
        await hub.begin("room")
        await hub.answer_handler("room")(None, events())                 # 예외 없이 끝나야 한다
        return hub._runs["room"].answer

    assert sync(go()) == "앞"


def test_늦게_붙은_화면은_마지막_미리보기_하나만_받는다():
    hub = progress.ProgressHub()

    async def go():
        await hub.begin("room")
        for text in ("가", "가나", "가나다"):
            await hub.publish("room", {"type": "answer", "text": text})
        reader = asyncio.create_task(_collect(hub, "room"))
        await asyncio.sleep(0.01)
        await hub.end("room")
        return await asyncio.wait_for(reader, 2)

    assert _events(sync(go())) == [{"type": "answer", "text": "가나다"}, {"type": "done", "run": True}]


def test_스위치를_켜면_실제_채팅에서_답_미리보기가_올라간다(client, fake_server, monkeypatch):
    monkeypatch.setenv("KUKIE_STREAM_ANSWER", "1")
    monkeypatch.setattr(progress, "ANSWER_INTERVAL_SECONDS", 0)
    room = new_room(fake_server)
    seen: list[dict] = []
    publish = progress.hub.publish

    async def record(key, event):
        if key == room:
            seen.append(event)
        await publish(key, event)

    monkeypatch.setattr(progress.hub, "publish", record)
    with agent.override(model=chat_model("파드 하나가 떠 있어요")):
        r = client.post(f"/conversations/{room}/chat", json={"text": "파드"}, headers=USER)
    assert r.status_code == 200 and r.json()["response"]["narration"] == "파드 하나가 떠 있어요"
    answers = [e["text"] for e in seen if e["type"] == "answer"]
    assert answers and answers[-1] == "파드 하나가 떠 있어요"                       # 미리보기 끝 = 진짜 답


def test_스위치가_꺼져_있으면_실제_채팅에서_답_미리보기가_없다(client, fake_server, monkeypatch):
    monkeypatch.delenv("KUKIE_STREAM_ANSWER", raising=False)
    room = new_room(fake_server)
    seen: list[dict] = []
    publish = progress.hub.publish

    async def record(key, event):
        if key == room:
            seen.append(event)
        await publish(key, event)

    monkeypatch.setattr(progress.hub, "publish", record)
    with agent.override(model=chat_model("답")):
        assert client.post(f"/conversations/{room}/chat", json={"text": "파드"}, headers=USER).status_code == 200
    assert seen and not [e for e in seen if e["type"] == "answer"]      # 단계는 오고 미리보기는 없다


def test_str_조각_뒤에_dict_조각이_와도_앞의_narration_을_잃지_않는다(monkeypatch):
    from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, ToolCallPart, ToolCallPartDelta

    monkeypatch.setattr(progress, "ANSWER_INTERVAL_SECONDS", 0)
    hub = progress.ProgressHub()

    async def events():
        yield PartStartEvent(index=0, part=ToolCallPart(tool_name="build_KukieResponse", args='{"narration": "nginx 파드를'))
        yield PartDeltaEvent(index=0, delta=ToolCallPartDelta(args_delta={"finding": None}))

    async def go():
        await hub.begin("room")
        await hub.answer_handler("room")(None, events())
        return hub._runs["room"].answer

    assert sync(go()) == "nginx 파드를"


def test_승인_카드로_끝난_run_은_미리보기를_지운다(monkeypatch):
    """같은 응답에 답 양식도 썼다면 미리보기에 '늘렸습니다' 가 남는다 — 실행 전이니 끝에 빈 글로 지운다."""
    from types import SimpleNamespace

    from pydantic_ai.tools import DeferredToolRequests

    from kukie_agent import server as srv

    monkeypatch.setenv("KUKIE_STREAM_ANSWER", "1")
    hub = progress.hub = progress.ProgressHub()
    seen: list[dict] = []
    publish = hub.publish

    async def record(key, event):
        seen.append(event)
        await publish(key, event)

    monkeypatch.setattr(hub, "publish", record)

    async def fake_run(**kwargs):
        await hub.publish("room", {"type": "answer", "text": "늘렸습니다"})
        return SimpleNamespace(output=DeferredToolRequests())

    monkeypatch.setattr(srv.agent, "run", fake_run)
    session = SimpleNamespace(progress_key="room", deps=None, history=[],
                              skill=SimpleNamespace(output_fn=lambda **_: None))
    sync(srv._run_agent(session, user_prompt="x"))
    assert seen[-1] == {"type": "answer", "text": ""}
