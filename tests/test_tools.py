"""툴 카탈로그 불변식 테스트 — 가드레일 v2 결정사항 검증.

구현(NotImplementedError) 없이도 검증 가능한 것들:
- 훅 대상 목록이 함수 리스트에서 자동 파생되는지 (수동 관리 사고 방지)
- 모든 변경 툴에 위험도 스티커가 등록돼 있는지 (미등록 = fail-closed 대상)
- LLM이 kubectl args를 직접 조립하는 경로가 없는지 (탈출구 제거 확인)
"""
from kukie.skills import SKILLS
from kukie.skills.base import COMMON_TOOLS
from kukie.tools.mutate import MUTATE_TOOLS, MUTATING_TOOLS, RISK_STICKERS, Risk
from kukie.tools.read import READ_TOOLS


def test_훅_대상은_변경_함수_목록에서_자동_파생된다():
    """MUTATING_TOOLS를 손으로 관리하면 툴 추가 시 훅 누락 사고가 난다."""
    assert MUTATING_TOOLS == frozenset(t.__name__ for t in MUTATE_TOOLS)


def test_모든_변경_툴에_위험도_스티커가_있다():
    """스티커 없는 변경 툴이 생기면 여기서 잡힌다."""
    assert set(RISK_STICKERS) == set(MUTATING_TOOLS)


def test_delete만_destructive_나머지_변경은_caution():
    assert RISK_STICKERS["delete_resource"] is Risk.DESTRUCTIVE
    for name in MUTATING_TOOLS - {"delete_resource"}:
        assert RISK_STICKERS[name] is Risk.CAUTION


def test_LLM이_kubectl_args를_직접_조립하는_툴이_없다():
    """팀 결정: 자유형 args를 받는 툴(탈출구) 없음 — 모든 명령은 코드가 조립."""
    for tool in [*READ_TOOLS, *MUTATE_TOOLS]:
        params = tool.__code__.co_varnames[: tool.__code__.co_argcount]
        assert "args" not in params, f"{tool.__name__}이 자유형 args를 받음"


def test_읽기_툴은_5종이다():
    assert {t.__name__ for t in READ_TOOLS} == {
        "list_resources", "describe_resource", "get_events",
        "get_logs", "explain_command",
    }


def test_스킬_레지스트리와_공통_툴():
    # MVP 스킬 3종 (히스토리·사전검토는 Action Plan 조회 툴과 함께 보류)
    assert set(SKILLS) == {"학습", "진단", "실습"}
    for skill in SKILLS.values():
        assert COMMON_TOOLS <= skill.allowed_tools


def test_실습_스킬만_변경_툴을_가진다():
    for skill in SKILLS.values():
        has_mutating = bool(skill.allowed_tools & MUTATING_TOOLS)
        assert has_mutating == (skill.name == "실습"), skill.name
