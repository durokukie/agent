"""대화의 진행 상황 — run 이 도는 동안 "지금 무엇을 하는지" 를 화면에 흘려 보낸다.

`POST /chat`·`/approve`·`/resume` 은 그대로 끝난 뒤 한 번에 답한다 (request_id 재전송·run 상태 규칙을 건드리지 않는다).
화면은 기다리는 동안 따로 `GET /conversations/{id}/progress` 를 열어 단계를 받는다. 이 모듈은 그 사이의 우체통이다.

- 단계는 도구 실행 훅에서 만든다 (`agent.run(capabilities=[hub.capability(방)])`).
  도구가 돌기 직전 `step_start`(무엇을 하려는지), 끝나면 `step_end`(실제로 돈 kubectl 명령·성공 여부, 근거 없으면 None).
  돌지 않고 넘어가면(승인으로 미룸·모델 재시도) `step_drop` 으로 그 단계를 지운다 — 실패가 아니다.
- 답 미리보기(`KUKIE_STREAM_ANSWER=1` 일 때만): `{"type":"answer","text":지금까지 쓴 설명 전체}` — 조각이 아니라 전체라 마지막
  것만 그리면 맞다. 승인 카드로 끝난 run 은 끝에 빈 글(`""`)을 보내 미리보기를 지운다 (실행 전인데 "늘렸습니다" 가 남지 않게).
- run 이 끝나면 `{"type":"done","run":true}`, 연결했는데 run 이 시작되지 않으면 `{"type":"done","run":false}` — 앱은 false 일 때만
  다시 연결한다.
- 방마다 지금 run 의 단계를 모아 둔다. 늦게 연결한 화면도 앞 단계를 다시 받는다.
- 메모리에만 둔다 — 에이전트는 한 대이고(세션도 메모리), 진행 상황은 run 이 끝나면 쓸모가 없다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry
from pydantic_ai.messages import AgentStreamEvent, PartDeltaEvent, PartStartEvent, ToolCallPart, ToolCallPartDelta
from pydantic_ai.tools import ToolDefinition
from pydantic_core import from_json

logger = logging.getLogger(__name__)

#: 연결한 뒤 run 이 시작되기를 기다리는 시간. 화면은 POST 와 거의 동시에 연결하는데, 에이전트는 run 을 열기 전에
#: 권한·클러스터를 먼저 확인한다 — 그 사이에 연결이 먼저 닿는다.
WAIT_FOR_RUN_SECONDS = 15.0
#: 아무 일 없이 연결이 오래 조용하면 프록시(ALB 기본 60초)가 끊는다 — 그 전에 빈 줄(주석)을 보낸다.
HEARTBEAT_SECONDS = 15.0

#: 도구 이름 → 화면에 보일 말. 인자는 그대로 끼워 넣되 화면이 글자로만 그린다(HTML 아님).
_LABELS: dict[str, str] = {
    "list_resources": "{kind} 목록 보기",
    "describe_resource": "{kind}/{name} 자세히 보기",
    "get_events": "이벤트 보기",
    "get_logs": "{pod} 로그 보기",
    "explain_command": "{resource_or_field} 설명 찾기",
    "apply_manifest": "매니페스트 적용",
    "scale_resource": "{kind}/{name} 개수 바꾸기",
    "rollout_restart": "{kind}/{name} 재시작",
    "delete_resource": "{kind}/{name} 삭제",
}


class _Blank(dict):
    def __missing__(self, key: str) -> str:   # 인자가 빠졌으면 그 자리만 비운다
        return "…"


def label(tool_name: str, args: dict[str, Any]) -> str:
    template = _LABELS.get(tool_name)
    if template is None:
        return tool_name
    return template.format_map(_Blank({k: v for k, v in args.items() if isinstance(v, (str, int))}))


def display_command(command: str) -> str:
    """실제로 돈 명령에서 화면에 필요 없는 것을 뺀다 — 임시 kubeconfig 경로와 --context.

    kubeconfig 경로는 서버 안의 임시 파일이고, context 는 방마다 같아서 줄만 길게 만든다.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    shown: list[str] = []
    skip = False
    for part in parts:
        if skip:
            skip = False
            continue
        if part in ("--kubeconfig", "--context"):
            skip = True
            continue
        if part.startswith(("--kubeconfig=", "--context=")):
            continue
        shown.append(part)
    return shlex.join(shown)


