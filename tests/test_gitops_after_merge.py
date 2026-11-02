"""승인 = 머지까지만 요청에 묶고, sync·health 는 뒤에서 — 끊긴 요청·재시작·재클릭·sync 실패·시간 초과 (DURO-158).

test_gitops_flow.py 와 같은 틀이다: 가드레일 훅을 진짜로 돌리고 모델·server·GitHub·클러스터는 가짜다.
끊긴 요청은 진짜 uvicorn 에 진짜 소켓을 열어 끊는다 — 취소되지 않는다는 것이 프레임워크의 동작이라 TestClient 로는 볼 수 없다.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import ToolApproved, ToolDenied

from helpers import USER, answer_model, new_room, scale_call
from kukie_agent import conversations_api, progress, server, server_api
from kukie_agent.agent import agent
from kukie_agent.gitops import watch
from kukie_agent.gitops.github import GitHubError
from kukie_agent.guardrail import gitops_after_merge, gitops_hook
from kukie_agent.response import merged_reply
from test_gitops_flow import _card, _decide, _plan, _settle, _tool_result
from test_gitops_flow import client, cluster, gh, room  # noqa: F401 — 픽스처를 같이 쓴다

WAIT = 10   # 이 안에 끝나지 않으면 실패로 본다 (초)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(gitops_after_merge, "SAVE_RETRY_SECONDS", (0, 0))
    monkeypatch.setattr(gitops_after_merge, "RECOVERY_RETRY_SECONDS", (0,))
    monkeypatch.setattr(gitops_hook, "RECORD_RETRY_SECONDS", 0)


@pytest.fixture
def gate(monkeypatch, cluster):  # noqa: F811
    """지켜보기의 kubectl 을 세운다 — set 하면 간다. 그동안 응답이 오는지 본다."""
    event = threading.Event()
    run = watch.run_kubectl

    def held(args, **kwargs):
        assert event.wait(WAIT)
        return run(args, **kwargs)

    monkeypatch.setattr(watch, "run_kubectl", held)
    yield event
    event.set()


def _actions(fake_server):
    return [c["action"] for name, c in fake_server.calls if name == "plan_action"]


def _sync(fake_server, room):  # noqa: F811
    return _plan(fake_server, room)["executionResult"]["sync_result"]


def _wait_for(check, what):
    deadline = time.monotonic() + WAIT
    while not check():
        assert time.monotonic() < deadline, f"시간 안에 안 됐다: {what}"
        time.sleep(0.01)


# ── 머지까지 하고 바로 답한다 ──────────────────────────────

def test_sync_를_기다리지_않고_머지_뒤_바로_답한다(client, fake_server, room, gh, gate):  # noqa: F811
    _card(client, room)
    body = _decide(client, room)                             # gate 가 닫혀 있어도 돌아온다

    assert body["kind"] == "answer"
    assert len(gh.merges) == 1
    plan = _plan(fake_server, room)
    assert plan["status"] == "APPLIED"
    assert plan["executionResult"]["merge_commit"] == gh.merges[0]["sha"]
    assert plan["executionResult"]["sync_result"]["status"] == "syncing"
    assert fake_server.runs_of(room)[-1]["status"] == "completed"
    assert "아직 반영됐다고 말하지 마라" in str(_tool_result(fake_server, room)["content"])
    assert body["response"]["narration"] == "PR #1 을 머지했어요. 반영 결과는 위 카드에서 볼 수 있어요."

    gate.set()
    _settle(client)
    assert _sync(fake_server, room)["status"] == "healthy"
    assert _actions(fake_server)[-2:] == ["result", "sync-result"]


def test_자동_sync가_꺼진_앱은_지켜보기를_시작하지_않는다(client, fake_server, room, gh, cluster):  # noqa: F811
    cluster.auto_sync = False
    _card(client, room)
    _decide(client, room)

    assert not gitops_after_merge._tasks
    assert _sync(fake_server, room)["status"] == "manual"
    assert "sync-result" not in _actions(fake_server)


def test_지켜보기는_요청과_따로_자기_임시_kubeconfig로_돈다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    seen: list[tuple[Path, bool]] = []
    run = watch.run_kubectl

    def spy(args, *, kubeconfig, **kwargs):
        seen.append((kubeconfig, kubeconfig.exists()))
        return run(args, kubeconfig=kubeconfig, **kwargs)

    monkeypatch.setattr(watch, "run_kubectl", spy)
    _card(client, room)
    _decide(client, room)
    _settle(client)

    assert seen and all(existed for _, existed in seen)      # 요청의 파일은 이미 지워졌어도 지켜보기의 파일은 있다
    assert not seen[0][0].exists()                           # 끝나면 자기 파일도 지운다
    assert client.portal.call(lambda: gitops_after_merge._approvals == set())


# ── 지켜보기의 끝: 실패·시간 초과 ───────────────────────────

def test_sync가_실패하면_sync_failed를_남긴다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    cluster.app_status = {"sync": {"status": "OutOfSync"},
                          "operationState": {"phase": "Failed", "message": "nope",
                                             "syncResult": {"revision": "merge1" + "0" * 34}}}
    _decide(client, room)
    _settle(client)

    plan = _plan(fake_server, room)
    assert plan["status"] == "APPLIED" and plan["executionResult"]["sync_result"]["status"] == "sync_failed"


def test_지켜보기가_시간을_넘기면_timeout으로_끝난다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    cluster.app_status = {"sync": {"status": "Synced", "revision": "old"}, "health": {"status": "Healthy"}}
    _decide(client, room)
    _settle(client)                                          # cluster 픽스처가 SYNC_WAIT 를 0 으로 둔다

    plan = _plan(fake_server, room)
    assert plan["status"] == "APPLIED" and plan["executionResult"]["sync_result"]["status"] == "timeout"


def test_지켜보기_자체가_터져도_syncing에_멈추지_않는다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("kubectl 없음")

    monkeypatch.setattr(watch, "run_kubectl", boom)
    _card(client, room)
    _decide(client, room)
    _settle(client)

    assert _sync(fake_server, room)["status"] == "unreadable"
    assert _plan(fake_server, room)["executionResult"]["merge_commit"] == gh.merges[0]["sha"]


def test_sync_결과_기록이_잠깐_실패해도_다시_남긴다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    real, failures = server_api.plan_action, [1]

    async def flaky(plan_id, action, body=None):
        if action == "sync-result" and failures:
            failures.pop()
            raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await real(plan_id, action, body)

    monkeypatch.setattr(server_api, "plan_action", flaky)
    _card(client, room)
    _decide(client, room)
    _settle(client)

    assert _sync(fake_server, room)["status"] == "healthy"


def test_끌_때_지켜보기를_멈추고_interrupted로_남긴다(fake_server, dev_auth, kubectl_ok, hook_passes, gh, cluster, room, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    async def guidance(plan, manifest_preview=None):
        return "확인한다."

    monkeypatch.setattr(gitops_hook, "generate_decision_guidance", guidance)
    monkeypatch.setattr(watch, "SYNC_WAIT", 3600)
    cluster.app_status = {"sync": {"status": "Synced", "revision": "old"}, "health": {"status": "Healthy"}}
    started = time.monotonic()
    with TestClient(server.app) as test_client:
        _card(test_client, room)
        _decide(test_client, room)
        assert _sync(fake_server, room)["status"] == "syncing"
    assert time.monotonic() - started < WAIT                 # 한 시간을 기다리지 않는다

    sync = _sync(fake_server, room)
    assert sync["status"] == "interrupted" and "머지는 됐으니" in sync["message"]


# ── 머지 기록 ──────────────────────────────────────────────

def test_머지_기록이_잠깐_실패하면_다시_남긴다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    real, failures = server_api.plan_action, [1]

    async def flaky(plan_id, action, body=None):
        if action == "result" and failures:
            failures.pop()
            raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await real(plan_id, action, body)

    monkeypatch.setattr(server_api, "plan_action", flaky)
    _card(client, room)
    _decide(client, room)
    _settle(client)

    plan = _plan(fake_server, room)
    assert plan["executionResult"]["merge_commit"] == gh.merges[0]["sha"]
    assert plan["executionResult"]["sync_result"]["status"] == "healthy"


def test_머지_기록의_응답만_잃으면_기록된_것으로_본다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    real, lost = server_api.plan_action, [1]

    async def lossy(plan_id, action, body=None):
        updated = await real(plan_id, action, body)
        if action == "result" and lost:
            lost.pop()
            raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "응답을 잃었다")
        return updated

    monkeypatch.setattr(server_api, "plan_action", lossy)
    _card(client, room)
    _decide(client, room)
    _settle(client)

    assert "실행 결과를 Action Plan에 기록하지 못했습니다" not in str(_tool_result(fake_server, room)["content"])
    assert _sync(fake_server, room)["status"] == "healthy"


def test_머지_기록을_끝내_못_하면_지켜보지_않고_재실행도_막는다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    real = server_api.plan_action

    async def down(plan_id, action, body=None):
        if action == "result":
            raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "down")
        return await real(plan_id, action, body)

    monkeypatch.setattr(server_api, "plan_action", down)
    _card(client, room)
    _decide(client, room)

    assert not gitops_after_merge._tasks
    assert "동일 요청의 재실행은 차단" in str(_tool_result(fake_server, room)["content"])
    assert len(gh.merges) == 1


# ── 재클릭·재시도 ───────────────────────────────────────────

def test_같은_카드를_다시_눌러도_머지는_한_번이다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    _decide(client, room)
    with agent.override(model=answer_model()):
        again = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)

    assert again.status_code == 409 and again.json()["detail"]["code"] == "NOT_PENDING"
    assert len(gh.merges) == 1
    _settle(client)
    assert _actions(fake_server).count("result") == 1 and _actions(fake_server).count("sync-result") == 1


def test_머지_뒤_모델이_실패해_다시_시도해도_머지와_지켜보기는_한_번이다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    calls = []

    def fails_once(messages, info):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("모델 장애")
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args={"narration": "머지했어요."}, tool_call_id="o")])

    with agent.override(model=FunctionModel(fails_once)):
        failed = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)
        assert failed.status_code == 503 and failed.json()["detail"]["code"] == "RESUME_RETRYABLE"
        assert fake_server.runs_of(room)[-1]["status"] == "recovery_required"
        retried = client.post(f"/conversations/{room}/resume", headers=USER)
    assert retried.status_code == 200 and retried.json()["kind"] == "answer"

    _settle(client)
    assert len(gh.merges) == 1
    assert _actions(fake_server).count("result") == 1 and _actions(fake_server).count("sync-result") == 1
    assert _plan(fake_server, room)["executionResult"]["sync_result"]["status"] == "healthy"


# ── 시간 안에 못 끝나면 먼저 답한다 ─────────────────────────

# ── 머지 직후의 답은 코드가 쓴다 ──────────────────────────

STALE_ANSWER = {
    "narration": "ArgoCD 가 아직 동기화하지 않아 4개 상태입니다.",
    "suggested_next_action": "잠시 뒤 다시 확인하세요.",
    "finding": {"cause": "아직 sync 전", "evidence": "READY 4/4", "recommendation": "기다린다"},
}
MERGED_REPLY = "PR #1 을 머지했어요. 반영 결과는 위 카드에서 볼 수 있어요."


def _stale_model():
    """머지 직후 조회한 값으로 답을 쓰는 모델 — 답 양식은 한 번에, 미리보기는 나눠서 낸다."""
    async def answer(messages, info):
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=STALE_ANSWER, tool_call_id="o")])

    async def stream(messages, info):
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=json.dumps(STALE_ANSWER, ensure_ascii=False), tool_call_id="o")}

    return FunctionModel(answer, stream_function=stream)


def _approve_with(client, room, model):  # noqa: F811
    with agent.override(model=model):
        reply = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)
    assert reply.status_code == 200, reply.text
    return reply.json()["response"]


def test_머지_직후의_답은_LLM의_상태_판단_대신_코드가_쓴다(client, fake_server, room, gh, gate):  # noqa: F811
    _card(client, room)
    response = _approve_with(client, room, _stale_model())

    assert response["narration"] == MERGED_REPLY
    assert response["finding"] is None and response["suggested_next_action"] is None
    assert response["steps"][0]["step_label"] == "PR 머지"       # 실행 기록은 그대로다
    assert fake_server.runs_of(room)[-1]["responsePayload"]["response"]["narration"] == MERGED_REPLY   # 새로고침해도 같다


def test_미리보기에도_LLM의_글이_보이지_않는다(client, fake_server, room, gh, gate, monkeypatch):  # noqa: F811
    _card(client, room)
    monkeypatch.setenv("KUKIE_STREAM_ANSWER", "1")
    monkeypatch.setattr(progress, "ANSWER_INTERVAL_SECONDS", 0)
    previews: list[str] = []
    publish = progress.hub.publish

    async def record(key, event):
        if event["type"] == "answer":
            previews.append(event["text"])
        await publish(key, event)

    monkeypatch.setattr(progress.hub, "publish", record)
    response = _approve_with(client, room, _stale_model())

    assert response["narration"] == MERGED_REPLY
    assert previews and set(previews) == {MERGED_REPLY}


def test_머지가_막히면_설명은_LLM이_쓴다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    gh.mergeable_state = "blocked"
    response = _approve_with(client, room, _stale_model())

    assert gh.merges == []
    assert response["narration"] == STALE_ANSWER["narration"] and response["finding"] is not None


def _merged_part(call_id="m1"):
    result = gitops_hook.GitOpsResult(command="github pr merge o/r#1", stdout="머지", stderr="", success=True,
                                      pull_request={"number": 1}, sync_result=watch.syncing())
    return ToolReturnPart(tool_name=gitops_hook.TOOL_NAME, content=result, tool_call_id=call_id)


def _reply_of(*requests):
    messages = [ModelRequest(parts=[UserPromptPart(content="web 3개로")]), *(ModelRequest(parts=list(p)) for p in requests)]
    return merged_reply(SimpleNamespace(messages=messages))


def test_같이_보낸_변경이_실패하면_설명은_LLM이_쓴다():
    failed = RetryPromptPart(tool_name=gitops_hook.TOOL_NAME, content="실행하지 않았다", tool_call_id="m2")
    denied = ToolReturnPart(tool_name=gitops_hook.TOOL_NAME, content="거절됐다", tool_call_id="m2")
    assert _reply_of([_merged_part()]) == MERGED_REPLY
    assert _reply_of([_merged_part(), failed]) is None                  # 같은 묶음
    assert _reply_of([failed, _merged_part()]) is None
    assert _reply_of([_merged_part()], [denied]) is None                # 머지 뒤


def test_머지_앞의_재시도는_정상_흐름이라_코드가_쓴다():
    wrong_tool = RetryPromptPart(tool_name="scale_resource", content="PR 길이다", tool_call_id="s1")
    assert _reply_of([wrong_tool], [_merged_part()]) == MERGED_REPLY


def test_다음_턴의_답은_LLM이_쓴다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    _decide(client, room)
    _settle(client)
    with agent.override(model=answer_model("지금 3개예요.")):
        reply = client.post(f"/conversations/{room}/chat", json={"text": "지금 몇 개야?"}, headers=USER)

    assert reply.json()["response"]["narration"] == "지금 3개예요."


class _SlowModel:
    """승인 뒤 답을 늦게 내는 모델 — release 할 때까지 답하지 않는다."""

    def __init__(self):
        self.release = threading.Event()
        self.called = threading.Event()

    def model(self):
        async def answer(messages, info):
            self.called.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args={"narration": "늦은 답"}, tool_call_id="o")])

        async def stream(messages, info):       # 쓰이지 않는다 — 답 미리보기는 꺼져 있다
            raise NotImplementedError
            yield ""

        return FunctionModel(answer, stream_function=stream)


def test_시간_안에_못_끝나면_202로_먼저_답하고_일은_끝까지_간다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    monkeypatch.setattr(conversations_api, "APPROVE_WAIT", 0.2)
    _card(client, room)
    plan_id = _plan(fake_server, room)["id"]
    slow = _SlowModel()
    with agent.override(model=slow.model()):
        first = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)
        assert first.status_code == 202
        assert first.json()["kind"] == "accepted" and first.json()["plan_ids"] == [plan_id]
        assert len(gh.merges) == 1                           # 머지는 이미 됐다

        # 도는 동안 다시 눌러도, 재시도해도 잠금에 걸린다
        again = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "BUSY"
        assert client.post(f"/conversations/{room}/resume", headers=USER).status_code == 409

        slow.release.set()
        _wait_for(lambda: fake_server.runs_of(room)[-1]["status"] == "completed", "run 이 끝난다")

    _settle(client)
    run = fake_server.runs_of(room)[-1]
    assert run["responsePayload"]["kind"] == "answer"        # 답은 기록으로 남아 앱이 다시 읽는다
    assert "PR #1 을 머지했어요" in json.dumps(run["responsePayload"], ensure_ascii=False)
    assert len(gh.merges) == 1
    assert _plan(fake_server, room)["executionResult"]["sync_result"]["status"] == "healthy"


def _with_kubectl_card(client, room):  # noqa: F811
    """PR 머지 카드(call-1) 옆에 kubectl 카드(call-2)가 같은 턴에 있는 방."""
    _card(client, room)
    conversation = conversations_api.registry.get(room)
    conversation.session.pending.approvals.append(scale_call("call-2"))
    return conversation


async def _finish(detach):
    conversation = conversations_api.Conversation(id="r", session=SimpleNamespace(skill=SimpleNamespace(name="Shared")))
    await conversation.lock.acquire()
    finished = asyncio.Event()

    async def work():
        await finished.wait()
        return {"kind": "answer"}

    asyncio.get_running_loop().call_later(0.02, finished.set)
    return conversation, await conversations_api._finish_locked(conversation, work, [], detach=detach)


@pytest.mark.asyncio
async def test_떼어_돌리지_않는_결정은_시간이_넘어도_끝날_때까지_답을_기다린다(monkeypatch):
    monkeypatch.setattr(conversations_api, "APPROVE_WAIT", 0)
    conversation, reply = await _finish(detach=False)
    assert reply == {"kind": "answer"} and not conversation.lock.locked()


@pytest.mark.parametrize(("earlier", "call_id", "approved", "status"), [
    (None, "call-2", True, 200),                # kubectl 승인 — 옆에 PR 카드가 있어도 끝날 때까지 기다린다
    (None, "call-1", False, 200),               # PR 거절
    (None, "call-1", True, 202),                # PR 승인만 먼저 답한다
    (ToolApproved(), "call-1", True, 200),      # 먼저 승인한 kubectl 이 이 요청에서 함께 돈다 — 202 가 아니다
    (ToolDenied("거절"), "call-1", True, 202),   # 먼저 거절한 kubectl 은 돌지 않는다
])
def test_섞인_턴은_함께_돌_승인_카드로_떼어_돌릴지_정한다(client, fake_server, room, gh, cluster, monkeypatch, earlier, call_id, approved, status):  # noqa: F811
    monkeypatch.setattr(conversations_api, "APPROVE_WAIT", 0)
    session = _with_kubectl_card(client, room).session
    if earlier is not None:
        session.decisions["call-2"] = earlier

    async def slow(session, call_id, approved):
        await asyncio.sleep(0.05)
        return {"kind": "approval", "skill": "Shared", "approvals": []}, None

    monkeypatch.setattr(server, "_approve", slow)
    r = client.post(f"/conversations/{room}/approve", json={"call_id": call_id, "approved": approved}, headers=USER)

    assert r.status_code == status
    client.portal.call(lambda: asyncio.wait(set(gitops_after_merge._approvals)) if gitops_after_merge._approvals else asyncio.sleep(0))
    assert gh.merges == []


def test_재개는_승인한_카드가_전부_PR일_때만_떼어_돌린다(client, fake_server, room, gh, cluster):  # noqa: F811
    session = _with_kubectl_card(client, room).session
    only = conversations_api._only_merge_cards
    assert only(session, {"call-1"}) is True
    assert only(session, {"call-1", "call-2"}) is False
    assert only(session, {"call-2"}) is False
    assert only(session, set()) is False                     # 전부 거절했으면 머지가 없다


def test_PR_카드를_거절하면_시간이_넘어도_202로_답하지_않는다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    monkeypatch.setattr(conversations_api, "APPROVE_WAIT", 0)
    _card(client, room)
    with agent.override(model=answer_model()):
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": False}, headers=USER)
    assert r.status_code == 200 and r.json()["kind"] == "answer"
    assert gh.merges == []


# ── 끌 때 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_끌_때_떼어_돌던_승인을_마친_뒤에_server_연결을_닫는다(fake_server, monkeypatch):  # noqa: F811
    order = []

    async def approval():
        await asyncio.sleep(0.05)
        order.append("승인 기록")

    async def close():
        order.append("연결 닫기")

    monkeypatch.setattr(server_api, "close", close)
    async with server._lifespan(server.app):
        gitops_after_merge.keep_approval(asyncio.create_task(approval()))

    assert order == ["승인 기록", "연결 닫기"]


@pytest.mark.asyncio
async def test_끌_때_못_끝난_승인은_취소하고_방_잠금을_푼다(monkeypatch):
    monkeypatch.setattr(gitops_after_merge, "STOP_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(conversations_api, "APPROVE_WAIT", 0)
    conversation = conversations_api.Conversation(id="r", session=SimpleNamespace(skill=SimpleNamespace(name="Shared")))
    await conversation.lock.acquire()

    async def stuck():
        await asyncio.Event().wait()

    reply = await conversations_api._finish_locked(conversation, stuck, [], detach=True)
    assert reply.status_code == 202 and conversation.lock.locked()

    await gitops_after_merge.stop()
    gitops_after_merge.start()

    assert not conversation.lock.locked() and not gitops_after_merge._approvals


# ── 끊긴 요청 ──────────────────────────────────────────────

@pytest.fixture
def real_server(client, monkeypatch):  # noqa: F811
    """진짜 uvicorn. agent.override 가 ContextVar 라 테스트의 컨텍스트에서 띄운다."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    instance = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, lifespan="off", log_config=None))
    started = threading.Event()
    threads: list[threading.Thread] = []

    def start():
        # 스레드는 부른 쪽의 ContextVar 를 물려받지 않는다 — 모델 바꿔치기가 든 컨텍스트를 여기서 잡아 넘긴다
        context = contextvars.copy_context()
        threads.append(threading.Thread(target=context.run, args=(asyncio.run, _serve(instance, started)), daemon=True))
        threads[0].start()
        assert started.wait(WAIT)

    yield port, start
    instance.should_exit = True
    for thread in threads:
        thread.join(WAIT)


