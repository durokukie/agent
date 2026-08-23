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
from kukie.response import build_response
from kukie.tools.read import READ_TOOLS

BASE_PROMPT = """너는 쿠버네티스를 처음 배우는 연수생을 돕는 조수 Kukie다.
1. 실행한 명령·결과·플래그 설명은 시스템이 자동으로 화면에 붙인다 — 너는 narration에서
   그 결과가 무엇을 뜻하는지 연수생 눈높이로 풀어 설명해라. 명령어를 다시 옮겨 적지 마라.
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
    output_type=build_response,       # 함수 output_type — LLM은 narration 등 해석 칸만, steps는 코드가 (DURO-44)
                                      # 스킬 특화 응답은 run마다 output_type=skill.output_fn 으로 오버라이드
    instructions=BASE_PROMPT,
    toolsets=[toolset],               # 스킬 필터를 거친 툴 목록
    capabilities=[guardrail_hooks],   # 가드레일 훅 장착
)


# ── 동적 프롬프트 (등록형 함수) ────────────────────────────────
# 아래 두 함수는 코드 어디에서도 직접 호출하지 않는다. 그런데도 매 run마다 실행된다.
#
# 이유는 `@agent.instructions` 데코레이터 때문이다.
#   @agent.instructions
#   def f(ctx): ...
# 는 사실
#   def f(ctx): ...
#   f = agent.instructions(f)
# 와 같다. 즉 정의 직후 f를 agent.instructions()에 넘겨서 "이 함수를 프롬프트 생성기로
# 등록해라"라고 알려주는 것. 그 뒤로는 pydantic-ai가 run을 시작할 때마다 등록된 함수를
# 전부 호출해 반환 문자열을 BASE_PROMPT 뒤에 이어 붙인다 (agent-flow.md 그림1 ①).
#
# 그래서 grep으로 호출부를 찾으면 안 나오지만 "미사용"이 아니다 — 지우면 그 프롬프트가
# 조용히 사라진다. add_skill_prompt를 지우면 학습/진단/실습이 전부 똑같이 행동한다.
#
# 이 파일에서 같은 원리로 동작하는 등록형 함수:
#   @agent.instructions      → 프롬프트 생성기 (아래 둘)
#   FunctionToolset(...)     → 툴 (READ_TOOLS의 함수들; LLM이 이름으로 호출)
#   @hooks.on.tool_execute   → 훅 (guardrail/hook.py의 guardrail())
#   output_type=build_response → 최종 응답 조립기 (response.py). 함수라서 LLM 스키마는
#     매개변수(narration, suggested_transition)뿐이고, steps는 본문에서 코드가 실행 기록으로 채운다.
#     사전에 없는 플래그는 validators.log_unregistered_flags가 로그만 남긴다 (반려 없음).
#
# 함수형(문자열 대신 함수)으로 두는 이유: BASE_PROMPT는 고정값이라 문자열로 충분하지만,
# 아래 둘은 run마다 달라지는 값(현재 대상, 현재 스킬)을 ctx.deps에서 읽어야 하므로
# 실행 시점에 계산돼야 한다.

@agent.instructions
def add_target(ctx: RunContext[Deps]) -> str:
    """현재 작업 대상 주입 — 대화만으로 대상을 바꾸지 않는다.

    매 run 시작 시 pydantic-ai가 자동 호출. 반환값이 시스템 프롬프트에 추가된다.
    """
    return f"현재 작업 대상: context={ctx.deps.context}, namespace={ctx.deps.namespace}"


@agent.instructions
def add_skill_prompt(ctx: RunContext[Deps]) -> str:
    """현재 스킬의 전용 프롬프트 주입 (스킬 = 프롬프트+툴+응답형식).

    매 run 시작 시 pydantic-ai가 자동 호출. deps.skill이 학습이면 학습 프롬프트,
    진단이면 진단 프롬프트가 붙는다 — 이게 "에이전트 하나로 모드를 갈아끼우는" 장치.
    """
    return ctx.deps.skill.prompt
