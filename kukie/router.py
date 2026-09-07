"""스킬 라우팅 — 명시적 /mode 명령으로 선택하고 나머지 입력에는 현재 모드를 유지한다.

suggested_next_action은 안내 문구이며 모드 전환을 실행하지 않는다.

채팅 메시지는 승인으로 해석하지 않는다. 승인은 Electron 화면이 `/approve`로 전달한다.
"""
from __future__ import annotations

from kukie.skills import DEFAULT_SKILL, SKILLS
from kukie.skills.base import Skill


def pick_skill(message: str, current: Skill | None) -> Skill:
    """명시적 모드 명령(/mode 학습 등)만 처리, 나머지는 현재 스킬 유지 (sticky).

    TODO(post-MVP): 강한 신호 키워드 자동 전환 + LLM 폴백
    """
    if message.startswith("/mode "):
        name = message.removeprefix("/mode ").strip()
        if name in SKILLS:
            return SKILLS[name]
    return current or DEFAULT_SKILL