async def _serve(instance, started):
    task = asyncio.create_task(instance.serve())
    while not instance.started:
        await asyncio.sleep(0.01)
    started.set()
    await task
    while gitops_after_merge._tasks:
        await asyncio.wait(set(gitops_after_merge._tasks))


def test_앞단이_요청을_끊어도_취소되지_않고_Plan에_끝까지_남는다(client, real_server, fake_server, room, gh, cluster, caplog, monkeypatch):  # noqa: F811
    """실측의 그 상황 — web 이 499 로 끊었다. 머지·run·지켜보기가 끝까지 가고, uvicorn 은 끊긴 요청의 접근 로그를 남기지 않는다."""
    _card(client, room)
    port, start = real_server
    slow = _SlowModel()
    body = json.dumps({"call_id": "call-1", "approved": True}).encode()
    with agent.override(model=slow.model()):
        start()
        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            with socket.create_connection(("127.0.0.1", port)) as sock:
                sock.sendall(
                    f"POST /conversations/{room}/approve HTTP/1.1\r\nHost: x\r\nX-User: u-1\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
                )
                assert slow.called.wait(WAIT)                # 머지까지 끝나고 모델이 답을 쓰는 중이다
            # 소켓을 닫았다 = 앞단이 끊었다. 그 뒤에 답을 내게 한다
            time.sleep(0.1)
            assert _plan(fake_server, room)["status"] == "APPLIED"
            assert fake_server.runs_of(room)[-1]["status"] == "awaiting_approval"
            slow.release.set()
            _wait_for(lambda: fake_server.runs_of(room)[-1]["status"] == "completed", "끊겨도 run 이 끝난다")
            _wait_for(lambda: _sync(fake_server, room)["status"] == "healthy", "끊겨도 sync 결과가 남는다")

    assert len(gh.merges) == 1
    assert not [r for r in caplog.records if "/approve" in r.getMessage()]


