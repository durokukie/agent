"""클러스터 등록 엔드포인트 (기획 04 §8, issue #61).

  POST   /clusters              kubeconfig 본문으로 등록
  GET    /clusters?team_id=     목록
  POST   /clusters/{id}/test    연결 확인 (kubectl version + auth can-i)
  PATCH  /clusters/{id}         이름·namespace·자격증명 교체
  DELETE /clusters/{id}         삭제 — 자격증명도 함께 사라진다

**자격증명은 어떤 응답에도 실리지 않는다** (기획 04 §4). 목록·상세는 접속 주소와 상태만 준다.

권한은 지금 "등록한 사람" 기준이다. 기획 04 §3 은 팀 Admin 을 요구하는데 팀 판단은 Spring 이 하고
agent 는 아직 팀 API 를 부르지 않는다 — 대화 승인 권한과 같은 임시 규칙이다 (#55).
"""
from __future__ import annotations

import logging
from typing import Any

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, ConfigDict, Field

import kukie.server as _server   # 순환 import: 이름은 호출 시점에만 쓴다
from kukie.auth import User, current_user
from kukie.clusters import crypto
from kukie.clusters.access import ClusterGone, kubeconfig_for
from kukie.clusters.kubeconfig import KubeconfigRejected, parse_kubeconfig
from kukie.clusters.settings import allow_local_clusters
from kukie.kubectl import run_kubectl
from kukie.store import ChatStore, get_store
from kukie.store.chat_store import ClusterInUse, ClusterRow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["clusters"])

#: kubeconfig 검사(이름 해석)에 쓸 수 있는 스레드 수. AnyIO 의 공용 워커 풀은 기본 40개인데,
#: getaddrinfo 는 타임아웃을 줄 수 없어 느린 이름 몇 개가 그 풀을 통째로 물면 같은 풀을 쓰는
#: 다른 일까지 함께 멈춘다 (자동 리뷰 지적). 등록은 드문 동작이라 넉넉히 4면 된다.
_RESOLVE_LIMIT = CapacityLimiter(4)


# ── 요청 본문 ──────────────────────────────────────────────

class ClusterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kubeconfig: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=100)
    context: str | None = None            # 여러 context 중 하나를 고를 때
    namespace: str | None = None
    team_id: str | None = None


class ClusterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    namespace: str | None = None
    kubeconfig: str | None = None         # 자격증명 교체
    context: str | None = None


# ── 응답 ───────────────────────────────────────────────────

