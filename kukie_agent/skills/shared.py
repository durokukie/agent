"""Shared 스킬 — 팀이 함께 보는 대화. 읽기 5종 + 레포 읽기 3종(DURO-151) + 변경 4종 (DURO-100) + PR 길 변경 1종 (DURO-134).

변경 툴의 승인·위험도 처리는 guardrail/ 훅이 담당한다.
스킬은 툴을 노출만 한다 (기능 2 §0).
"""
from kukie_agent.skills.base import Skill

PROMPT = """이 대화는 Shared 대화다. 클러스터를 읽을 수 있고, 변경 툴로 바꿀 수도 있다.
변경은 승인 카드에서 사용자가 승인해야 실행된다.
ArgoCD 가 관리하는 리소스는 레포가 연결돼 있으면 propose_git_change 로 PR 을 올려 바꾼다. 어느 길인지는
툴 결과와 시스템 알림이 알려 준다 — 대화 기록의 예전 답변이 아니라 지금 알려 준 길을 따른다."""

SKILL = Skill(
    name="Shared",
    prompt=PROMPT,
    extra_tools=frozenset({
        "apply_manifest", "scale_resource", "rollout_restart",
        "delete_resource",   # destructive — 훅에서 단일 승인
        "propose_git_change",  # ArgoCD 관리 리소스를 매니페스트 레포 PR 로 — guardrail/gitops_hook.py
    }),
)
