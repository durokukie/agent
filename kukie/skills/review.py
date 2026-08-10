"""사전 검토 스킬 — "이거 해도 안전해?" (기능 2 §3)

실행·기록 없이 훅의 판정·dry-run 코드만 공유해 평가한다.
"""
from pydantic import BaseModel

from kukie.skills.base import Skill, KukieResponse


class Verdict(BaseModel):
    """시스템 판정 결과 — 코드가 채운다 (LLM 등급 자기신고 금지)."""
    command: str
    risk_level: str            # safe / caution / destructive
    matched_rules: list[str]
    dry_run_output: str
    required_approval: str     # "승인 불필요" / "1회 확인" / "이중 확인"


class ReviewResponse(KukieResponse):
    verdict: Verdict | None = None


PROMPT = """지금은 사전 검토 모드다. 어떤 경우에도 클러스터를 변경하지 마라.
검토 요청이 오면 evaluate_action으로 시스템 판정을 받은 뒤,
(a)등급과 판정 이유(매칭 룰) (b)dry-run 결과 (c)예상 영향·부작용 (d)더 안전한 대안 순서로 설명해라.
등급을 네가 추측해서 먼저 말하지 마라 — 판정은 항상 시스템 결과를 인용해라.
왜 이 룰이 존재하는지(예: --force가 왜 위험한지)를 학습 관점에서 함께 해설해라.
실행을 원하면 실습 모드로 안내하고, 검토 결과는 기록으로 남지 않는다고 알려줘라."""

SKILL = Skill(
    name="사전검토",
    prompt=PROMPT,
    extra_tools=frozenset({"evaluate_action"}),
    output_type=ReviewResponse,
)
