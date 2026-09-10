"""요청 본문의 문자열 칸을 다듬는 검증기 — 여러 API 가 같은 규칙을 써야 한다.

빈 문자열의 뜻은 **"값을 안 정했다" 하나**로 고정한다. 두 곳에서 뜻이 갈리면 폼 전체를 보내는
화면이 사용자가 안 건드린 칸을 조용히 갈아엎고, `team_id: ""` 같은 값이 "팀 없음" 검사(falsy)와
"그 팀 것만"(IN) 검사 사이로 새어 목록과 상세의 답이 달라진다 (자동 리뷰 지적).
"""
from __future__ import annotations


def stripped(cls: object, value: object) -> object:
    """앞뒤 공백을 떼고 다음 검사(min_length 등)에 넘긴다. 공백만 있는 값은 `''` 가 돼 걸린다."""
    return value.strip() if isinstance(value, str) else value


def blank_is_none(cls: object, value: object) -> object:
    """공백을 떼고, 남은 게 없으면 None. 정확히 일치해야 하는 값(context)의 공백도 여기서 뗀다."""
    value = value.strip() if isinstance(value, str) else value
    return None if value == "" else value


__all__ = ["blank_is_none", "stripped"]
