"""에이전트 조립 — 프롬프트·툴·훅·검증기를 한곳에서 연결.

에이전트는 하나. 스킬(deps.skill)에 따라 프롬프트·툴·응답 스키마가 갈아끼워진다.
"""
from __future__ import annotations

import os

from pydantic_ai import Agent, RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from kukie.deps import Deps
from kukie.guardrail.hook import hooks as guardrail_hooks
from kukie.skills.base import KukieResponse
from kukie.tools.read import READ_TOOLS
from kukie.validators import enforce_explanations

BASE_PROMPT = """너는 쿠버네티스를 처음 배우는 연수생을 돕는 조수 Kukie다.
1. kubectl 명령을 다룰 때는 각 플래그·필드의 의미를 explanations에 반드시 채운다.
2. '무엇을' 하는지와 '왜' 하는지를 항상 함께 설명한다.
3. 제공된 툴로 할 수 없는 작업은 실행하려 하지 말고, 사용자가 직접 터미널에
   실행할 kubectl 명령을 만들어 안내하고 각 플래그의 의미를 설명해라.
4. 클러스터를 변경하는 툴을 호출하면 시스템 가드레일이 자동 개입한다.
   위험도는 시스템이 판정한다 — 네가 판정하거나 우회하거나 실행됐다고 말하지 마라.
   승인은 사용자가 CLI에서 직접 입력해야 성립한다."""

# ── 툴 등록 ──────────────────────────────────────────────────
# 읽기 5종만 등록한다. 변경 4종은 가드레일 훅 본체가 완성된 뒤 추가 (마일스톤 2) —
# 훅 없이 등록하면 승인 없이 delete가 나갈 수 있다.
_read_toolset: FunctionToolset[Deps] = FunctionToolset(READ_TOOLS)


def _only_skill_tools(ctx: RunContext[Deps], tool_def: ToolDefinition) -> bool:
    """현재 스킬(deps.skill)에 허용된 툴만 LLM에게 노출한다 (스킬 = 프롬프트+툴+응답형식)."""
    return tool_def.name in ctx.deps.skill.allowed_tools


toolset = _read_toolset.filtered(_only_skill_tools)


# 모델은 환경변수로 지정한다. 미지정 시 'test'(TestModel) — 키 없이 import·테스트 가능.
#   예: KUKIE_MODEL=anthropic:claude-sonnet-4-6  (ANTHROPIC_API_KEY 필요)
# TODO: Model Adapter로 Upstage 등 교체 경계 정리 (Architecture.md 6.5)
MODEL = os.environ.get("KUKIE_MODEL", "test")

agent = Agent(
    MODEL,
    name="kukie",
    deps_type=Deps,
    output_type=KukieResponse,        # run마다 skill.output_type으로 오버라이드
    instructions=BASE_PROMPT,
    toolsets=[toolset],               # 스킬 필터를 거친 툴 목록
    capabilities=[guardrail_hooks],   # 가드레일 훅 장착
)


@agent.instructions
def add_target(ctx: RunContext[Deps]) -> str:
    """현재 작업 대상 주입 — 대화만으로 대상을 바꾸지 않는다."""
    return f"현재 작업 대상: context={ctx.deps.context}, namespace={ctx.deps.namespace}"


@agent.instructions
def add_skill_prompt(ctx: RunContext[Deps]) -> str:
    """현재 스킬의 전용 프롬프트 주입 (스킬 = 프롬프트+툴+응답형식)."""
    return ctx.deps.skill.prompt


agent.output_validator(enforce_explanations)