def _view(row: ClusterRow) -> dict[str, Any]:
    """자격증명은 절대 넣지 않는다. 앱이 보여줄 값만."""
    return {
        "id": row.id,
        "team_id": row.team_id,
        "registered_by": row.registered_by,
        "name": row.name,
        "provider": row.provider,
        "api_server": row.api_server,
        "context": row.context_name,
        "namespace": row.default_namespace,
        "fingerprint": row.fingerprint,
        "status": row.status,
        "insecure": row.insecure,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _require_key() -> None:
    if not crypto.available():
        raise _error(
            503, "SECRET_KEY_MISSING",
            f"서버에 암호화 키({crypto.KEY_ENV})가 없어 클러스터를 등록할 수 없습니다",
        )


def _owned(store: ChatStore, cluster_id: str, user: User) -> ClusterRow:
    """남의 클러스터는 없는 것처럼 404 — 있다는 사실 자체를 알려 주지 않는다."""
    row = store.get_cluster(cluster_id)
    if row is None or row.registered_by != user.id:
        raise _error(404, "NOT_FOUND", f"클러스터가 없다: {cluster_id}")
    return row


async def _parse(body_text: str, context: str | None):
    """kubeconfig 검사. 이름 해석(getaddrinfo)이 블로킹이라 스레드로 넘긴다 — 응답하지 않는
    네임서버를 가진 도메인을 등록하면 그동안 서버 전체가 멈춘다 (자동 리뷰 지적)."""
    def run():
        return parse_kubeconfig(body_text, context_name=context, allow_local=allow_local_clusters())

    try:
        return await to_thread.run_sync(run, limiter=_RESOLVE_LIMIT)
    except KubeconfigRejected as exc:
        raise _error(400, "KUBECONFIG_REJECTED", str(exc)) from None


# ── 엔드포인트 ─────────────────────────────────────────────

@router.post("")
async def register_cluster(
    body: ClusterIn,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    _require_key()
    # team_id 를 검사하지 않으면 409 CLUSTER_NAME_TAKEN 이 "그 팀에 그 이름이 있나" 를 알려 주는
    # 신호가 되고, 남의 팀 이름 공간에 자리를 선점할 수도 있다 (자동 리뷰 지적).
    # 소속 판단은 회원 서버가 하므로 여기서는 팀을 지정한 등록 자체를 막고, 팀 클러스터는
    # 팀 API 가 붙은 뒤(#64)에 연다.
    if body.team_id:
        raise _error(501, "TEAM_CLUSTER_UNSUPPORTED",
                     "팀 클러스터 등록은 아직 지원하지 않습니다 — 팀 권한 검사가 붙은 뒤에 열립니다")
    parsed = await _parse(body.kubeconfig, body.context)
    try:
        row = store.create_cluster(
            registered_by=user.id,
            team_id=body.team_id,
            name=body.name.strip(),
            api_server=parsed.api_server,
            ca_data=parsed.ca_data,
            insecure=parsed.insecure,
            credential_encrypted=crypto.encrypt(parsed.credential),
            context_name=parsed.context_name,
            default_namespace=(body.namespace or parsed.namespace).strip() or "default",
            fingerprint=parsed.fingerprint,
        )
    except IntegrityError:
        raise _error(409, "CLUSTER_NAME_TAKEN", f"같은 이름의 클러스터가 이미 있습니다: {body.name.strip()}") from None
    return _view(row)


@router.get("")
async def list_clusters(
    team_id: str | None = None,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> list[dict[str, Any]]:
    return [_view(row) for row in store.list_clusters(user.id, team_id=team_id)]


@router.get("/{cluster_id}")
async def get_cluster(
    cluster_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    return _view(_owned(store, cluster_id, user))


@router.post("/{cluster_id}/test")
async def test_cluster(
    cluster_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    """연결 확인 (기획 04 §8). 서버 버전을 읽고 변경 권한이 있는지 물어본다."""
    row = _owned(store, cluster_id, user)
    _require_key()
    from datetime import datetime, timezone

    def probe() -> tuple[Any, Any]:
        with kubeconfig_for(store, cluster_id) as path:
            return (
                run_kubectl(["version", "-o", "json"], context=row.context_name, kubeconfig=path),
                run_kubectl(
                    ["auth", "can-i", "update", "deployments", "-n", row.default_namespace],
                    context=row.context_name, kubeconfig=path,
                ),
            )

    try:
        # run_kubectl 은 subprocess.run 을 그대로 부른다. 여기서 직접 부르면 닿지 않는 주소일 때
        # 최대 60초(version + can-i) 동안 서버 전체가 멈춘다 (자동 리뷰 지적). 스레드로 넘긴다.
        version, can_edit = await to_thread.run_sync(probe)
    except ClusterGone:
        raise _error(404, "NOT_FOUND", f"클러스터가 없다: {cluster_id}") from None
    except crypto.CredentialUnreadable as exc:
        raise _error(503, "CREDENTIAL_UNREADABLE", str(exc)) from None
    status = "connected" if version.success else _failure_status(version.stderr)
    store.update_cluster(cluster_id, status=status, last_checked_at=datetime.now(timezone.utc))
    return {
        "status": status,
        "reachable": version.success,
        # 실패 사유는 그대로 보여줘야 고칠 수 있다. 자격증명은 원래 stderr 에 실리지 않는다
        "detail": (version.stderr or version.stdout).strip()[:500],
        "can_change": can_edit.success and can_edit.stdout.strip() == "yes",
    }


@router.patch("/{cluster_id}")
async def update_cluster(
    cluster_id: str,
    body: ClusterPatch,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    row = _owned(store, cluster_id, user)
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.namespace is not None:
        fields["default_namespace"] = body.namespace.strip() or "default"
    if body.kubeconfig is not None:
        _require_key()
        parsed = await _parse(body.kubeconfig, body.context)
        if parsed.fingerprint != row.fingerprint:
            raise _error(
                409, "CLUSTER_MISMATCH",
                "다른 클러스터의 kubeconfig 입니다 — 자격증명 교체는 같은 클러스터만 됩니다",
            )
        # context 이름은 바꾸지 않는다. 지문은 주소·CA 만 덮으므로 새 kubeconfig 의 context 이름이
        # 달라도 통과하는데, 그 이름을 방마다 복사해 뒀기 때문에 바꾸면 기존 방이 전부 엉뚱한
        # context 를 가리킨다 (자동 리뷰 지적). 이름을 바꿔야 하면 다시 등록한다.
        fields.update(
            credential_encrypted=crypto.encrypt(parsed.credential),
            ca_data=parsed.ca_data,
            insecure=parsed.insecure,
            status="disconnected",       # 바꿨으니 다시 확인해야 한다
        )
    if not fields:
        raise _error(400, "NOTHING_TO_UPDATE", "바꿀 내용이 없습니다")
    try:
        store.update_cluster(cluster_id, **fields)
    except IntegrityError:
        raise _error(409, "CLUSTER_NAME_TAKEN", f"같은 이름의 클러스터가 이미 있습니다: {fields.get('name')}") from None
    updated = store.get_cluster(cluster_id)
    assert updated is not None
    return _view(updated)


@router.delete("/{cluster_id}")
async def delete_cluster(
    cluster_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    _owned(store, cluster_id, user)
    try:
        store.delete_cluster(cluster_id)
    except ClusterInUse:
        raise _error(
            409, "CLUSTER_IN_USE",
            "이 클러스터를 쓰는 대화가 남아 있습니다 — 대화를 먼저 정리하세요",
        ) from None
    return {"deleted": cluster_id}


def _failure_status(stderr: str) -> str:
    lowered = stderr.lower()
    if "unauthorized" in lowered or "forbidden" in lowered or "credential" in lowered:
        return "auth_expired"
    return "disconnected"


_server.app.include_router(router)

__all__ = ["router"]
