"""에이전트 조립 검증 — 툴 등록·스킬 필터 (DURO-42).

TestModel로 결정론적으로 돌린다 (실제 LLM 호출·API 키 없음).

검증기(enforce_explanations) 테스트는 제거됨 — explanations를 코드(FLAG_GLOSSARY)가
채우는 방향 확정(DURO-44)으로 LLM 감시형 검증기를 이 PR에서 뺐다.
DURO-44에서 "사전 미등록 플래그 로그" 테스트로 대체된다.
"""
import pytest
from pydantic_ai.models.test import TestModel

from kukie.agent import agent
from kukie.deps import Deps
from kukie.skills import SKILLS


def _deps(skill_name: str = "학습") -> Deps:
    return Deps(context="kind-dev", namespace="study", skill=SKILLS[skill_name])


GOOD_RESPONSE = {
    "narration": "결과입니다.",
    "steps": [],
    "suggested_transition": None,
}


# ── 툴 등록 + 스킬 필터 ─────────────────────────────────────

@pytest.mark.parametrize("skill,expected", [
    ("학습", {"list_resources", "explain_command"}),
    ("진단", {"list_resources", "explain_command",
              "describe_resource", "get_events", "get_logs"}),
    ("실습", {"list_resources", "explain_command", "describe_resource"}),
])
def test_스킬별로_허용된_툴만_LLM에게_노출된다(skill, expected):
    m = TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)
    with agent.override(model=m):
        agent.run_sync("파드 보여줘", deps=_deps(skill))
    exposed = {t.name for t in m.last_model_request_parameters.function_tools}
    assert exposed == expected


def test_변경_툴은_아직_등록되지_않았다():
    """훅 본체 완성 전까지 변경 툴은 어떤 스킬에서도 노출되면 안 된다 (승인 우회 방지)."""
    m = TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)
    with agent.override(model=m):
        agent.run_sync("nginx 지워줘", deps=_deps("실습"))
    exposed = {t.name for t in m.last_model_request_parameters.function_tools}
    assert not exposed & {"apply_manifest", "scale_resource",
                          "rollout_restart", "delete_resource"}


def test_응답은_KukieResponse_형식으로_강제된다():
    m = TestModel(call_tools=[], custom_output_args=GOOD_RESPONSE)
    with agent.override(model=m):
        result = agent.run_sync("파드 보여줘", deps=_deps())
    assert result.output.narration
    assert result.output.steps == []
