"""승인 전 Action Plan에서 사용자의 추가 판단 항목을 생성한다."""
from __future__ import annotations

from pydantic_ai import Agent

from kukie.guardrail.action_plan import ActionPlan


INSTRUCTIONS = """너는 아직 승인되지 않은 Kubernetes Action Plan의 검토자다.
사용자가 승인 또는 거절하기 전에 추가로 확인하거나 판단해야 할 사항만 작성한다.

규칙:
- Action Plan에 기록된 정보만 근거로 사용한다.
- 승인 또는 거절 결론을 대신 내리지 않는다.
- 위험 등급을 다시 판정하거나 변경하지 않는다.
- Intent, Expected Effects, Side Effects를 단순 반복하지 않는다.
- 대상의 적절성, 영향 범위, 복구 가능성, 실행 시점, 확인할 전제 중 필요한 것만 쓴다.
- 최대 3개의 짧은 항목으로 작성한다.
- 추가 판단이 필요하지 않으면 '추가 판단 없음'이라고 작성한다.
- 설명문만 반환한다."""

guidance_agent = Agent(
    "anthropic:claude-sonnet-4-6",
    name="action-plan-decision-guidance",
    output_type=str,
    instructions=INSTRUCTIONS,
    defer_model_check=True,
)


async def generate_decision_guidance(plan: ActionPlan) -> str:
    plan.validate_for_decision_guidance()
    context = plan.render(include_decision_guidance=False)
    result = await guidance_agent.run(context)
    guidance = result.output.strip()
    if not guidance:
        raise ValueError("LLM returned empty decision guidance")
    return guidance
