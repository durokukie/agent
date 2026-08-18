"""학습 스킬 — "이게 뭐예요?" (기능 1 §4.1)"""
from kukie.skills.base import Skill

PROMPT = """지금은 학습 모드다. 클러스터를 변경하지 말고 개념 설명에 집중해라.
개념 설명 순서: 비유 → 정확한 정의 → 실제 예시 명령.
실제 조회가 이해를 돕는 경우에만 읽기 툴을 사용하고, 그 결과도 학습 자료로 해설해라."""

SKILL = Skill(
    name="학습",
    prompt=PROMPT,
    # 추가 툴 없음 — 공통 툴(list_resources/explain_command)만으로 동작
)
