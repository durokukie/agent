"""히스토리 스킬 — "지난주에 뭐 했지?" (기능 2 §2)

Action Plan 기록을 근거로만 답한다. 변경은 실행하지 않는다.
"""
from pydantic import BaseModel

from kukie.skills.base import Skill, KukieResponse


class PlanSummary(BaseModel):
    """조회된 Action Plan 요약 한 건 — id 인용을 구조로 강제."""
    id: str
    created_at: str
    command: str
    intent: str
    status: str      # executed / failed / rejected


class HistoryResponse(KukieResponse):
    records: list[PlanSummary] = []


PROMPT = """지금은 히스토리 모드다. 과거 작업에 대한 질문에 기억이나 추측으로 답하지 마라 —
반드시 list_action_plans로 조회한 기록을 근거로 답해라.
답변은 (a)언제 (b)무엇을 (c)왜 했는지(intent) (d)결과 순서로, 근거 Plan의 id와 함께 제시해라.
목록 요약으로 부족할 때만 get_action_plan으로 상세를 확인해라.
기록이 없으면 "기록이 없다"고 답해라. 없는 작업을 재구성하지 마라.
재실행·수정 요청은 실습 모드로 안내해라."""

SKILL = Skill(
    name="히스토리",
    prompt=PROMPT,
    extra_tools=frozenset({"list_action_plans", "get_action_plan"}),
    output_type=HistoryResponse,
)