# ── 재시작 ─────────────────────────────────────────────────

def _die_while_executing(fake_server, room):  # noqa: F811
    """앞 프로세스가 머지 전후로 죽은 자리를 만든다 — 승인·시작까지 남았고, 메모리의 지켜보기도 티켓도 없다."""
    plan = _plan(fake_server, room)
    fake_server._decision(plan, {"approved": True, "userId": "u-1", "call": {
        "tool": plan["toolName"], "args": plan["payload"]["args"], "command": plan["payload"]["command"],
        "risk": plan["risk"], "target": plan["payload"]["target"]}})
    fake_server._start(plan, {})
    return plan


def test_재시작하면_지켜보던_계획은_interrupted로_닫히고_머지는_남는다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    plan = _die_while_executing(fake_server, room)
    fake_server._result(plan, {"success": True, "stdout": "머지", "stderr": "", "exitCode": 0, "mergeCommit": "m" * 40,
                               "syncResult": watch.syncing()})

    asyncio.run(gitops_after_merge.recover())

    assert plan["status"] == "APPLIED"
    assert plan["executionResult"]["merge_commit"] == "m" * 40
    assert plan["executionResult"]["sync_result"]["status"] == "interrupted"
    run = fake_server.runs_of(room)[-1]
    assert run["status"] == "interrupted"                    # 이어 갈 티켓이 없다
    assert run["responsePayload"]["kind"] == "approval"      # 카드는 기록에 남아 Plan 으로 결과를 그린다


