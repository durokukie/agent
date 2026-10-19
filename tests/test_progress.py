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


def test_run이_시작되기_전에_연결하면_시작을_기다렸다가_받는다():
    hub = progress.ProgressHub()

    async def go():
        reader = asyncio.create_task(_collect(hub, "room"))
        await asyncio.sleep(0.01)
        await hub.begin("room")
        await hub.publish("room", {"type": "step_start", "id": "c1", "label": "이벤트 보기"})
        await hub.end("room")
        return await asyncio.wait_for(reader, 2)

    assert [e["type"] for e in _events(sync(go()))] == ["step_start", "done"]


def test_기다려도_run이_없으면_done만_보내고_닫는다(monkeypatch):
    monkeypatch.setattr(progress, "WAIT_FOR_RUN_SECONDS", 0.01)
    assert _events(sync(_collect(progress.ProgressHub(), "room"))) == [{"type": "done"}]


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
    assert ": keep-alive\n\n" in chunks and _events(chunks)[-1] == {"type": "done"}


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
    assert _events(r.text.split("\n\n")) == [{"type": "done"}]

    assert client.get(f"/conversations/{room}/progress", headers=OTHER).status_code in (403, 404)


def test_근거_없는_결과는_성공이_아니라_불명이고_승인_미룸은_단계를_남기지_않는다():
    from pydantic_ai.exceptions import ApprovalRequired
    from pydantic_ai.messages import ToolCallPart

    hub = progress.ProgressHub()
    cap = hub.capability("room")
    read = ToolCallPart(tool_name="list_resources", args={"kind": "pod"}, tool_call_id="r1")
    change = ToolCallPart(tool_name="scale_resource", args={"kind": "deployment", "name": "api"}, tool_call_id="m1")

    async def go():
        await hub.begin("room")
        await cap.before_tool_execute(None, call=read, tool_def=None, args={})
        await cap.after_tool_execute(None, call=read, tool_def=None, args={}, result="문자열 결과")
        await cap.before_tool_execute(None, call=change, tool_def=None, args={})
        with pytest.raises(ApprovalRequired):
            await cap.on_tool_execute_error(None, call=change, tool_def=None, args={}, error=ApprovalRequired())
        return list(hub._runs["room"].events)

    events = sync(go())
    assert [e["type"] for e in events] == ["step_start", "step_end"]      # 변경 도구는 아무것도 안 남는다
    assert events[1]["ok"] is None
