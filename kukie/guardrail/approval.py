"""CLI 승인 — stdin으로만 성립. 대화의 "해도 돼"는 승인이 아니다 (기능 2 §0).

caution      → [y/N] 1회
destructive  → [y/N] + 대상 리소스 이름 직접 타이핑 (이중 확인)
"""
from __future__ import annotations

from kukie.guardrail.action_plan import ActionPlan


def cli_approve(plan: ActionPlan, *, double: bool = False) -> bool:
    """Plan 요약(명령/대상/intent/영향/부작용/등급/매칭 룰)을 출력하고 확인을 받는다.

    주의: 승인 응답 입력은 스킬 라우팅을 타지 않는다 (별도 입력 채널).
    """
    raise NotImplementedError  # TODO: Plan 요약 출력 + input() 확인
