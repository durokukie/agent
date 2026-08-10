"""스킬 정의와 공통 응답 스키마.

스킬 = 프롬프트 + 툴 이름 목록 + 응답 스키마 (3요소).
툴 실제 구현은 tools/ 카탈로그에 있고, 스킬은 이름으로만 참조한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel


# ── 공통 응답 뼈대 (모든 스킬 공유 — UI 일관성) ──────────────────

class FieldExplanation(BaseModel):
    """명령/필드 설명 한 줄 — '배우면서' 가치의 그릇."""
    field: str      # 예: "-n study"
    meaning: str    # 예: "study 네임스페이스를 대상으로 지정"


class ToolStep(BaseModel):
    """화면의 명령 실행 블록 하나.

    command/output/access는 코드가 채운다 (LLM 자기신고 금지).
    explanations만 LLM 몫.
    """
    step_label: str
    access: Literal["read-only", "mutating"]
    command: str
    output: str
    explanations: list[FieldExplanation]


class KukieResponse(BaseModel):
    """공통 응답 뼈대. 스킬별 특화 블록은 이걸 상속해 추가한다."""
    narration: str
    steps: list[ToolStep] = []
    suggested_transition: str | None = None   # "실습 모드로 전환할까요?" 등


# ── 스킬 정의 ────────────────────────────────────────────────

# 공통 베이스 툴 — 어떤 스킬이든 항상 포함 (기능 1 §3)
COMMON_TOOLS: frozenset[str] = frozenset({
    "list_resources",
    "explain_command",
    "run_readonly_kubectl",
})


@dataclass(frozen=True)
class Skill:
    name: str
    prompt: str                          # 스킬 전용 프롬프트 (공통 베이스 위에 얹힘)
    extra_tools: frozenset[str] = field(default_factory=frozenset)
    output_type: type[KukieResponse] = KukieResponse

    @property
    def allowed_tools(self) -> frozenset[str]:
        return COMMON_TOOLS | self.extra_tools
