"""스킬 레지스트리 — 대화의 공개 범위가 스킬을 정한다 (DURO-100).

Private 대화는 읽기(kubectl 5종 + 레포 3종)만, Shared 대화는 읽기 + 변경 4종 + PR 길. 학습/진단/실습 모드는 없앴다.
공개 범위는 대화 안에서 바뀌지 않으므로 한 대화의 툴 목록·응답 스키마·시스템 프롬프트는 턴마다 같다
(프롬프트 캐시가 맞도록 — 턴마다 달라지는 문구를 넣지 않는다).
사전검토 스킬은 Issue #43에서 현재 가드레일 정책을 기준으로 후속 설계한다.
"""
from kukie_agent.skills import private, shared
from kukie_agent.skills.base import COMMON_TOOLS, Finding, KukieResponse, Skill

PRIVATE = private.SKILL
SHARED = shared.SKILL


def skill_for(is_shared: bool) -> Skill:
    """방의 공개 범위 → 스킬. Shared 방만 변경 툴을 받는다."""
    return SHARED if is_shared else PRIVATE


__all__ = ["PRIVATE", "SHARED", "skill_for", "Skill", "KukieResponse", "Finding", "COMMON_TOOLS"]
