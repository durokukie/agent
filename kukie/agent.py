"""에이전트 조립 — 프롬프트·툴·훅·검증기를 한곳에서 연결.

에이전트는 하나. 스킬(deps.skill)에 따라 프롬프트·툴·응답 스키마가 갈아끼워진다.
"""
from __future__ import annotations

from pydantic_ai import Agent, RunContext

from kukie.deps import Deps
from kukie.guardrail.hook import hooks as guardrail_hooks
from kukie.skills.base import KukieResponse
from kukie.validators import enforce_explanations

BASE_PROMPT = """너는 쿠버네티스를 처음 배우는 연수생을 돕는 조수 Kukie다.
1. kubectl 명령을 다룰 때는 각 플래그·필드의 의미를 explanations에 반드시 채운다.
2. '무엇을' 하는지와 '왜' 하는지를 항상 함께 설명한다.
3. 제공된 툴로 할 수 없는 작업은 실행하려 하지 말고, 사용자가 직접 터미널에
   실행할 kubectl 명령을 만들어 안내하고 각 플래그의 의미를 설명해라.
4. 클러스터를 변경하는 툴을 호출하면 시스템 가드레일이 자동 개입한다.
   위험도는 시스템이 판정한다 — 네가 판정하거나 우회하거나 실행됐다고 말하지 마라.
   승인은 사용자가 CLI에서 직접 입력해야 성립한다."""

agent = Agent(
    "anthropic:claude-sonnet-4-6",   # TODO: Model Adapter로 교체 가능하게 (Upstage 등)
    name="kukie",
    deps_type=Deps,
    output_type=KukieResponse,        # run마다 skill.output_type으로 오버라이드
    instructions=BASE_PROMPT,
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

# TODO: 툴 등록 — read.READ_TOOLS / mutate.MUTATE_TOOLS 를 @agent.tool로 연결
# TODO: prepare(툴 필터)는 MVP 미적용 (프롬프트 유도) — 논의사항 1 참고
