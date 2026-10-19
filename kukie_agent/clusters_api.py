"""클러스터 연결 확인 엔드포인트 (기획 04 §8, issue #61).

  POST /clusters/{id}/test       연결 확인 (kubectl version + auth can-i)
  GET  /clusters/{id}/overview   개요 — 웹 대시보드의 클러스터 지도·통계 (issue #102, kubectl get × 3)

등록·목록·수정·삭제는 Kukie API 서버(kukie-server)의 공개 API 다 (DURO-113) — kubeconfig 검사와 자격증명
암호화도 그쪽에 있다. 연결 확인만 agent 에 남은 이유는 kubectl 이 여기서 돌기 때문이다.

권한은 server 가 본다. 연결 정보를 사용자 토큰으로 받으므로(clusters/access.py) 못 보는 클러스터는 그 호출이
404 를 준다. 팀 구성원도 부를 수 있고, 결과(status·last_checked_at)를 `PATCH /internal/clusters/{id}/status` 로
남긴다 — 연결 확인의 부산물이라 "쓰기는 Admin" 경계에서 일부러 빼 뒀다 (자동 리뷰 지적).

**자격증명은 어떤 응답에도 실리지 않는다** (기획 04 §4).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException

import kukie_agent.server as _server   # 순환 import: 이름은 호출 시점에만 쓴다
from kukie_agent import server_api
from kukie_agent.auth import User, current_user
from kukie_agent.clusters import access, overview
from kukie_agent.kubectl import run_kubectl, thread_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["clusters"])

#: 공용 워커 풀을 통째로 물지 않도록 이 모듈이 쓰는 스레드 수를 따로 묶는다 (자동 리뷰 지적, 이유는 thread_limiter).
#: kubectl 은 30초씩 잡는다.
PROBE_WORKERS = 4            # 연결 확인 (kubectl × 2, 최대 60초)
OVERVIEW_WORKERS = 4         # 개요 (kubectl × 3). 대시보드가 띄울 때마다 부른다 — 연결 확인과 따로 묶는다
#: 개요의 kubectl 한 번 시간 제한(초). 기본 30초 × 3 이면 한 요청이 슬롯을 90초 잡는다 — 닿지 않으면 첫 번째에서 15초로 끝난다 (자동 리뷰 지적)
OVERVIEW_TIMEOUT = 15


@router.post("/{cluster_id}/test")
async def test_cluster(
    cluster_id: str,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """연결 확인 (기획 04 §8). 서버 버전을 읽고 변경 권한이 있는지 물어본다."""
    conn = await access.connection(cluster_id, user)

    def probe() -> tuple[Any, Any]:
        with access.kubeconfig(conn) as path:
            return (
                run_kubectl(["version", "-o", "json"], context=conn.context_name, kubeconfig=path),
                run_kubectl(
                    ["auth", "can-i", "update", "deployments", "-n", conn.namespace],
                    context=conn.context_name, kubeconfig=path,
                ),
            )

    # run_kubectl 은 subprocess.run 을 그대로 부른다. 여기서 직접 부르면 닿지 않는 주소일 때
    # 최대 60초(version + can-i) 동안 서버 전체가 멈춘다 (자동 리뷰 지적). 스레드로 넘긴다.
    version, can_edit = await to_thread.run_sync(probe, limiter=thread_limiter("probe", PROBE_WORKERS))
    status = "connected" if version.success else _failure_status(version.stderr)
    await server_api.update_cluster_status(cluster_id, status=status, checked_at=datetime.now(timezone.utc))
    return {
        "status": status,
        "reachable": version.success,
        # 실패 사유는 그대로 보여줘야 고칠 수 있다. 자격증명은 원래 stderr 에 실리지 않는다
        "detail": (version.stderr or version.stdout).strip()[:500],
        "can_change": can_edit.success and can_edit.stdout.strip() == "yes",
    }


@router.get("/{cluster_id}/overview")
async def cluster_overview(
    cluster_id: str,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """개요 (issue #102). 클러스터 namespace 의 워크로드·파드·최근 이벤트를 읽는다 — 응답 모양과 판정은 clusters/overview.py.

    권한·자격증명은 연결 확인과 같은 길이다 — 못 보는 클러스터는 server 의 404, 팀 구성원 누구나 부른다.
    읽기 전용이고 namespace 범위(`-n`)로만 조회한다. 상태(status)는 남기지 않는다 — 그건 연결 확인의 일이다.

    kubectl 이 실패하면 연결 확인과 같은 기준(_failure_status)으로 갈라 502 로 답한다.
      CLUSTER_AUTH_EXPIRED  인증 실패·권한 없음 (연결 확인의 auth_expired)
      CLUSTER_UNREACHABLE   그 밖의 실패 — 닿지 않음·시간 초과 (연결 확인의 disconnected)
      CLUSTER_BAD_RESPONSE  kubectl 은 성공했는데 JSON 으로 읽지 못했다
    401 이 아니라 502 인 이유: 앱은 401 을 사용자 토큰 만료로 읽고 로그아웃한다. 클러스터 자격증명 문제는 그게 아니다.
    """
    conn = await access.connection(cluster_id, user)

    def collect() -> dict[str, Any]:
        outputs: dict[str, str] = {}
        with access.kubeconfig(conn) as path:
            for name, args in overview.READ_COMMANDS.items():
                result = run_kubectl([*args, "-n", conn.namespace], context=conn.context_name,
                                     kubeconfig=path, timeout=OVERVIEW_TIMEOUT)
                if not result.success:
                    _raise_kubectl_failure(result.stderr or result.stdout)   # 첫 실패에서 멈춘다
                outputs[name] = result.stdout
        try:
            return overview.build(context=conn.context_name, namespace=conn.namespace,
                                  now=datetime.now(timezone.utc), **outputs)
        except ValueError as exc:
            # JSON 이 아니거나 items 가 목록이 아닐 때만 (json.JSONDecodeError 도 ValueError). 우리 코드의 버그는 500 으로 둔다.
            # 원문은 클러스터 내용이라 남기지 않고 예외 종류·위치만 남긴다
            logger.warning("개요의 kubectl 출력을 읽지 못했다: %s", type(exc).__name__, exc_info=True)
            raise HTTPException(502, {"code": "CLUSTER_BAD_RESPONSE",
                                      "message": f"kubectl 출력을 읽지 못했다: {type(exc).__name__}"}) from None

    # 연결 확인과 같은 이유로 스레드로 넘긴다 — kubectl 이 도는 동안 서버 전체가 멈추지 않게
    return await to_thread.run_sync(collect, limiter=thread_limiter("overview", OVERVIEW_WORKERS))


def _raise_kubectl_failure(stderr: str) -> None:
    # 실패 사유는 그대로 보여줘야 고칠 수 있다 (연결 확인의 detail 과 같다). 자격증명은 원래 stderr 에 실리지 않는다
    reason = stderr.strip()[:500]
    if _failure_status(stderr) == "auth_expired":
        raise HTTPException(502, {"code": "CLUSTER_AUTH_EXPIRED",
                                  "message": f"클러스터 인증에 실패했거나 권한이 없다: {reason}"})
    raise HTTPException(502, {"code": "CLUSTER_UNREACHABLE", "message": f"클러스터에 연결할 수 없다: {reason}"})


def _failure_status(stderr: str) -> str:
    lowered = stderr.lower()
    if "unauthorized" in lowered or "forbidden" in lowered or "credential" in lowered:
        return "auth_expired"
    return "disconnected"


_server.app.include_router(router)

__all__ = ["router"]
