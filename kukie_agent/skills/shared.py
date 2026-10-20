"""Shared 스킬 — 팀이 함께 보는 대화. 읽기 5종 + 변경 4종 (DURO-100).

변경 툴의 승인·위험도 처리는 guardrail/ 훅이 담당한다.
스킬은 툴을 노출만 한다 (기능 2 §0).
"""
from kukie_agent.skills.base import Skill

PROMPT = """이 대화는 Shared 대화다. 클러스터를 읽을 수 있고, 변경 툴로 바꿀 수도 있다.
변경은 승인 카드에서 사용자가 승인해야 실행된다."""

SKILL = Skill(
    name="Shared",
    prompt=PROMPT,
    extra_tools=frozenset({
        "apply_manifest", "scale_resource", "rollout_restart",
        "delete_resource",   # destructive — 훅에서 단일 승인
    }),
)
