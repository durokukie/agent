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

    네 칸 전부 코드가 채운다 (response.collect_steps) — LLM은 이 블록의 존재를 모른다.
    command/output/access 는 실제 툴 실행 기록에서, explanations 는 FLAG_GLOSSARY 사전에서.
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
    suggested_next_action: str | None = None   # 답변·실행 결과에 근거한 다음 행동 안내


# ── 스킬 정의 ────────────────────────────────────────────────

# 공통 베이스 툴 — 어떤 스킬이든 항상 포함
# (탈출구 run_readonly_kubectl은 팀 결정으로 제거 — 안내 폴백으로 대체)
COMMON_TOOLS: frozenset[str] = frozenset({
    "list_resources",
    "explain_command",
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

    @property
    def output_fn(self):
        """run 에 꽂을 조립 함수 — output_type 클래스에서 steps 를 뺀 시그니처로 LLM 스키마를 만들고,
        steps 는 코드가 실행 기록에서 채운다 (DURO-44). 호출부: agent.run(..., output_type=skill.output_fn)."""
        from kukie.response import build_response_for   # 순환 import 회피 (response → base)
        return build_response_for(self.output_type)
