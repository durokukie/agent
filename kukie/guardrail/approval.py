"""Plan 기반 Electron 승인 DTO와 대기 요청 응답 검증."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ToolCallPart

from kukie.guardrail.action_plan import ActionPlan, PlanTarget
from kukie.guardrail.mutation_request import canonicalize_mutation_args


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    plan_id: str
    tool: str
    target: PlanTarget
    command: list[str]
    risk: str
    intent: str
    expected_effects: list[str]
    side_effects: list[str]
    dry_run_result: dict[str, object]
    decision_guidance: str


def build_approval_request(
    call: ToolCallPart,
    metadata: dict[str, Any],
    *,
    default_namespace: str,
) -> ApprovalRequest:
    call_id = call.tool_call_id
    if not call_id:
        raise ValueError("pending approval mismatch: missing call_id")
    plan = ActionPlan.find_by_call_id(call_id)
    canonical = canonicalize_mutation_args(
        call.tool_name,
        call.args_as_dict(raise_if_invalid=True),
        default_namespace,
    )
    if (
        metadata.get("plan_id") != plan.id
        or call.tool_name != plan.tool
        or canonical.plan != plan.args
        or canonical.normalized.get("intent") != plan.intent
        or canonical.normalized.get("expected_effects") != plan.expected_effects
        or canonical.normalized.get("side_effects") != plan.side_effects
        or plan.status != "draft"
        or not isinstance(plan.dry_run_result, dict)
        or plan.dry_run_result.get("status") not in {"succeeded", "unsupported"}
        or not plan.decision_guidance
    ):
        raise ValueError("pending approval mismatch")
    return ApprovalRequest(
        tool_call_id=call_id,
        plan_id=plan.id,
        tool=plan.tool,
        target=plan.target,
        command=plan.command,
        risk=plan.risk_level,
        intent=plan.intent,
        expected_effects=plan.expected_effects,
        side_effects=plan.side_effects,
        dry_run_result=plan.dry_run_result,
        decision_guidance=plan.decision_guidance,
    )
