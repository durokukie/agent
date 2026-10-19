"""클러스터 개요 — 웹 대시보드의 "클러스터 지도"·통계가 읽는 모양으로 kubectl JSON 을 정리한다 (issue #102).

kubectl 은 clusters_api.py 가 돌리고, 여기는 그 출력(JSON 문자열)을 받아 응답을 만드는 순수 함수만 둔다.
지금 시각(now)도 인자로 받아 같은 입력이면 늘 같은 결과다 — 테스트가 JSON 픽스처로 그대로 확인한다.

조회 범위는 클러스터에 등록된 namespace 하나다. EKS 고객 클러스터는 namespace 범위 Edit 만 주므로
`-A`·`get nodes` 같은 클러스터 범위 조회는 하지 않는다 (READ_COMMANDS 가 그 목록이다).

상태 판정 (웹의 Available·Progressing·Degraded)
  - Available    ready ≥ desired 이고 새 버전으로 다 바뀌었다 (desired 0 도 여기)
  - Progressing  ready < desired 이지만 롤아웃이 진행 중이다
                   공통: metadata.generation 을 컨트롤러가 아직 못 봤다(observedGeneration 이 작다) 또는 updated < desired
                   StatefulSet: currentRevision ≠ updateRevision 도 진행 중으로 본다
  - Degraded     ready < desired 인데 진행 중인 롤아웃이 없다 (새 버전으로 다 바뀐 뒤 파드가 죽는다 등)
                   Deployment 는 Progressing 조건이 False(ProgressDeadlineExceeded — 기본 600초 안에 못 끝냄)여도 Degraded
  롤아웃 중에 파드가 계속 죽으면 Deployment 는 진행 기한(progressDeadlineSeconds)까지 Progressing 이다가 Degraded 가 되고,
  StatefulSet·DaemonSet 은 기한 조건이 없어 updated 가 desired 에 닿을 때까지 Progressing 으로 남는다 — 단순하게 둔 것이다.

파드 phase
  kubelet 이 주는 status.phase 를 그대로 쓰되, 컨테이너(init 포함) 하나라도 BROKEN_WAITING 의 대기 사유에 걸려 있으면
  "Failed" 로 바꾼다. CrashLoopBackOff 파드는 kubernetes 에서는 Running(또는 Pending)이지만, 대시보드는 Failed 를
  문제로 센다 — 문제를 정상으로 세지 않게 하려는 것이다. 모르는 phase 는 "Unknown".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

#: 개요에 쓰는 읽기 전용 kubectl 인자 (namespace 는 부르는 쪽이 `-n` 으로 붙인다). 순서대로 돌린다.
READ_COMMANDS: dict[str, list[str]] = {
    "workloads": ["get", "deployments,statefulsets,daemonsets", "-o", "json"],
    "pods": ["get", "pods", "-o", "json"],
    "events": ["get", "events", "-o", "json"],
}

#: 응답에 싣는 이벤트 수 (최근 것부터)
MAX_EVENTS = 20

#: 이 대기 사유에 걸린 컨테이너가 있으면 파드를 Failed 로 센다 (모듈 설명 "파드 phase")
BROKEN_WAITING = frozenset({
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError",
})

PHASES = frozenset({"Running", "Pending", "Succeeded", "Failed", "Unknown"})
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def build(
    *, context: str, namespace: str, workloads: str, pods: str, events: str, now: datetime,
) -> dict[str, Any]:
    """kubectl 세 번의 stdout(JSON) 으로 ClusterOverview 응답을 만든다."""
    return {
        "context": context,
        "namespace": namespace,
        "at": iso(now),
        "workloads": parse_workloads(_items(workloads), now),
        "pods": parse_pods(_items(pods), now),
        "events": parse_events(_items(events)),
    }


# ── 워크로드 ──────────────────────────────────────────────

def parse_workloads(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("kind")
        if kind not in WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        ready, desired, status = workload_status(item)
        rows.append({
            "kind": kind,
            "name": meta.get("name", ""),
            "ready": ready,
            "desired": desired,
            "status": status,
            "age": age(meta.get("creationTimestamp"), now),
        })
    rows.sort(key=lambda r: (WORKLOAD_KINDS.index(r["kind"]), r["name"]))
    return rows


def workload_status(item: dict[str, Any]) -> tuple[int, int, str]:
    """(ready, desired, 상태) — 판정 규칙은 모듈 설명."""
    kind = item.get("kind")
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}

    if kind == "DaemonSet":
        desired = _int(status.get("desiredNumberScheduled"))
        ready = _int(status.get("numberReady"))
        updated = _int(status.get("updatedNumberScheduled"))
    else:
        desired = _int(spec.get("replicas"), default=1)   # 생략하면 1 (kubernetes 기본값)
        ready = _int(status.get("readyReplicas"))
        updated = _int(status.get("updatedReplicas"))

    stale = _int(status.get("observedGeneration")) < _int(meta.get("generation"))
    rolling = stale or updated < desired
    if kind == "StatefulSet" and status.get("updateRevision"):
        rolling = rolling or status.get("currentRevision") != status.get("updateRevision")

    if ready >= desired and not rolling:
        return ready, desired, "Available"
    if kind == "Deployment" and _condition(status, "Progressing") == "False":
        return ready, desired, "Degraded"
    if ready >= desired:
        return ready, desired, "Progressing"       # 다 떠 있지만 새 버전으로 바꾸는 중
    return ready, desired, "Progressing" if rolling else "Degraded"


# ── 파드 ─────────────────────────────────────────────────

def parse_pods(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        containers = status.get("containerStatuses") or []
        rows.append({
            "name": meta.get("name", ""),
            "phase": pod_phase(status),
            "restarts": sum(_int(c.get("restartCount")) for c in containers),
            "node": (item.get("spec") or {}).get("nodeName") or "",
            "age": age(meta.get("creationTimestamp"), now),
        })
    rows.sort(key=lambda r: r["name"])
    return rows


def pod_phase(status: dict[str, Any]) -> str:
    """status.phase, 단 BROKEN_WAITING 에 걸린 컨테이너가 있으면 Failed (모듈 설명 "파드 phase")."""
    for container in [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]:
        waiting = (container.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in BROKEN_WAITING:
            return "Failed"
    phase = status.get("phase")
    return phase if phase in PHASES else "Unknown"


# ── 이벤트 ───────────────────────────────────────────────

def parse_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """최근 것부터 MAX_EVENTS 개. 시각을 모르는 이벤트는 맨 뒤로."""
    rows: list[tuple[datetime | None, dict[str, Any]]] = []
    for item in items:
        involved = item.get("involvedObject") or item.get("regarding") or {}
        when = _event_time(item)
        rows.append((when, {
            "type": "Warning" if item.get("type") == "Warning" else "Normal",
            "reason": item.get("reason") or "",
            "object": f"{str(involved.get('kind') or '').lower()}/{involved.get('name') or ''}",
            "message": (item.get("message") or item.get("note") or "").strip(),
            "at": iso(when) if when else "",
        }))
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: r[0] or epoch, reverse=True)
    return [row for _, row in rows[:MAX_EVENTS]]


def _event_time(item: dict[str, Any]) -> datetime | None:
    """마지막으로 일어난 시각. core/v1 은 lastTimestamp, 새 API 로 만든 이벤트는 series·eventTime 에 있다."""
    series = item.get("series") or {}
    for value in (series.get("lastObservedTime"), item.get("lastTimestamp"), item.get("eventTime"),
                  item.get("firstTimestamp"), (item.get("metadata") or {}).get("creationTimestamp")):
        parsed = parse_time(value)
        if parsed is not None:
            return parsed
    return None


# ── 공통 ─────────────────────────────────────────────────

def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(when: datetime) -> str:
    """UTC ISO8601, 초 단위, 끝은 Z (예 2026-10-19T03:04:05Z)."""
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def age(created: Any, now: datetime) -> str:
    """kubectl AGE 처럼 가장 큰 단위 하나 — 45s, 25m, 3h, 2d. 시각을 모르면 빈 문자열."""
    start = parse_time(created)
    if start is None:
        return ""
    seconds = max(0, int((now - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _items(stdout: str) -> list[dict[str, Any]]:
    """`kubectl get -o json` 의 List. 모양이 다르면 ValueError — 부르는 쪽이 오류 응답으로 바꾼다."""
    data = json.loads(stdout or "{}")
    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError("kubectl 출력의 items 가 목록이 아니다")
    return [item for item in items if isinstance(item, dict)]


def _condition(status: dict[str, Any], kind: str) -> str | None:
    for condition in status.get("conditions") or []:
        if condition.get("type") == kind:
            return condition.get("status")
    return None


def _int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
