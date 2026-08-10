"""스킬 레지스트리 — 외부는 SKILLS 딕셔너리만 본다."""
from kukie.skills import diagnosis, history, learning, practice, review
from kukie.skills.base import COMMON_TOOLS, KukieResponse, Skill

SKILLS: dict[str, Skill] = {
    s.name: s
    for s in (learning.SKILL, diagnosis.SKILL, practice.SKILL,
              history.SKILL, review.SKILL)
}

DEFAULT_SKILL = learning.SKILL

__all__ = ["SKILLS", "DEFAULT_SKILL", "Skill", "KukieResponse", "COMMON_TOOLS"]
