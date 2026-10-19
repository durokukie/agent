"""대화의 진행 상황 — run 이 도는 동안 "지금 무엇을 하는지" 를 화면에 흘려 보낸다.

`POST /chat`·`/approve`·`/resume` 은 그대로 끝난 뒤 한 번에 답한다 (request_id 재전송·run 상태 규칙을 건드리지 않는다).
화면은 기다리는 동안 따로 `GET /conversations/{id}/progress` 를 열어 단계를 받는다. 이 모듈은 그 사이의 우체통이다.

- 단계는 도구 실행 훅에서 만든다 (`agent.run(capabilities=[hub.capability(방)])`).
  도구가 돌기 직전 `step_start`(무엇을 하려는지), 끝나면 `step_end`(실제로 돈 kubectl 명령·성공 여부).
- 방마다 지금 run 의 단계를 모아 둔다. 늦게 연결한 화면도 앞 단계를 다시 받는다.
- 메모리에만 둔다 — 에이전트는 한 대이고(세션도 메모리), 진행 상황은 run 이 끝나면 쓸모가 없다.
"""
from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

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
    "apply_manifest": "매니페스트 적용 준비",
    "scale_resource": "{kind}/{name} 개수 바꾸기 준비",
    "rollout_restart": "{kind}/{name} 재시작 준비",
    "delete_resource": "{kind}/{name} 삭제 준비",
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
    done: bool = False
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)


class ProgressHub:
    """방(대화 id) 마다 지금 run 의 단계를 모으고, 연결한 화면에 흘려 보낸다."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}
        self._started: dict[str, asyncio.Event] = {}

    def _signal(self, key: str) -> asyncio.Event:
        return self._started.setdefault(key, asyncio.Event())

    async def begin(self, key: str) -> None:
        """새 run 을 연다. 앞 run 의 단계는 버린다."""
        self._runs[key] = _Run()
        self._signal(key).set()

    async def publish(self, key: str, event: dict[str, Any]) -> None:
        run = self._runs.get(key)
        if run is None or run.done:
            return
        async with run.changed:
            run.events.append(event)
            run.changed.notify_all()

    async def end(self, key: str) -> None:
        run = self._runs.get(key)
        if run is None:
            return
        async with run.changed:
            run.done = True
            run.changed.notify_all()
        self._signal(key).clear()

    async def stream(self, key: str) -> AsyncIterator[str]:
        """SSE 본문. 지금 run 이 있으면 지난 단계부터, 없으면 시작을 잠깐 기다린다. run 이 끝나면 `done` 으로 닫는다."""
        run = self._runs.get(key)
        if run is None or run.done:
            try:
                await asyncio.wait_for(self._signal(key).wait(), WAIT_FOR_RUN_SECONDS)
            except TimeoutError:
                yield _sse({"type": "done"})
                return
            run = self._runs[key]
        sent = 0
        while True:
            async with run.changed:
                if sent == len(run.events) and not run.done:
                    try:
                        await asyncio.wait_for(run.changed.wait(), HEARTBEAT_SECONDS)
                    except TimeoutError:
                        pass
                pending, finished = run.events[sent:], run.done
            for event in pending:
                yield _sse(event)
            sent += len(pending)
            if finished and sent == len(run.events):
                yield _sse({"type": "done"})
                return
            if not pending:
                yield ": keep-alive\n\n"

    def capability(self, key: str) -> "_Progress":
        """`agent.run(capabilities=[...])` 에 넘길 것 — 이 run 의 도구 실행을 이 방의 단계로 올린다."""
        return _Progress(self, key)


@dataclass
class _Progress(AbstractCapability[Any]):
    """도구 실행 앞뒤에서 단계를 올린다. 모델 요청은 건드리지 않는다.

    `agent.run(event_stream_handler=...)` 로도 같은 일을 할 수 있지만, 그러면 Pydantic AI 가 모델 요청을 스트리밍으로
    바꾼다 — 답을 받는 방식이 바뀌는 셈이라 도구 훅만 쓴다. 가드레일이 승인으로 미룬 변경 도구는 실행되지 않으므로
    단계도 남지 않는다(승인 카드가 대신 뜬다).
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
        command = getattr(result, "command", None)       # 읽기 도구는 KubectlResult 를 돌려준다
        success = getattr(result, "success", None)
        await self.hub.publish(self.key, {
            "type": "step_end",
            "id": call.tool_call_id,
            "command": display_command(command) if isinstance(command, str) else None,
            "ok": success if isinstance(success, bool) else True,
        })
        return result

    async def on_tool_execute_error(self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition,
                                    args: Any, error: Exception) -> Any:
        await self.hub.publish(self.key, {"type": "step_end", "id": call.tool_call_id, "command": None, "ok": False})
        raise error


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


hub = ProgressHub()
