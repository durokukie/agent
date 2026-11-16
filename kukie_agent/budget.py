"""요청 예산과 강제 답 — 모델 요청 N번째에는 도구를 빼고 답 양식만 준다 (Kukie Model 제품 설정 고르기).

기본은 꺼져 있다. 켜는 길은 둘이다.
  - 환경변수 `KUKIE_REQUEST_BUDGET=N` — agent 전체(모든 run)에 건다 (agent.py 가 읽는다)
  - `agent.run(..., capabilities=[RequestBudget(N)])` — 그 run 에만 건다 (평가 harness 가 쓴다)

N 은 한 run(사용자 발화 하나)의 모델 요청 수 상한이다. 요청 1~N-1 은 평소와 같고, 요청 N 은 이렇게 바뀐다.
  1. 도구 목록이 빈다 (prepare_tools). 답 양식(출력 도구)만 남으므로 모델은 답을 낼 수밖에 없다.
  2. 그 요청의 끝(마지막 ModelRequest)에 사용자 문장 FORCED_ANSWER_PROMPT 를 붙인다. 기록에도 남는다.
  3. 그 답에 finding 이 없거나(null) 양식이 틀려 재시도가 필요하면 run 을 ForcedAnswerFailed 로 끝낸다.
     빈 finding 으로 끝나 "답했다" 로 보이지 않게 하려는 것이다. 평가에서는 답 없음(오답)으로 센다.

문장은 사전 등록(kukie-model `eval/PREREG.md` §3.3) 분석 호출과 같은 문장이다. 그 호출로 잰 "상한 뒤 답" 을
제품 경로에서 그대로 재현하는 것이 목적이라 문장을 바꾸지 않는다.

예산 밖 요청 상한(`UsageLimits.request_limit`, pydantic-ai 기본 50)이 N 보다 작으면 그쪽이 먼저 걸린다.
N 은 2 이상 50 이하로 받는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart, UserPromptPart
from pydantic_ai.tools import ToolDefinition

ENV = "KUKIE_REQUEST_BUDGET"
MAX_BUDGET = 50   # pydantic-ai UsageLimits 기본 request_limit — 이보다 크면 강제 답 턴 전에 상한이 먼저 걸린다

#: 강제 답 턴에 붙이는 사용자 문장. 같은 턴에서 이미 붙였는지·강제 답 턴인지 이 문장으로 찾는다
FORCED_ANSWER_PROMPT = "지금까지 본 근거로 최종 답을 내라. 도구는 더 부르지 마라."


class ForcedAnswerFailed(RuntimeError):
    """강제 답 턴에서 쓸 수 있는 답을 받지 못했다 (finding 이 null 이거나 양식이 틀려 다시 물어야 함)."""


@dataclass
class RequestBudget(AbstractCapability[Any]):
    """요청 N번째를 강제 답 턴으로 만든다. 상태가 없어 여러 run 이 같은 인스턴스를 써도 된다."""

    requests: int

    def __post_init__(self) -> None:
        if not 2 <= self.requests <= MAX_BUDGET:
            raise ValueError(f"요청 예산은 2~{MAX_BUDGET} 이어야 한다: {self.requests}")

    def _is_forced_turn(self, ctx: RunContext[Any]) -> bool:
        # ctx.usage.requests 는 지금까지 끝난(응답이 기록된) 요청 수다. 다음 요청 번호 = requests + 1
        return ctx.usage.requests + 1 >= self.requests

    async def prepare_tools(self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        if ctx.usage.requests >= self.requests:
            # 강제 답 턴(N)이 끝났는데 또 요청하려 한다 — 양식 오류·없는 도구 호출·finding 되묻기로 재시도가 났다
            raise ForcedAnswerFailed(f"요청 예산 {self.requests} 의 강제 답 턴에서 쓸 수 있는 답을 받지 못했다")
        if self._is_forced_turn(ctx):
            return []   # 함수 도구만 받는다. 출력 도구(답 양식)는 prepare_output_tools 쪽이라 그대로 남는다
        return tool_defs

    async def before_model_request(self, ctx: RunContext[Any], request_context: Any) -> Any:
        if not self._is_forced_turn(ctx):
            return request_context
        messages = list(request_context.messages)
        last = messages[-1] if messages else None
        if not isinstance(last, ModelRequest) or is_forced_request(last):
            return request_context
        messages[-1] = replace(last, parts=[*last.parts, UserPromptPart(content=FORCED_ANSWER_PROMPT)])
        # pydantic-ai ProcessHistory 와 같은 방식 — 이번 요청과 기록(ctx.messages) 둘 다 바꿔 대화 기록에 문장이 남게 한다.
        # 맨 끝 요청은 이번 run 에서 생긴 것이라 new_messages() 에도 잡힌다 (gitops/memo.py 와 같은 이유)
        request_context.messages = messages.copy()
        ctx.messages[:] = messages
        return request_context


def is_forced_request(message: ModelMessage) -> bool:
    """강제 답 문장이 붙은 요청인가. 툴 결과·재시도 문구와 같은 요청에 붙은 것만 센다 —
    사용자가 같은 문장을 새 질문으로 보내면(사용자 문장만 든 요청) 강제 답이 아니라 새 턴이다."""
    if not isinstance(message, ModelRequest):
        return False
    parts = message.parts
    return (any(isinstance(p, UserPromptPart) and p.content == FORCED_ANSWER_PROMPT for p in parts)
            and any(isinstance(p, ToolReturnPart | RetryPromptPart) for p in parts))


def forced_answer_given(messages: list[ModelMessage]) -> bool:
    """이 메시지들(보통 response._current_turn 의 이번 턴) 안에 강제 답 문장을 붙인 요청이 있나."""
    return any(is_forced_request(msg) for msg in messages)


def from_env() -> list[RequestBudget]:
    """`KUKIE_REQUEST_BUDGET` 이 비어 있으면 빈 목록(꺼짐). 숫자가 아니거나 범위 밖이면 import 때 바로 실패한다."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return []
    return [RequestBudget(int(raw))]
