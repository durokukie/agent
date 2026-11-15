"""Shared 스킬 — 팀이 함께 보는 대화. 읽기 5종 + 레포 읽기 3종(DURO-151) + 변경 4종 (DURO-100) + PR 길 변경 1종 (DURO-134).

변경 툴의 승인·위험도 처리는 guardrail/ 훅이 담당한다.
스킬은 툴을 노출만 한다 (기능 2 §0).
"""
from kukie_agent import prompts
from kukie_agent.skills.base import Skill

PROMPT = prompts.load("shared")

SKILL = Skill(
    name="Shared",
    prompt=PROMPT,
    extra_tools=frozenset({
        "apply_manifest", "scale_resource", "rollout_restart",
        "delete_resource",   # destructive — 훅에서 단일 승인
        "propose_git_change",  # ArgoCD 관리 리소스를 매니페스트 레포 PR 로 — guardrail/gitops_hook.py
    }),
)
