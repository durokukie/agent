"""툴 카탈로그 불변식 테스트 — 가드레일 v2 결정사항 검증.

구현(NotImplementedError) 없이도 검증 가능한 것들:
- 훅 대상 목록이 함수 리스트에서 자동 파생되는지 (수동 관리 사고 방지)
- 모든 변경 툴에 위험도 스티커가 등록돼 있는지 (미등록 = fail-closed 대상)
- 읽기 탈출구의 화이트리스트가 변경 verb를 거부하는지
"""
from types import SimpleNamespace

from kukie.skills import SKILLS
from kukie.skills.base import COMMON_TOOLS
from kukie.tools.mutate import MUTATE_TOOLS, MUTATING_TOOLS, RISK_STICKERS, Risk
from kukie.tools.read import run_readonly_kubectl


def _dummy_ctx():
    return SimpleNamespace(deps=SimpleNamespace(context="test", namespace="default"))


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


def test_읽기_탈출구는_변경_verb를_거부한다():
    """fail-closed: 화이트리스트에 없으면 실행하지 않고 거부."""
    for args in (
        ["delete", "pod", "nginx"],
        ["apply", "-f", "x.yaml"],
        ["scale", "deployment", "nginx", "--replicas=0"],
        ["rollout", "restart", "deployment/nginx"],  # rollout status만 허용
        ["config", "use-context", "prod"],           # config view만 허용
        [],
    ):
        result = run_readonly_kubectl(_dummy_ctx(), args)
        assert result.success is False, f"차단됐어야 함: {args}"


def test_스킬_레지스트리와_공통_툴():
    # MVP 스킬 3종 (히스토리·사전검토는 Action Plan 조회 툴과 함께 보류)
    assert set(SKILLS) == {"학습", "진단", "실습"}
    for skill in SKILLS.values():
        assert COMMON_TOOLS <= skill.allowed_tools


def test_실습_스킬만_변경_툴을_가진다():
    for skill in SKILLS.values():
        has_mutating = bool(skill.allowed_tools & MUTATING_TOOLS)
        assert has_mutating == (skill.name == "실습"), skill.name
