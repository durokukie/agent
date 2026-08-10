"""실습 스킬 — "만들어 주세요" (기능 1 §4.3) — 변경 작업 포함.

변경 툴의 승인·위험도 처리는 guardrail/ 훅이 담당한다.
스킬은 툴을 노출만 한다 (기능 2 §0).
"""
from kukie.skills.base import Skill

PROMPT = """지금은 실습 모드다. 변경 전에 무엇을/왜/어떤 명령이 나갈지를 먼저 설명해라.
매니페스트를 만들 때는 각 필드가 왜 필요한지 해설해라.
실행 후에는 결과를 확인(list/describe)하고 방금 무슨 일이 일어났는지 복기해라.
변경 툴을 호출할 때 intent/expected_effects/side_effects를 구체적으로 채워라."""

SKILL = Skill(
    name="실습",
    prompt=PROMPT,
    extra_tools=frozenset({
        "apply_manifest", "scale_resource", "rollout_restart",
        "delete_resource",   # destructive — 훅에서 이중 승인
        "describe_resource",  # 실행 후 결과 확인용 (매트릭스 검토 반영)
    }),
)