def test_머지하고_기록_전에_죽었으면_GitHub을_보고_머지를_남긴다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    plan = _die_while_executing(fake_server, room)
    sha = gh.merge_elsewhere(plan["payload"]["pull_request"]["number"])      # 머지 요청까지 닿고 기록 전에 죽었다

    asyncio.run(gitops_after_merge.recover())

    assert plan["status"] == "APPLIED"
    assert plan["executionResult"]["merge_commit"] == sha
    assert plan["executionResult"]["sync_result"]["status"] == "interrupted"
    assert not gh.merges                                     # 복구는 머지하지 않는다


def test_머지_전에_죽었으면_FAILED로_닫고_PR을_남긴다(client, fake_server, room, gh, cluster):  # noqa: F811
    _card(client, room)
    plan = _die_while_executing(fake_server, room)

    asyncio.run(gitops_after_merge.recover())

    assert plan["status"] == "FAILED" and plan["failureReason"] == "EXECUTION_FAILED"
    assert "PR 은 남겨 뒀어요" in plan["executionResult"]["stderr"]
    assert gh.pull_of(1)["state"] == "open" and not gh.merges


def test_GitHub을_못_읽으면_다시_묻고_끝내_못_읽으면_UNKNOWN이다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    _card(client, room)
    plan = _die_while_executing(fake_server, room)

    async def down(*a, **k):
        raise GitHubError(502, "bad gateway")

    monkeypatch.setattr(gitops_after_merge.pull_requests, "recheck", down)
    with pytest.raises(GitHubError):
        asyncio.run(gitops_after_merge.recover(final=False))
    assert plan["status"] == "EXECUTING"                     # 아직 판단하지 않는다

    asyncio.run(gitops_after_merge.recover(final=True))
    assert plan["status"] == "UNKNOWN"


