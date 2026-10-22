"""클러스터 개요 — 웹 대시보드의 "클러스터 지도"·통계가 읽는 모양으로 kubectl JSON 을 정리한다 (issue #102).

kubectl 은 clusters_api.py 가 돌리고, 여기는 그 출력(JSON 문자열)을 받아 응답을 만드는 순수 함수만 둔다.
지금 시각(now)도 인자로 받아 같은 입력이면 늘 같은 결과다 — 테스트가 JSON 픽스처로 그대로 확인한다.

조회 범위는 클러스터에 등록된 namespace 하나다. EKS 고객 클러스터는 namespace 범위 Edit 만 주므로
`-A`·`get nodes` 같은 클러스터 범위 조회는 하지 않는다 (READ_COMMANDS 가 그 목록이다).
예외는 CNI 를 알아보는 NETWORK_COMMAND 하나다 — kube-system 의 DaemonSet 만 읽고, 못 읽으면 network 를 null 로 둔다.

네트워크(CNI) — network
  EKS 애드온이 아닌 CNI(예: Helm 으로 깐 Cilium)도 대시보드가 보이도록 kube-system 의 DaemonSet 이름으로 알아본다.
  CNI_DAEMONSETS 에서 위에 있는 것이 먼저다. Cilium·Calico 를 aws-node(Amazon VPC CNI)보다 앞에 둔 것은
  Cilium 이 vpc-cni 위에 얹혀(chaining) 돌 때도 aws-node 가 남아 있기 때문이다 — 둘 다 있으면 Cilium 이라고 답한다.
  (Calico 를 네트워크 정책 용도로만 vpc-cni 와 함께 쓰는 경우도 Calico 로 보인다 — 단순하게 둔 것이다.)
  버전은 그 DaemonSet 의 첫 컨테이너 이미지 태그다 (`quay.io/cilium/cilium:v1.20.3@sha256:…` → `1.20.3`).
  태그가 없거나 latest 이면 null. kube-system 밖에 까는 설치(tigera-operator 의 calico-system,
  새 Flannel 의 kube-flannel 등)는 보지 않는다 — 그때도 network 는 null 이다.

상태 판정 (웹의 Available·Progressing·Degraded)
  - Available    ready ≥ desired 이고 새 버전으로 다 바뀌었다 (desired 0 도 여기)
  - Progressing  ready < desired 이지만 롤아웃이 진행 중이다
                   공통: metadata.generation 을 컨트롤러가 아직 못 봤다(observedGeneration 이 작다) 또는 updated < desired
                   StatefulSet: currentRevision ≠ updateRevision 도 진행 중으로 본다
                   StatefulSet 의 partition, StatefulSet·DaemonSet 의 OnDelete 는 옛 리비전이 남는 게 정상이라 그만큼은 진행 중으로 보지 않는다
  - Degraded     ready < desired 인데 진행 중인 롤아웃이 없다 (새 버전으로 다 바뀐 뒤 파드가 죽는다 등)
  Deployment 는 숫자 대신 Progressing 조건을 먼저 본다 — updatedReplicas 는 Ready 와 무관하게 파드가 생기면 올라서
  숫자만으로는 "막 뜨는 중" 과 "다 바뀐 뒤 죽음" 이 갈리지 않는다.
    False(ProgressDeadlineExceeded — 기본 600초 안에 못 끝냄)       → Degraded
    True/NewReplicaSetAvailable(롤아웃을 끝냈다) 인데 ready < desired → Degraded (끝난 뒤 파드가 죽었다)
    그 밖의 True(NewReplicaSetCreated·ReplicaSetUpdated …)          → Progressing
  롤아웃 중에 파드가 계속 죽으면 Deployment 는 진행 기한까지 Progressing 이다가 Degraded 가 되고,
  StatefulSet·DaemonSet 은 기한 조건이 없어 updated 가 다 찰 때까지 Progressing 으로 남는다 — 단순하게 둔 것이다.
  진행 기한을 끈 Deployment(progressDeadlineSeconds 2147483647)도 조건이 False 로 뒤집히지 않아 롤아웃이 망가져도
  Degraded 로 넘어가지 않고 Progressing 으로 남는다.

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

#: CNI 조회 (모듈 설명 "네트워크"). namespace 를 이미 담고 있어 부르는 쪽이 `-n` 을 붙이지 않는다.
#: 고객 클러스터에서는 Forbidden 이 흔하다 — 실패해도 개요 전체를 실패로 만들지 않고 network 만 null 이다.
NETWORK_COMMAND: list[str] = ["get", "daemonsets", "-o", "json", "-n", "kube-system"]

#: kube-system DaemonSet 이름 → 표시 이름. 위에 있는 것이 먼저다 (모듈 설명 "네트워크")
CNI_DAEMONSETS: tuple[tuple[str, str], ...] = (
    ("cilium", "Cilium"),
    ("calico-node", "Calico"),
    ("antrea-agent", "Antrea"),
    ("kube-flannel-ds", "Flannel"),
    ("kube-flannel", "Flannel"),
    ("weave-net", "Weave"),
    ("aws-node", "Amazon VPC CNI"),
)

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
    network: str | None = None,
) -> dict[str, Any]:
    """READ_COMMANDS 세 번의 stdout(JSON) 으로 ClusterOverview 응답을 만든다.

    network 는 NETWORK_COMMAND 의 stdout 이다. 그 조회가 실패했으면 None 을 넘긴다 — 응답의 network 는 null.
    """
    return {
        "context": context,
        "namespace": namespace,
        "at": iso(now),
        "workloads": parse_workloads(_items(workloads), now),
        "pods": parse_pods(_items(pods), now),
        "events": parse_events(_items(events)),
        "network": parse_network(network),
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
    expected_updated, check_revision = desired, True
    strategy = spec.get("updateStrategy") or {}
    if kind in ("StatefulSet", "DaemonSet") and strategy.get("type") == "OnDelete":
        expected_updated, check_revision = 0, False           # 사람이 파드를 지울 때까지 옛 리비전이 정상이다
    elif kind == "StatefulSet":
        partition = _int((strategy.get("rollingUpdate") or {}).get("partition"))
        expected_updated = max(0, desired - partition)        # partition 아래 순번은 옛 리비전에 두는 게 정상이다
        check_revision = partition == 0
    rolling = stale or updated < expected_updated
    if check_revision and kind == "StatefulSet" and status.get("updateRevision"):
        rolling = rolling or status.get("currentRevision") != status.get("updateRevision")

    if ready >= desired and not rolling:
        return ready, desired, "Available"
    if kind == "Deployment":
        progressing = _condition(status, "Progressing")
        if progressing is not None:
            if progressing.get("status") == "False":
                return ready, desired, "Degraded"             # ProgressDeadlineExceeded
            if ready < desired and progressing.get("reason") == "NewReplicaSetAvailable" and not rolling:
                return ready, desired, "Degraded"             # 롤아웃을 끝낸 뒤에 파드가 죽었다
            # 그 밖의 사유(NewReplicaSetCreated·FoundNewReplicaSet·ReplicaSetUpdated)는 진행 중이다.
            # updatedReplicas 는 Ready 와 무관하게 파드가 생기면 오르므로 숫자만 보면 막 뜨는 중인 것도 Degraded 가 된다
            return ready, desired, "Progressing"
    if ready >= desired:
        return ready, desired, "Progressing"       # 다 떠 있지만 새 버전으로 바꾸는 중
    return ready, desired, "Progressing" if rolling else "Degraded"


# ── 파드 ─────────────────────────────────────────────────

def parse_pods(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        # kubectl 의 RESTARTS 처럼 init(사이드카 포함) 컨테이너도 센다 — pod_phase 도 init 을 본다
        containers = [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]
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


# ── 네트워크(CNI) ──────────────────────────────────────────

def parse_network(stdout: str | None) -> dict[str, Any] | None:
    """kube-system DaemonSet 목록에서 CNI 를 찾는다 (모듈 설명 "네트워크"). 못 찾거나 못 읽으면 None.

    다른 칸과 달리 깨진 출력도 오류로 올리지 않는다 — 부가 정보라 개요 전체를 502 로 만들 까닭이 없다.
    """
    if stdout is None:
        return None
    try:
        items = _items(stdout)
    except ValueError:
        return None
    by_name = {(item.get("metadata") or {}).get("name"): item for item in items}
    for daemonset, display in CNI_DAEMONSETS:
        item = by_name.get(daemonset)
        if item is not None:
            return {"name": display, "version": image_version(_first_image(item)), "source": "daemonset"}
    return None


def image_version(image: str | None) -> str | None:
    """이미지 참조의 태그에서 버전을 뗀다. `repo/cilium:v1.20.3@sha256:…` → `1.20.3`. 태그가 없거나 latest 면 None."""
    if not image:
        return None
    name = image.split("@", 1)[0]                 # 다이제스트를 뗀다
    last = name.rsplit("/", 1)[-1]                # registry:5000/… 의 포트를 태그로 읽지 않게 마지막 조각만 본다
    if ":" not in last:
        return None
    tag = last.split(":", 1)[1]
    if not tag or tag == "latest":
        return None
    return tag[1:] if tag[0] in "vV" and len(tag) > 1 and tag[1].isdigit() else tag


def _first_image(item: dict[str, Any]) -> str | None:
    pod_spec = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = [c for c in pod_spec.get("containers") or [] if isinstance(c, dict)]
    image = containers[0].get("image") if containers else None
    return image if isinstance(image, str) else None


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


def _condition(status: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for condition in status.get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == kind:
            return condition
    return None


def _int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