@dataclass
class _Run:
    events: list[dict[str, Any]] = field(default_factory=list)
    # 답 미리보기는 events 에 쌓지 않고 마지막 것 한 칸만 — "지금까지의 글 전체" 라 마지막 것만 그리면 맞고,
    # 쌓으면 늦게 붙은 화면이 수백 개를 되받고 메모리가 답 길이의 제곱으로 는다 (리뷰)
    answer: str = ""
    done: bool = False
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)


class ProgressHub:
    """방(대화 id) 마다 지금 run 의 단계를 모으고, 연결한 화면에 흘려 보낸다."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}
        # run 이 시작되기를 기다리는 연결들. 시작되면 그 run 을 직접 건넨다 — 바로 끝나 _runs 에서 빠져도 받은 쪽은
        # run 을 쥐고 있어 단계를 다시 보고 done 까지 간다. 끝난 run 을 따로 들고 있지 않는다 (메모리)
        self._waiting: dict[str, list[asyncio.Future[_Run]]] = {}

    async def begin(self, key: str) -> None:
        """새 run 을 연다. 앞 run 의 단계는 버린다."""
        run = self._runs[key] = _Run()
        for waiter in self._waiting.pop(key, []):
            if not waiter.done():
                waiter.set_result(run)

    async def publish(self, key: str, event: dict[str, Any]) -> None:
        run = self._runs.get(key)
        if run is None or run.done:
            return
        async with run.changed:
            if event["type"] == "answer":
                run.answer = event["text"]
                run.changed.notify_all()
                return
            # 덧붙이기만 한다 — stream 은 "몇 번째까지 보냈나" 를 정수로 세서, 중간을 지우면 이미 본 화면이 step_drop 을
            # 못 받는다 (리뷰). 늦게 붙은 화면은 시작과 drop 을 둘 다 받아 앱에서 지운다
            run.events.append(event)
            run.changed.notify_all()

    async def end(self, key: str) -> None:
        # 끝난 run 은 들고 있지 않는다 — 방마다 영구히 쌓인다 (리뷰). 이미 붙은 스트림은 run 을 쥐고 있어 done 까지 간다.
        # 끝난 뒤에 처음 연결한 화면은 단계를 못 받지만, 그때는 답이 이미 와 있다 (다음 run 을 기다리다 done 으로 닫힌다)
        run = self._runs.pop(key, None)
        if run is None:
            return
        async with run.changed:
            run.done = True
            run.changed.notify_all()

    async def _next_run(self, key: str) -> _Run | None:
        run = self._runs.get(key)
        if run is not None:
            return run
        waiter: asyncio.Future[_Run] = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(key, []).append(waiter)
        try:
            return await asyncio.wait_for(waiter, WAIT_FOR_RUN_SECONDS)
        except TimeoutError:
            return None
        finally:
            waiters = self._waiting.get(key)
            if waiters and waiter in waiters:
                waiters.remove(waiter)
                if not waiters:
                    del self._waiting[key]

    async def stream(self, key: str) -> AsyncIterator[str]:
        """SSE 본문. 지금 run 이 있으면 지난 단계부터, 없으면 시작을 잠깐 기다린다. run 이 끝나면 `done` 으로 닫는다."""
        run = await self._next_run(key)
        if run is None:
            # run 이 끝난 done 과 가른다 — 앱은 아직 답을 기다리는 중이면 다시 연결한다 (리뷰)
            yield _sse({"type": "done", "run": False})
            return
        sent = 0
        sent_answer = ""
        while True:
            async with run.changed:
                if sent == len(run.events) and run.answer == sent_answer and not run.done:
                    try:
                        await asyncio.wait_for(run.changed.wait(), HEARTBEAT_SECONDS)
                    except TimeoutError:
                        pass
                pending, answer, finished = run.events[sent:], run.answer, run.done
            for event in pending:
                yield _sse(event)
            sent += len(pending)
            if answer != sent_answer:
                yield _sse({"type": "answer", "text": answer})
                sent_answer = answer
            elif not pending and not finished:
                yield ": keep-alive\n\n"
            if finished and sent == len(run.events):
                yield _sse({"type": "done", "run": True})   # run:false(시작 안 됨)와 짝을 맞춰 늘 적는다
                return

    def answer_handler(self, key: str):
        """`agent.run(event_stream_handler=...)` — 모델이 쓰는 답 양식(JSON)에서 설명(narration)만 꺼내 흘린다.

        답은 도구 호출 꼴의 JSON 으로 조금씩 온다 (`{"narr` → `ation": "nginx 를` …). 조각을 모아 미완성 JSON 을 그대로
        읽고(`allow_partial`), narration 이 있으면 **지금까지의 글 전체**를 `{"type":"answer","text":…}` 로 보낸다.
        조각만 보내지 않는 이유: 늦게 붙거나 다시 붙은 화면도, 모델이 양식을 다시 쓸 때(검증 재시도)도 마지막 것만
        그리면 맞다. 미리보기일 뿐이고 진짜 답은 POST 응답이다.
        """

        async def handle(_: RunContext[Any], events: AsyncIterable[AgentStreamEvent]) -> None:
            args: dict[int, str | dict[str, Any]] = {}   # 응답 조각 번호 → 지금까지 모인 args (문자열 조각 또는 dict)
            latest = sent = ""
            last = 0.0
            async for event in events:
                # 미리보기는 없어도 되는 덤이다 — 여기서 터져 진짜 답(run)이 죽으면 안 된다 (리뷰). 이벤트는 끝까지 소비한다
                try:
                    text = _collect(args, event)
                    if text is None:
                        continue
                    latest = text
                    now = time.monotonic()
                    if latest == sent or now - last < ANSWER_INTERVAL_SECONDS:
                        continue
                    sent, last = latest, now
                    await self.publish(key, {"type": "answer", "text": latest})
                except Exception:
                    logger.warning("답 미리보기를 만들지 못했다 (방=%s)", key, exc_info=True)
            # 마지막 조각은 대개 앞 전송 50ms 안에 온다 — 간격에 걸려 버려지면 미리보기가 문장 중간에서 끊긴다 (리뷰)
            if latest and latest != sent:
                await self.publish(key, {"type": "answer", "text": latest})

        return handle

    def capability(self, key: str) -> "_Progress":
        """`agent.run(capabilities=[...])` 에 넘길 것 — 이 run 의 도구 실행을 이 방의 단계로 올린다."""
        return _Progress(self, key)


@dataclass
class _Progress(AbstractCapability[Any]):
    """도구 실행 앞뒤에서 단계를 올린다. 모델 요청은 건드리지 않는다.

    단계는 `event_stream_handler` 가 아니라 이 도구 훅에서 만든다 — 그 핸들러를 붙이면 모델 요청이 스트리밍으로 바뀌는데,
    단계는 답 미리보기 스위치(`KUKIE_STREAM_ANSWER`)와 상관없이 늘 보여야 해서다. 가드레일은 이 훅보다 먼저 변경 도구를 승인으로 미뤄서
    카드 단계에는 아무것도 안 올라간다(승인 뒤 실제로 돌 때 뜬다). 순서가 달라 시작이 먼저 올라가도 step_drop 이 지운다.
    """

    hub: ProgressHub
    key: str

    async def before_tool_execute(self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition,
                                  args: Any) -> Any:
        await self.hub.publish(self.key, {
            "type": "step_start",
            "id": call.tool_call_id,
            "label": label(call.tool_name, call.args_as_dict()),
        })
        return args

    async def after_tool_execute(self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition,
                                 args: Any, result: Any) -> Any:
        command = getattr(result, "command", None)       # kubectl 도구는 KubectlResult 를 돌려준다
        success = getattr(result, "success", None)
        await self.hub.publish(self.key, {
            "type": "step_end",
            "id": call.tool_call_id,
            "command": display_command(command) if isinstance(command, str) else None,
            # 근거(KubectlResult.success)가 없으면 성공이라고 하지 않는다 — 불명(None)
            "ok": success if isinstance(success, bool) else None,
        })
        return result

    async def on_tool_execute_error(self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition,
                                    args: Any, error: Exception) -> Any:
        if isinstance(error, _NOT_RUN):
            # 실행하지 않고 넘어간 것 — 승인으로 미룸(카드가 대신 뜬다), 모델에게 인자를 다시 내라고 함(정상 흐름).
            # 실패가 아니므로 그 단계를 지운다. 승인 뒤 /approve 가 실제로 돌리면 그때 다시 시작부터 뜬다
            await self.hub.publish(self.key, {"type": "step_drop", "id": call.tool_call_id})
        else:
            await self.hub.publish(self.key, {"type": "step_end", "id": call.tool_call_id, "command": None, "ok": False})
        raise error


#: 도구가 실제로 돌지 않았음을 뜻하는 흐름 제어 예외. kubectl 자체의 실패는 예외가 아니라 KubectlResult(success=False) 다
_NOT_RUN = (ApprovalRequired, CallDeferred, ModelRetry)


#: 답 미리보기를 이보다 자주 보내지 않는다 — 조각마다 미완성 JSON 을 다시 읽어 답이 길수록 일이 는다
ANSWER_INTERVAL_SECONDS = 0.05


def answer_streaming_enabled() -> bool:
    """답 미리보기 스위치 (`KUKIE_STREAM_ANSWER=1`). 켜면 모델 요청이 스트리밍으로 바뀐다 — 답을 받는 방식이
    달라지므로 끄면 예전 그대로다. 실행·승인·저장 규칙은 켜도 그대로다."""
    return os.environ.get("KUKIE_STREAM_ANSWER", "").strip() in ("1", "true", "yes")


def _collect(args: dict[int, "str | dict[str, Any]"], event: AgentStreamEvent) -> str | None:
    """응답 조각 이벤트를 모으고, 그 조각이 답 양식이면 지금까지의 narration."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, ToolCallPart):
        args[event.index] = event.part.args if event.part.args is not None else ""
    elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, ToolCallPartDelta):
        delta, before = event.delta.args_delta, args.get(event.index, "")
        if isinstance(delta, str):
            args[event.index] = (before if isinstance(before, str) else json.dumps(before, ensure_ascii=False)) + delta
        elif isinstance(delta, dict):
            # dict 조각은 pydantic-ai 처럼 합친다 — 덮어쓰면 narration 이 담긴 앞 조각이 사라진다 (리뷰).
            # 앞이 미완성 JSON 문자열이면 지금까지 읽히는 만큼 dict 로 올려서 합친다 (str → dict 전이)
            if isinstance(before, str):
                try:
                    parsed = from_json(before, allow_partial="trailing-strings") if before else {}
                except ValueError:
                    parsed = {}
                before = parsed if isinstance(parsed, dict) else {}
            args[event.index] = {**before, **delta}
    else:
        return None
    current = args.get(event.index, "")
    if isinstance(current, dict):
        text = current.get("narration")
        return text if isinstance(text, str) and text else None
    return _narration(current)


def _narration(args: str) -> str | None:
    """답 양식 JSON(미완성일 수 있음)에서 narration. 도구 인자처럼 narration 이 없는 JSON 이면 None."""
    try:
        value = from_json(args, allow_partial="trailing-strings")
    except ValueError:
        return None
    text = value.get("narration") if isinstance(value, dict) else None
    return text if isinstance(text, str) and text else None


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


hub = ProgressHub()
