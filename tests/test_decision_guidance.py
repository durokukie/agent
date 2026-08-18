from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from kukie.guardrail import action_plan
from kukie.guardrail.action_plan import ActionPlan
from kukie.guardrail.decision_guidance import (
    generate_decision_guidance,
    guidance_agent,
)

models.ALLOW_MODEL_REQUESTS = False


def _ready_plan(monkeypatch, tmp_path: Path) -> ActionPlan:
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    plan = ActionPlan.create_draft(
        tool="scale_resource",
        command=["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        risk="caution",
        skill="실습",
        target={
            "context": "minikube",
            "namespace": "study",
            "kind": "deployment",
            "name": "nginx",
        },
        intent="nginx 레플리카를 늘린다.",
        expected_effects=["레플리카가 3개가 된다."],
        side_effects=["추가 Pod가 자원을 사용한다."],
    )
    plan.record_dry_run("dry-run ok", True)
    return plan


@pytest.mark.asyncio
async def test_generate_decision_guidance_returns_stripped_text(monkeypatch, tmp_path):
    plan = _ready_plan(monkeypatch, tmp_path)

    with guidance_agent.override(
        model=TestModel(custom_output_text="  실행 시간과 롤백 기준을 확인한다.  ")
    ):
        guidance = await generate_decision_guidance(plan)

    assert guidance == "실행 시간과 롤백 기준을 확인한다."
    metadata, _ = plan._read()
    assert metadata["decision_guidance"] is None


@pytest.mark.asyncio
async def test_generate_decision_guidance_validates_before_model_call(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    plan = ActionPlan.create_draft(
        tool="scale_resource",
        command=["scale", "deployment", "nginx", "--replicas=3"],
        risk="caution",
        skill="실습",
        target={"context": "minikube", "namespace": "study"},
        intent="nginx 레플리카를 늘린다.",
        expected_effects=["레플리카가 3개가 된다."],
        side_effects=["추가 Pod가 자원을 사용한다."],
    )

    def unexpected_model_call(messages, info):
        pytest.fail("검증 실패 뒤 모델이 호출됨")

    with guidance_agent.override(model=FunctionModel(unexpected_model_call)):
        with pytest.raises(ValueError, match="dry_run_result.success must be true"):
            await generate_decision_guidance(plan)


@pytest.mark.asyncio
async def test_generate_decision_guidance_rejects_empty_output(monkeypatch, tmp_path):
    plan = _ready_plan(monkeypatch, tmp_path)

    with guidance_agent.override(model=TestModel(custom_output_text="   ")):
        with pytest.raises(ValueError, match="LLM returned empty decision guidance"):
            await generate_decision_guidance(plan)
