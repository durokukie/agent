import pytest
from pydantic_ai.messages import ToolCallPart

from kukie.guardrail import action_plan
from kukie.guardrail.action_plan import ActionPlan
from kukie.guardrail.approval import ApprovalRequest, build_approval_request


CALL_ARGS = {
    "kind": "deployment",
    "name": "nginx",
    "replicas": 3,
    "namespace": "study",
    "intent": "nginx 레플리카를 늘린다.",
    "expected_effects": ["레플리카가 3개가 된다."],
    "side_effects": ["추가 노드 자원을 사용한다."],
}


def _ready_plan(
    monkeypatch,
    tmp_path,
    dry_run_status: str = "succeeded",
) -> ActionPlan:
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    plan = ActionPlan.create_draft(
        call_id="call-123",
        tool="scale_resource",
        args={
            "kind": "deployment",
            "name": "nginx",
            "replicas": 3,
            "namespace": "study",
        },
        command=["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        risk="caution",
        skill="실습",
        target={
            "context": "kind-dev",
            "namespace": "study",
            "kind": "deployment",
            "name": "nginx",
        },
        intent=CALL_ARGS["intent"],
        expected_effects=CALL_ARGS["expected_effects"],
        side_effects=CALL_ARGS["side_effects"],
    )
    stdout = "deployment.apps/nginx configured\n" if dry_run_status == "succeeded" else ""
    stderr = "server does not support dry run\n" if dry_run_status == "unsupported" else ""
    plan.record_dry_run(dry_run_status, stdout, stderr)
    plan.record_decision_guidance("현재 replica와 가용 자원을 확인한다.")
    return plan


@pytest.mark.parametrize("dry_run_status", ["succeeded", "unsupported"])
def test_Plan과_pending_call로_승인_DTO를_만든다(
    monkeypatch, tmp_path, dry_run_status
):
    plan = _ready_plan(monkeypatch, tmp_path, dry_run_status)
    call = ToolCallPart(
        tool_name="scale_resource",
        args=CALL_ARGS,
        tool_call_id="call-123",
    )

    dto = build_approval_request(
        call,
        {"plan_id": plan.id},
        default_namespace="study",
    )

    assert dto.tool_call_id == "call-123"
    assert dto.plan_id == plan.id
    assert dto.tool == "scale_resource"
    assert dto.risk == "caution"
    assert dto.command == plan.command
    assert dto.target == plan.target
    assert dto.dry_run_result["status"] == dry_run_status
    assert dto.decision_guidance == "현재 replica와 가용 자원을 확인한다."
    assert set(dto.model_dump()) == {
        "tool_call_id",
        "plan_id",
        "tool",
        "target",
        "command",
        "risk",
        "intent",
        "expected_effects",
        "side_effects",
        "dry_run_result",
        "decision_guidance",
    }
    assert ApprovalRequest.model_validate_json(dto.model_dump_json()) == dto


@pytest.mark.parametrize(
    ("tool", "args", "metadata"),
    [
        ("rollout_restart", CALL_ARGS, {"plan_id": "ap-unused"}),
        ("scale_resource", {**CALL_ARGS, "replicas": 4}, {"plan_id": "ap-unused"}),
        (
            "scale_resource",
            {**CALL_ARGS, "intent": "다른 작업을 수행한다."},
            {"plan_id": "ap-unused"},
        ),
        ("scale_resource", CALL_ARGS, {"plan_id": "wrong-plan"}),
    ],
)
def test_pending_tool_args_plan_id가_다르면_DTO를_거부한다(
    monkeypatch, tmp_path, tool, args, metadata
):
    plan = _ready_plan(monkeypatch, tmp_path)
    if metadata["plan_id"] == "ap-unused":
        metadata = {"plan_id": plan.id}
    call = ToolCallPart(tool_name=tool, args=args, tool_call_id="call-123")

    with pytest.raises(ValueError, match="pending approval mismatch"):
        build_approval_request(call, metadata, default_namespace="study")
