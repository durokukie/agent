"""스킬 라우팅 — MVP: 직접 선택 + LLM의 전환 제안 (원클릭 y).

MVP에서 자동 라우팅은 하지 않는다. LLM이 suggested_transition으로 제안하면
사용자가 y 한 번으로 전환한다. (전환 제안 수락률 데이터가 쌓이면 자동 라우팅 검토)

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
