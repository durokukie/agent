import os
from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from kukie.guardrail import action_plan
from kukie.guardrail import decision_guidance
from kukie.guardrail.action_plan import ActionPlan
from kukie.guardrail.decision_guidance import (
    generate_decision_guidance,
    guidance_agent,
)

def _ready_plan(monkeypatch, tmp_path: Path) -> ActionPlan:
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
            "context": "minikube",
            "namespace": "study",
            "kind": "deployment",
            "name": "nginx",
        },
        intent="nginx 레플리카를 늘린다.",
        expected_effects=["레플리카가 3개가 된다."],
        side_effects=["추가 Pod가 자원을 사용한다."],
    )
    plan.record_dry_run("succeeded", "dry-run ok", "")
    return plan


def test_guidance_agent는_설정된_모델과_reasoning_설정을_쓴다():
    """모델명을 리터럴로 단언하면 셸·.env 의 KUKIE_GUIDANCE_MODEL 에 따라 깨진다
    (CodeRabbit 이 지적한 격리 문제의 두 번째 자리 — 실제 로컬 .env 로 재현됨).
    불변식은 "agent 가 GUIDANCE_MODEL 설정값을 쓴다"이고, 기본값 자체는
    test_판단_가이드_모델_기본값은_기존_동작을_유지한다 가 검증한다."""
    assert guidance_agent.model == decision_guidance.GUIDANCE_MODEL
    assert guidance_agent.model_settings == {"openai_reasoning_effort": "medium"}


def test_테스트_suite는_실제_모델_요청을_금지한다():
    assert models.ALLOW_MODEL_REQUESTS is False


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
async def test_판단_보조_모델은_동적으로_조합한_Markdown을_받는다(
    monkeypatch, tmp_path
):
    plan = _ready_plan(monkeypatch, tmp_path)
    received_context = ""

    def capture_context(messages, info):
        nonlocal received_context
        received_context = str(messages)
        return ModelResponse(parts=[TextPart("추가 판단 없음")])

    with guidance_agent.override(model=FunctionModel(capture_context)):
        await generate_decision_guidance(plan)

    assert "# Action Plan" in received_context
    assert "## Expected Effects" in received_context
    assert received_context.count(plan.intent) == 1
    assert "decision_guidance" not in received_context


@pytest.mark.asyncio
async def test_generate_decision_guidance_loads_openai_key_from_dotenv(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=test-openai-key\n",
        encoding="utf-8",
    )
    plan = _ready_plan(monkeypatch, tmp_path)

    with guidance_agent.override(model=TestModel(custom_output_text="추가 판단 없음")):
        await generate_decision_guidance(plan)

    assert os.environ["OPENAI_API_KEY"] == "test-openai-key"


@pytest.mark.asyncio
async def test_generate_decision_guidance_validates_before_model_call(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    plan = ActionPlan.create_draft(
        call_id="call-123",
        tool="scale_resource",
        args={"kind": "deployment", "name": "nginx", "replicas": 3},
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
        with pytest.raises(ValueError, match="dry_run_result.status must be succeeded"):
            await generate_decision_guidance(plan)


@pytest.mark.asyncio
async def test_generate_decision_guidance_rejects_empty_output(monkeypatch, tmp_path):
    plan = _ready_plan(monkeypatch, tmp_path)

    with guidance_agent.override(model=TestModel(custom_output_text="   ")):
        with pytest.raises(ValueError, match="LLM returned empty decision guidance"):
            await generate_decision_guidance(plan)


def test_판단_가이드_모델은_환경변수로_바꿀_수_있다(monkeypatch):
    """메인 모델과 다른 제공사에 고정돼 있으면 키를 두 벌 요구하게 된다.
    실패해도 훅이 삼켜서 조용히 빈칸이 되는 자리라 설정 가능해야 한다."""
    import importlib

    monkeypatch.setenv("KUKIE_GUIDANCE_MODEL", "openrouter:openai/gpt-5-mini")
    reloaded = importlib.reload(decision_guidance)
    try:
        assert reloaded.GUIDANCE_MODEL == "openrouter:openai/gpt-5-mini"
    finally:
        monkeypatch.delenv("KUKIE_GUIDANCE_MODEL", raising=False)
        importlib.reload(decision_guidance)   # 다른 테스트를 위해 원상 복구


def test_판단_가이드_모델_기본값은_기존_동작을_유지한다(monkeypatch):
    """셸이나 .env 에 KUKIE_GUIDANCE_MODEL 이 있어도 기본값 검증이 흔들리지 않게
    변수를 지우고 실제 읽기 함수를 다시 부른다 (CodeRabbit 지적). 모듈 reload 대신
    읽기 로직을 _guidance_model() 로 빼서 검증한다 — reload 는 guidance_agent 를
    새로 만들어 다른 모듈이 들고 있는 참조와 어긋날 수 있다."""
    monkeypatch.delenv("KUKIE_GUIDANCE_MODEL", raising=False)
    assert decision_guidance._guidance_model() == "openai:gpt-5.6-luna"


def test_판단_가이드_모델은_빈_환경변수를_미설정과_같게_본다(monkeypatch):
    """.env 템플릿을 그대로 복사해 `KUKIE_GUIDANCE_MODEL=` 빈 값이 등록돼도
    Agent("") 가 import 시점에 UserError 로 터지면 안 된다 — `or` 기본값 회귀 방지."""
    monkeypatch.setenv("KUKIE_GUIDANCE_MODEL", "")
    assert decision_guidance._guidance_model() == "openai:gpt-5.6-luna"
