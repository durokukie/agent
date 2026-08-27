"""에이전트 조립 검증 — 툴 등록·스킬 필터 (DURO-42).

TestModel로 결정론적으로 돌린다 (실제 LLM 호출·API 키 없음).

응답 조립(steps를 코드가 채우는 것)은 tests/test_response.py 에서 검증한다 (DURO-44).
"""
import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import (
    DeferredToolRequests,
    DeferredToolResults,
    ToolApproved,
    ToolDenied,
)

from kukie.agent import agent
from kukie.deps import Deps
from kukie.guardrail import action_plan, hook
from kukie.guardrail.action_plan import ActionPlan
from kukie.kubectl import KubectlResult
from kukie.response import build_response
from kukie.skills import SKILLS
from kukie.tools import mutate
from kukie.tools.mutate import MUTATING_TOOLS


def _deps(skill_name: str = "학습") -> Deps:
    return Deps(context="kind-dev", namespace="study", skill=SKILLS[skill_name])


# LLM이 채우는 칸만 — steps 는 스키마에 없다 (코드가 채움)
GOOD_RESPONSE = {
    "narration": "결과입니다.",
    "suggested_transition": None,
}


# ── 툴 등록 + 스킬 필터 ─────────────────────────────────────

@pytest.mark.parametrize("skill,expected", [
    ("학습", {"list_resources", "explain_command"}),
    ("진단", {"list_resources", "explain_command",
              "describe_resource", "get_events", "get_logs"}),
    ("실습", {"list_resources", "explain_command", "describe_resource"}
             | MUTATING_TOOLS),
])
def test_스킬별로_허용된_툴만_LLM에게_노출된다(skill, expected):
    m = TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)
    with agent.override(model=m):
        agent.run_sync("파드 보여줘", deps=_deps(skill))
    exposed = {t.name for t in m.last_model_request_parameters.function_tools}
    assert exposed == expected


MUTATION_CASES = [
    (
        "apply_manifest",
        {
            "manifest_yaml": "kind: Pod\nmetadata:\n  name: nginx\n",
            "namespace": "study",
            "intent": "nginx Pod를 만든다.",
            "expected_effects": ["Pod가 생성된다."],
            "side_effects": ["클러스터 자원을 사용한다."],
        },
    ),
    (
        "scale_resource",
        {
            "kind": "deployment",
            "name": "nginx",
            "replicas": 3,
            "namespace": "study",
            "intent": "replica를 늘린다.",
            "expected_effects": ["replica가 3개가 된다."],
            "side_effects": ["자원 사용량이 늘어난다."],
        },
    ),
    (
        "rollout_restart",
        {
            "kind": "deployment",
            "name": "nginx",
            "namespace": "study",
            "intent": "배포를 재시작한다.",
            "expected_effects": ["Pod가 순차 교체된다."],
            "side_effects": ["일시적으로 가용 Pod 수가 줄 수 있다."],
        },
    ),
    (
        "delete_resource",
        {
            "kind": "deployment",
            "name": "nginx",
            "namespace": "study",
            "intent": "실습 배포를 삭제한다.",
            "expected_effects": ["Deployment가 삭제된다."],
            "side_effects": ["서비스가 중단된다."],
        },
    ),
]


