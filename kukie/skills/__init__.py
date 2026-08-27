"""스킬 레지스트리 — 외부는 SKILLS 딕셔너리만 본다.

팀 결정 (가드레일 v2): MVP 스킬은 학습/진단/실습 3종.
히스토리 스킬은 LLM이 호출하는 Action Plan 조회 툴과 함께 MVP 제외다.
사전검토 스킬은 현재 가드레일 정책을 기준으로 후속 설계한다.
"""
from kukie.skills import diagnosis, learning, practice
from kukie.skills.base import COMMON_TOOLS, KukieResponse, Skill

SKILLS: dict[str, Skill] = {
    s.name: s
    for s in (learning.SKILL, diagnosis.SKILL, practice.SKILL)
}

DEFAULT_SKILL = learning.SKILL

__all__ = ["SKILLS", "DEFAULT_SKILL", "Skill", "KukieResponse", "COMMON_TOOLS"]
