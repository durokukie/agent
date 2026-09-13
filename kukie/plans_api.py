"""Action Plan 조회 엔드포인트 — kukie-electron docs/api-spec.md 의 `GET /action-plans` (#58).

  GET /action-plans?cluster_id=&status=   대시보드 목록 (ActionPlanSummary[])
  GET /action-plans/{plan_id}             계획 전문 (사람이 읽는 Markdown)

읽기 전용이다. 계획을 만들고 상태를 바꾸는 것은 가드레일 훅과 승인 흐름뿐이다 (guardrail/hook.py).

범위는 대화 목록과 같다 — 내 방 + shared 방 (기획 05 §4). plan 에는 회원 id 가 없으므로
run_id → session_id → user_id 로 거슬러 올라가 검사한다 (DB 문서 6절).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

import kukie.server as _server   # 순환 import: 이름은 호출 시점에만 쓴다
from kukie import membership
from kukie.auth import User, current_user
from kukie.fields import none_if_blank
from kukie.guardrail import action_plan
from kukie.store import ChatStore, get_store
from kukie.store.chat_store import PlanSummaryRow

router = APIRouter(prefix="/action-plans", tags=["action-plans"])


def _summary_view(row: PlanSummaryRow) -> dict[str, Any]:
    """앱 api/types.ts 의 ActionPlanSummary. 상태는 DURO-83 이름(대문자)을 그대로 보낸다."""
    return {
        "id": row.id,
        "cluster_id": row.cluster_id,      # 클러스터 레코드가 붙기 전 만든 방은 null
        "title": row.title,
        "status": row.status,
        "running": row.running,
        "risk": row.risk,
        "requested_by": row.requested_by,
        "updated_at": row.updated_at.isoformat(),
        "approvals": row.approvals,
    }


@router.get("")
async def list_action_plans(
    cluster_id: str | None = None,
    status: str | None = None,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> list[dict[str, Any]]:
    # 대화 목록과 같은 범위여야 "목록엔 있는데 열 수 없는 계획" 이 안 생긴다 (자동 리뷰 지적)
    mine = list(await membership.team_roles(user)) if membership.available() else None
    try:
        rows = action_plan.list_plans(
            user.id, cluster_id=none_if_blank(cluster_id), status=none_if_blank(status),
            team_ids=mine,
        )
    except ValueError as exc:
        raise HTTPException(400, {"code": "BAD_STATUS", "message": str(exc)}) from None
    return [_summary_view(row) for row in rows]


@router.get("/{plan_id}")
async def get_action_plan(
    plan_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    """계획 전문. 남의 private 방 계획은 없는 것처럼 404 (대화 조회와 같은 규칙)."""
    scope = store.plan_scope(plan_id)
    missing = HTTPException(404, {"code": "NOT_FOUND", "message": f"계획이 없다: {plan_id}"})
    if scope is None or (scope.owner_id != user.id and not scope.shared):
        raise missing
    # shared 방은 _load 와 같은 규칙이다. 팀이 붙었으면 지금 소속을 묻는다 — 안 물으면 로그인한 아무나
    # 남의 팀 계획 전문(명령·대상·결과)을 읽는다 (자동 리뷰 지적). 팀이 없으면 만든 사람만 (#75).
    if scope.shared and membership.available():
        if not scope.team_id:
            if scope.owner_id != user.id:
                raise missing
        elif not await membership.is_member(user, scope.team_id):
            raise missing
    row = store.get_plan(plan_id)
    assert row is not None
    return {
        "id": row.id,
        "run_id": row.run_id,
        "status": row.status,
        "risk": row.risk,
        "failure_reason": row.failure_reason,
        "decision": row.decision,
        "execution_result": row.execution_result,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "markdown": action_plan.ActionPlan.from_row(row).render_markdown(),
    }


_server.app.include_router(router)   # server.py 맨 아래의 plain import 가 이 줄을 실행시킨다

__all__ = ["router"]