@pytest.mark.parametrize(("tool_name", "args"), MUTATION_CASES)
def test_등록된_mutation은_모두_Hook을_거쳐_Deferred요청이된다(
    monkeypatch, tmp_path, tool_name, args
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(
        hook,
        "run_kubectl",
        lambda *call_args, **call_kwargs: KubectlResult(
            command="kubectl dry-run",
            stdout="ok\n",
            stderr="",
            success=True,
        ),
    )

    async def fixed_guidance(plan):
        return "대상과 롤백 기준을 확인한다."

    monkeypatch.setattr(hook, "generate_decision_guidance", fixed_guidance)

    def forbidden_handler(*args, **kwargs):
        pytest.fail("승인 전에 mutation handler를 실행하면 안 된다")

    monkeypatch.setattr(mutate, "run_kubectl", forbidden_handler)

    def model_call(messages, info):
        return ModelResponse(parts=[ToolCallPart(
            tool_name=tool_name,
            args=args,
            tool_call_id=f"call-{tool_name}",
        )])

    with agent.override(model=FunctionModel(model_call)):
        result = agent.run_sync("변경해줘", deps=_deps("실습"))

    assert isinstance(result.output, DeferredToolRequests)
    assert result.output.approvals[0].tool_name == tool_name
    assert ActionPlan.find_by_call_id(f"call-{tool_name}").status == "draft"


def test_ToolApproved_재개는_기존_history와_handler를_한번_사용한다(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(
        hook,
        "run_kubectl",
        lambda *args, **kwargs: KubectlResult(
            command="kubectl dry-run",
            stdout="ok\n",
            stderr="",
            success=True,
        ),
    )

    async def fixed_guidance(plan):
        return "대상과 롤백 기준을 확인한다."

    monkeypatch.setattr(hook, "generate_decision_guidance", fixed_guidance)
    executions = []

    def actual_run(command, **kwargs):
        executions.append((command, kwargs))
        return KubectlResult(
            command="kubectl scale deployment nginx --replicas=3 -n study",
            stdout="scaled\n",
            stderr="",
            success=True,
            exit_code=0,
        )

    monkeypatch.setattr(mutate, "run_kubectl", actual_run)

    def model_call(messages, info):
        return ModelResponse(parts=[ToolCallPart(
            tool_name="scale_resource",
            args=dict(MUTATION_CASES[1][1]),
            tool_call_id="call-123",
        )])

    with agent.override(model=FunctionModel(model_call)):
        pending_result = agent.run_sync("변경해줘", deps=_deps("실습"))
    history = pending_result.all_messages()

    with agent.override(model=TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)):
        resumed = agent.run_sync(
            deps=_deps("실습"),
            output_type=[SKILLS["실습"].output_fn, DeferredToolRequests],
            message_history=history,
            deferred_tool_results=DeferredToolResults(
                approvals={"call-123": ToolApproved()}
            ),
        )

    assert resumed.output.narration
    assert len(executions) == 1
    assert ActionPlan.find_by_call_id("call-123").status == "executed"


def test_ToolDenied_재개는_2차Hook과_handler를_호출하지않는다(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(
        hook,
        "run_kubectl",
        lambda *args, **kwargs: KubectlResult(
            command="kubectl dry-run",
            stdout="ok\n",
            stderr="",
            success=True,
        ),
    )

    async def fixed_guidance(plan):
        return "대상과 롤백 기준을 확인한다."

    monkeypatch.setattr(hook, "generate_decision_guidance", fixed_guidance)

    def forbidden_handler(*args, **kwargs):
        pytest.fail("거절 전·후에 mutation handler나 2차 Hook을 실행하면 안 된다")

    monkeypatch.setattr(mutate, "run_kubectl", forbidden_handler)

    def model_call(messages, info):
        return ModelResponse(parts=[ToolCallPart(
            tool_name="scale_resource",
            args=dict(MUTATION_CASES[1][1]),
            tool_call_id="call-123",
        )])

    with agent.override(model=FunctionModel(model_call)):
        pending_result = agent.run_sync("변경해줘", deps=_deps("실습"))
    assert isinstance(pending_result.output, DeferredToolRequests)

    monkeypatch.setattr(hook, "run_kubectl", forbidden_handler)

    with agent.override(model=TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)):
        resumed = agent.run_sync(
            deps=_deps("실습"),
            output_type=[SKILLS["실습"].output_fn, DeferredToolRequests],
            message_history=pending_result.all_messages(),
            deferred_tool_results=DeferredToolResults(
                approvals={"call-123": ToolDenied("사용자가 거절했습니다.")}
            ),
        )

    assert resumed.output.narration


def test_응답은_KukieResponse_형식으로_강제된다():
    m = TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)
    with agent.override(model=m):
        result = agent.run_sync("파드 보여줘", deps=_deps())
    assert result.output.narration
    assert result.output.steps == []


def test_Agent는_일반응답과_Deferred요청을_output으로_허용한다():
    assert agent.output_type == [build_response, DeferredToolRequests]
