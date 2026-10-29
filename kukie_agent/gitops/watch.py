"""머지 뒤 지켜보기 — ArgoCD Application 이 머지 커밋으로 sync 되고 건강해지는지 본다 (검증 09, DURO-134).

ArgoCD 는 기본 3분마다 git 을 읽는다(웹훅이 있으면 바로). 그래서 정해진 시간(SYNC_WAIT)만 기다리고, 그 안에 끝나지 않으면
"아직" 으로 보고한다 — 머지는 이미 됐으니 실패가 아니다. Application 의 refresh 어노테이션은 붙이지 않는다(쓰기 권한·승인 밖의 변경).

결과(sync_result) — Plan 의 executionResult.sync_result 와 승인 뒤 툴 결과에 그대로 싣는다
    {"status": healthy | sync_failed | degraded | timeout | manual | unreadable,
     "sync_status": "Synced" …, "health_status": "Healthy" …, "revision": 마지막으로 본 sync 커밋,
     "message": 사람이 읽는 한 줄, "waited_seconds": 기다린 시간}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from kukie_agent.kubectl import run_kubectl

#: 기다리는 최대 시간(초). 승인 요청 하나가 이만큼 길어진다 — 앱의 요청 시간 제한보다 짧아야 한다
SYNC_WAIT = float(os.environ.get("KUKIE_GITOPS_SYNC_WAIT") or 180)
POLL = 5.0
ATTEMPT_TIMEOUT = 10


def _status(item: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    status = item.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    return (str(sync.get("status") or "Unknown"), str(health.get("status") or "Unknown"),
            str(sync.get("revision") or ""), status.get("operationState") or {})


def _result(status: str, message: str, sync: str, health: str, revision: str, waited: float) -> dict[str, Any]:
    return {"status": status, "sync_status": sync, "health_status": health, "revision": revision,
            "message": message, "waited_seconds": round(waited)}


def watch(
    name: str, namespace: str, revision: str, *, auto_sync: bool, context: str, kubeconfig: Path | None,
    wait: float | None = None, poll: float = POLL, clock=time.monotonic, sleep=time.sleep,
) -> dict[str, Any]:
    """머지 커밋 revision 으로 Synced + Healthy 가 될 때까지 (최대 wait 초). 블로킹 — 부르는 쪽이 스레드로 돌린다."""
    wait = SYNC_WAIT if wait is None else wait
    if not auto_sync:
        return _result("manual", "자동 sync 가 꺼진 앱이에요. ArgoCD 에서 sync 해야 클러스터에 반영돼요.",
                       "Unknown", "Unknown", "", 0)
    started = clock()
    sync = health = "Unknown"
    seen = ""
    while True:
        waited = clock() - started
        result = run_kubectl(["get", "applications.argoproj.io", name, "-n", namespace, "-o", "json"],
                             context=context, kubeconfig=kubeconfig, timeout=ATTEMPT_TIMEOUT)
        item: dict[str, Any] | None = None
        if result.success:
            try:
                item = json.loads(result.stdout)
            except ValueError:
                item = None
        if isinstance(item, dict):
            sync, health, seen, operation = _status(item)
            op_revision = str(((operation.get("syncResult") or {}).get("revision")) or "")
            phase = str(operation.get("phase") or "")
            if op_revision == revision and phase in ("Failed", "Error"):
                return _result("sync_failed", f"ArgoCD sync 가 실패했어요: {operation.get('message') or phase}",
                               sync, health, op_revision, waited)
            if seen == revision and sync == "Synced":
                if health == "Healthy":
                    return _result("healthy", "머지한 커밋으로 sync 됐고 앱이 정상(Healthy)이에요.",
                                   sync, health, seen, waited)
                if health in ("Degraded", "Missing"):
                    message = ((item.get("status") or {}).get("health") or {}).get("message") or health
                    return _result("degraded", f"sync 는 됐지만 앱 상태가 {health} 예요: {message}",
                                   sync, health, seen, waited)
        elif waited >= wait:
            return _result("unreadable", "ArgoCD 앱 상태를 읽지 못했어요.", sync, health, seen, waited)
        if waited >= wait:
            if seen != revision:
                message = "아직 ArgoCD 가 머지한 커밋을 가져가지 않았어요 (보통 3분 안에 가져가요)."
            else:
                message = f"sync 는 시작됐지만 아직 끝나지 않았어요 (sync {sync}, 상태 {health})."
            return _result("timeout", message, sync, health, seen, waited)
        sleep(min(poll, max(0.0, wait - waited)) or poll)
