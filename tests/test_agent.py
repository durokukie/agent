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


# ── 재시도 (DURO-66 ③·④) ────────────────────────────────────

def test_응답_형식_실패는_run_안에서_두번까지_흡수한다():
    """출력 검증 실패(B 유형)가 서버 503 까지 올라오기 전에 run 안에서 먼저 재시도한다."""
    assert agent._max_output_retries == 2
    assert agent._max_tool_retries == 1          # 툴 재시도 예산은 건드리지 않는다


def test_build_model은_test와_접두어_없는_이름을_그대로_둔다():
    from kukie.agent import _build_model

    assert _build_model("test") == "test"
    assert _build_model("gpt-4o") == "gpt-4o"    # 공급자 미지정 → pydantic-ai 가 해석


def _http_libs():
    """공급자 SDK 가 예외를 던질 수 있는 HTTP 라이브러리 — pydantic-ai 2.3x 는 httpx, 2.4x 는 httpx2 도.
    (httpx2 가 깔려 있어도 2.3x 의 SDK 는 httpx 로 통신하므로 agent 가 고른 목록을 그대로 쓴다.)"""
    from kukie import agent as agent_module

    return list(agent_module._HTTP_LIBS)


def test_build_model은_실제_공급자에_HTTP_재시도_전송층을_끼운다(monkeypatch, caplog):
    import pydantic_ai.retries as retries

    from kukie.agent import _build_model

    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    model = _build_model("anthropic:claude-sonnet-4-6")

    http_client = model.client._client                       # anthropic SDK 가 감싼 HTTP 클라이언트
    transports = tuple(
        cls for name in ("AsyncHTTPX2TenacityTransport", "AsyncTenacityTransport")
        if (cls := getattr(retries, name, None)) is not None
    )
    assert isinstance(http_client._transport, transports)   # 설치된 스택(httpx2 / httpx)에 맞는 재시도 전송층
    assert http_client.timeout.read == 600                   # 기본 5초는 LLM 에 너무 짧다
    assert http_client.timeout.connect == 5
    assert "HTTP 재시도 없이" not in caplog.text             # 폴백(재시도 없음)으로 빠지지 않았다


@pytest.mark.parametrize("lib", _http_libs(), ids=lambda lib: lib.__name__)
@pytest.mark.parametrize(("status", "expected"), [
    (429, True), (500, True), (503, True),          # 레이트리밋·서버 오류 → 다시 보내면 풀린다
    (400, False), (401, False), (404, False),       # 잘못된 요청·키 → 다시 보내도 같은 답
])
def test_HTTP_재시도는_429와_5xx만(lib, status, expected):
    from kukie.agent import _retryable

    request = lib.Request("POST", "https://api.example")
    error = lib.HTTPStatusError("x", request=request, response=lib.Response(status, request=request))
    assert _retryable(error) is expected


@pytest.mark.parametrize("lib", _http_libs(), ids=lambda lib: lib.__name__)
def test_HTTP_재시도는_네트워크와_타임아웃도_포함한다(lib):
    from kukie.agent import _retryable

    assert _retryable(lib.ReadTimeout("t")) is True
    assert _retryable(lib.ConnectError("c")) is True
    assert _retryable(ValueError("x")) is False
