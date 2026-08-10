"""진단 스킬 — "왜 안 돼요?" (기능 1 §4.2) — 읽기 전용 유지."""
from pydantic import BaseModel

from kukie.skills.base import Skill, KukieResponse


class Finding(BaseModel):
    """진단 결론 — (a)원인 (b)근거 (c)권장 조치를 구조로 강제."""
    cause: str
    evidence: str
    recommendation: str


class DiagnosisResponse(KukieResponse):
    finding: Finding | None = None


PROMPT = """지금은 진단 모드다. 추측으로 답하지 마라.
describe(리소스 상태) → events(클러스터 차원) → logs(앱 차원) 순서로 확인하고,
각 단계에서 무엇을 확인했고 무엇을 발견했는지 설명해라.
원인을 찾으면 finding에 (a)원인 (b)근거가 된 출력 (c)권장 조치를 구분해 담아라.
조치 실행은 하지 않고 실습 모드로 안내해라."""

SKILL = Skill(
    name="진단",
    prompt=PROMPT,
    extra_tools=frozenset({"describe_resource", "get_events", "get_logs"}),
    output_type=DiagnosisResponse,
)
