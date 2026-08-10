"""Action Plan 조회·평가 툴 (기능 2 §2.1, §3.1) — 전부 읽기 성격, 자동 실행."""
from __future__ import annotations

from pydantic_ai import RunContext

from kukie.deps import Deps
from kukie.guardrail import action_plan


def list_action_plans(ctx: RunContext[Deps], namespace: str | None = None,
                      kind: str | None = None, status: str | None = None,
                      since: str | None = None, limit: int = 20) -> list[dict]:
    """과거 Action Plan의 frontmatter 요약 목록을 조회한다 (본문 안 읽음 — 토큰 절약)."""
    raise NotImplementedError  # TODO → action_plan.list_plans()


def get_action_plan(ctx: RunContext[Deps], plan_id: str) -> str:
    """특정 Action Plan의 전문(intent/영향/부작용/결과 포함)을 읽는다.

    목록 요약으로 부족할 때만 사용한다.
    """
    raise NotImplementedError  # TODO → action_plan.get_plan()


def evaluate_action(ctx: RunContext[Deps], tool_name: str, tool_args: dict) -> dict:
    """실행 없이 위험도를 평가한다 (사전 검토 전용).

    훅의 ②조립·③판정·⑤dry-run 코드를 그대로 공유한다 — 별도 판정 로직 금지.
    Plan 파일을 만들지 않고, 기록도 남기지 않는다.
    반환: {command, risk_level, matched_rules, dry_run_output, required_approval}
    """
    raise NotImplementedError  # TODO → guardrail.hook의 단계 함수 재사용


PLAN_TOOLS = [list_action_plans, get_action_plan, evaluate_action]