def test_복구가_늦어도_이_프로세스가_실행_중인_계획은_건드리지_않는다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gitops_after_merge, "_recovering", True)
    monkeypatch.setattr(gitops_after_merge, "_claimed", set())
    _card(client, room)
    plan = _die_while_executing(fake_server, room)
    gitops_after_merge.claim(plan["id"])

    asyncio.run(gitops_after_merge.recover())

    assert plan["status"] == "EXECUTING"
    assert fake_server.runs_of(room)[-1]["status"] == "awaiting_approval"


def test_기동하면_뒤에서_복구하고_server가_아직_없으면_다시_시도한다(fake_server, gh, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient
    monkeypatch.setenv("KUKIE_SERVER_URL", "http://server.test")
    monkeypatch.setenv("KUKIE_INTERNAL_TOKEN", "internal")
    real, failures = server_api.unsettled_plans, [1]
    seen = []

    async def flaky(tool_name):
        seen.append(tool_name)
        if failures:
            failures.pop()
            raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "아직 안 떴다")
        return await real(tool_name)

    monkeypatch.setattr(server_api, "unsettled_plans", flaky)
    with TestClient(server.app) as test_client:
        assert test_client.get("/health").status_code == 200
        _wait_for(lambda: len(seen) == 2, "다시 묻는다")
        _settle(test_client)

    assert seen == ["propose_git_change"] * 2
    assert gitops_after_merge._recovering is False


