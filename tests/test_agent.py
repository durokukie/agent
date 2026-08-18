"""에이전트 조립 검증 — 툴 등록·스킬 필터·설명 강제 (DURO-42).

TestModel로 결정론적으로 돌린다 (실제 LLM 호출·API 키 없음).
"""
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel

from kukie.agent import agent
from kukie.deps import Deps
from kukie.skills import SKILLS


def _deps(skill_name: str = "학습") -> Deps:
    return Deps(context="kind-dev", namespace="study", skill=SKILLS[skill_name])


# ── 응답 픽스처 ─────────────────────────────────────────────

GOOD_STEP = {
    "step_label": "1/1 · 파드 목록",
    "access": "read-only",
    "command": "kubectl --context kind-dev get pods -n study -o wide",
    "output": "NAME  READY  STATUS\nnginx  1/1  Running",
    "explanations": [
        {"field": "get pods", "meaning": "파드 목록을 조회한다"},
        {"field": "-n study", "meaning": "study 네임스페이스 대상"},
        {"field": "-o wide", "meaning": "IP·노드 등 상세 열 포함"},
    ],
}


def _response(step: dict | None) -> dict:
    return {"narration": "결과입니다.", "steps": [step] if step else [],
            "suggested_transition": None}


# ── 1. 툴 등록 + 스킬 필터 ───────────────────────────────────

@pytest.mark.parametrize("skill,expected", [
    ("학습", {"list_resources", "explain_command"}),
    ("진단", {"list_resources", "explain_command",
              "describe_resource", "get_events", "get_logs"}),
    ("실습", {"list_resources", "explain_command", "describe_resource"}),
])
def test_스킬별로_허용된_툴만_LLM에게_노출된다(skill, expected):
    m = TestModel(call_tools=[], custom_output_args=_response(GOOD_STEP))
    with agent.override(model=m):
        agent.run_sync("파드 보여줘", deps=_deps(skill))
    exposed = {t.name for t in m.last_model_request_parameters.function_tools}
    assert exposed == expected


def test_변경_툴은_아직_등록되지_않았다():
    """훅 본체 완성 전까지 변경 툴은 어떤 스킬에서도 노출되면 안 된다 (승인 우회 방지)."""
    m = TestModel(call_tools=[], custom_output_args=_response(GOOD_STEP))
    with agent.override(model=m):
        agent.run_sync("nginx 지워줘", deps=_deps("실습"))
    exposed = {t.name for t in m.last_model_request_parameters.function_tools}
    assert not exposed & {"apply_manifest", "scale_resource",
                          "rollout_restart", "delete_resource"}


# ── 2. 설명 필드 강제 (output_validator) ─────────────────────

def test_설명이_있는_정상_응답은_통과한다():
    m = TestModel(call_tools=[], custom_output_args=_response(GOOD_STEP))
    with agent.override(model=m):
        result = agent.run_sync("파드 보여줘", deps=_deps())
    assert result.output.steps[0].explanations


def test_명령이_있는데_설명이_비면_반려된다():
    bad = {**GOOD_STEP, "explanations": []}
    m = TestModel(call_tools=[], custom_output_args=_response(bad))
    # TestModel은 같은 답을 반복하므로 ModelRetry가 재시도 한도를 넘겨 UnexpectedModelBehavior로 끝난다
    with agent.override(model=m), pytest.raises(UnexpectedModelBehavior):
        agent.run_sync("파드 보여줘", deps=_deps())


def test_플래그_설명이_누락되면_반려된다():
    partial = {**GOOD_STEP, "explanations": [
        {"field": "get pods", "meaning": "파드 목록"},   # -n, -o 설명 없음
    ]}
    m = TestModel(call_tools=[], custom_output_args=_response(partial))
    with agent.override(model=m), pytest.raises(UnexpectedModelBehavior):
        agent.run_sync("파드 보여줘", deps=_deps())


def test_명령을_실행하지_않은_응답은_설명_없이도_통과한다():
    """개념 설명만 한 턴(steps 비어 있음)은 검사 대상이 아니다."""
    m = TestModel(call_tools=[], custom_output_args=_response(None))
    with agent.override(model=m):
        result = agent.run_sync("Deployment가 뭐예요?", deps=_deps())
    assert result.output.steps == []