def test_계획은_닫았는데_run을_못_닫으면_다시_시도에서_이어_닫는다(client, fake_server, room, gh, cluster, monkeypatch):  # noqa: F811
    _card(client, room)
    plan = _die_while_executing(fake_server, room)
    fake_server._result(plan, {"success": True, "stdout": "머지", "stderr": "", "exitCode": 0, "mergeCommit": "m" * 40,
                               "syncResult": watch.syncing()})
    real, failures = server_api.update_run, [1]

    async def flaky(run_id, **fields):
        if failures:
            failures.pop()
            raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "잠깐 못 받는다")
        return await real(run_id, **fields)

    monkeypatch.setattr(server_api, "update_run", flaky)
    asyncio.run(gitops_after_merge._recover_with_retries())

    assert plan["executionResult"]["sync_result"]["status"] == "interrupted"
    assert fake_server.runs_of(room)[-1]["status"] == "interrupted"


def test_kubectl_계획은_복구가_건드리지_않는다(fake_server):
    room_id = new_room(fake_server, shared=True)
    run = fake_server.put_run(room_id, request_id="r", status="awaiting_approval")
    plan_id = fake_server.add_plan(run["id"], tool_call_id="c", tool_name="scale_resource", risk="caution", payload={},
                                   status="EXECUTING", decision={"approved": True})

    asyncio.run(gitops_after_merge.recover())

    assert fake_server.plans[plan_id]["status"] == "EXECUTING" and run["status"] == "awaiting_approval"


def test_sync_결과의_칸은_어느_값이든_같다():
    # 앱이 읽는 칸이 그대로다 (api/gitops.ts readSyncResult)
    for result in (watch.syncing(), watch.manual(), watch.unreadable(), watch.interrupted()):
        assert set(result) == {"status", "sync_status", "health_status", "revision", "message", "waited_seconds"}
