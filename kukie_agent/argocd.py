"""ArgoCD 관리 리소스 판별 — 가드레일 경고(DURO-129)와 ArgoCD 앱 목록·상세 API(DURO-140·DURO-145)가 같이 쓴다.

설계: Linear 문서 "[GitOps] PR 길 판단 · kubectl 경고" 1·2절. kubectl 조회만으로 판단한다 — ArgoCD API 토큰은 쓰지 않는다.

판별 순서
  1. 대상 리소스의 추적 표시 (tracking_of)
       argocd.argoproj.io/tracking-id 어노테이션  ArgoCD v3 기본. `<앱>:<group>/<Kind>:<namespace>/<name>`.
                                                  표시가 가리키는 kind·namespace·name 이 리소스와 다르면(다른 리소스를 복사해 만든 것 —
                                                  다른 namespace 로 옮겨 복사한 것 포함) 보지 않는다
       app.kubernetes.io/instance 라벨            label 추적 설정일 때. Helm 차트도 같은 라벨을 붙이므로 이것만으로는 모른다 —
                                                  그 이름의 Application 이 있고 status.resources 에 이 리소스가 있을 때만 관리로 본다
     앱 이름이 `<namespace>_<name>` 이면 "apps in any namespace" 의 Application 이다 (쿠버네티스 이름에는 `_` 가 없다)
  2. 표시가 가리키는 Application(applications.argoproj.io)을 읽는다 — repoURL·path·targetRevision·status.sourceType·sync 정책
  3. sync 정책으로 경고 수준을 정한다 (LEVELS)

못 읽으면 (read_applications 의 status)
  absent     applications.argoproj.io CRD 가 없다 — "ArgoCD 없음"
  forbidden  읽을 권한이 없다(403) — 있는지 알 수 없다. 고객 클러스터 중 v4 제한 권한인 곳이 그렇다. **조용히 건너뛴다**
  error      닿지 않음·시간 초과·모양이 다름
가드레일·읽기 툴은 어느 쪽이든 판별을 건너뛰고 지금처럼 동작한다. 하나 예외로, 어노테이션 추적 표시가 있는데 Application 을
못 찾으면(허브형 ArgoCD 가 다른 클러스터에 있거나 CRD 가 없다) 관리 리소스로 보되 정책을 모른다(unknown)고 경고한다 —
어노테이션은 ArgoCD 만 붙이기 때문이다. 권한이 없을 때(forbidden)는 그래도 건너뛴다.

kubectl 은 이 모듈의 run_kubectl 로 돈다 (inspect·note). 목록·상세 API 는 clusters_api 가 LIST_COMMAND·get_command 를 돌리고
여기 순수 함수로 정리한다.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kukie_agent.kubectl import KubectlResult, run_kubectl

logger = logging.getLogger(__name__)

TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
INSTANCE_LABEL = "app.kubernetes.io/instance"
APPLICATIONS = "applications.argoproj.io"
#: 모든 namespace 의 Application (클러스터 범위). 부르는 쪽이 `-n` 을 붙이지 않는다
LIST_COMMAND: list[str] = ["get", APPLICATIONS, "-A", "-o", "json"]

#: 앱 상세의 배포 기록 수 — ArgoCD 기본 revisionHistoryLimit 과 같다
HISTORY_LIMIT = 10
#: Application 이름(DNS subdomain)과 namespace(DNS label). URL 에서 받은 값을 kubectl 인자로 넘기기 전에 본다
_APP_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")

#: 판별(inspect)의 kubectl 한 번 시간 제한(초)과 전체 시간(초). 변경 툴마다 돌아 승인 카드가 그만큼 늦게 뜬다 — 짧게 끊는다
LOOKUP_TIMEOUT = 5
LOOKUP_BUDGET = 10
#: apply_manifest 의 리소스 중 판별해 보는 수. 넘는 것은 보지 않는다
MAX_RESOURCES = 5

#: Plan 에 남기는 한 줄 (설계 2절 "경고 후 허용")
SUMMARY = "ArgoCD 관리 리소스 직접 변경"

#: 경고 수준 — 위에 있는 것이 더 세다. 리소스가 여럿이면 가장 센 것으로 카드에 싣는다
LEVELS: dict[str, str] = {
    "self_heal": "몇 분 안에 ArgoCD가 되돌려요. 유지하려면 sync를 멈추거나 PR을 같이 올리세요.",
    "auto_sync": "다음 git 변경이 반영될 때 덮어써져요.",
    "unknown": "ArgoCD 앱 설정을 읽지 못해 되돌려질지는 알 수 없어요. 유지하려면 git 에도 같은 변경을 올리세요.",
    "manual": "자동 sync 는 꺼져 있어요. ArgoCD 화면에 OutOfSync 로 보이고, 누가 sync 하면 git 내용으로 돌아가요.",
}


@dataclass(frozen=True)
class Tracking:
    """리소스에 붙은 ArgoCD 추적 표시 하나."""
    app: str                    # Application 이름
    app_namespace: str | None   # `<namespace>_<name>` 꼴일 때만. 아니면 ArgoCD 제어 namespace(보통 argocd)라 모른다
    source: str                 # "annotation" | "label"


# ── 추적 표시 ────────────────────────────────────────────

def _split_app(value: str) -> tuple[str, str | None]:
    namespace, sep, name = value.partition("_")
    return (name, namespace) if sep and namespace and name else (value, None)


def tracking_of(resource: dict[str, Any]) -> Tracking | None:
    """리소스(kubectl -o json 한 개)의 추적 표시. 어노테이션이 먼저다. 없으면 None."""
    meta = resource.get("metadata") if isinstance(resource, dict) else None
    if not isinstance(meta, dict):
        return None
    annotations = meta.get("annotations") or {}
    labels = meta.get("labels") or {}
    tracking_id = annotations.get(TRACKING_ANNOTATION) if isinstance(annotations, dict) else None
    if isinstance(tracking_id, str) and tracking_id.strip():
        app, _, rest = tracking_id.strip().partition(":")
        # rest = "<group>/<Kind>:<namespace>/<name>". 다른 리소스를 가리키면 그 리소스를 복사해 만든 것이다 — ArgoCD 도 제 것으로 안 본다
        group_kind, _, namespaced_name = rest.partition(":")
        kind = group_kind.rpartition("/")[2]
        namespace, _, name = namespaced_name.rpartition("/")
        if (
            app
            and (not kind or kind == resource.get("kind"))
            and (not namespace or namespace == meta.get("namespace"))
            and (not name or name == meta.get("name"))
        ):
            return Tracking(*_split_app(app), source="annotation")
        return None
    instance = labels.get(INSTANCE_LABEL) if isinstance(labels, dict) else None
    if isinstance(instance, str) and instance.strip():
        return Tracking(*_split_app(instance.strip()), source="label")
    return None


# ── Application ─────────────────────────────────────────

#: API 서버에 못 닿았거나 인증이 안 된 표시. kubectl 은 discovery 가 실패하면 내장 리소스에도 "doesn't have a resource type" 을
#: 내므로(앞에 memcache.go 의 "couldn't get current server API group list" 를 찍는다) absent 보다 먼저 본다 (agent#123 리뷰).
#: "unable to retrieve the complete list of server APIs" 는 넣지 않는다 — 집계 API(metrics-server 등) 하나가 죽어도 나고, 그때도 클러스터에는 닿는다
_UNREACHABLE_MARKERS = (
    "couldn't get current server api group list",
    "unable to connect to the server",
    "unauthorized",
    "connection refused",
    "i/o timeout",
    "no such host",
    "tls handshake timeout",
    "context deadline exceeded",
)


def failure_kind(stderr: str) -> str:
    """kubectl 실패를 absent·forbidden·error 로 가른다 (모듈 설명 "못 읽으면"). 닿지 않음·인증 실패는 error 가 먼저다."""
    lowered = stderr.lower()
    if any(marker in lowered for marker in _UNREACHABLE_MARKERS):
        return "error"
    if "doesn't have a resource type" in lowered or "no matches for kind" in lowered:
        return "absent"
    if "forbidden" in lowered:
        return "forbidden"
    return "error"


def get_command(namespace: str, name: str) -> list[str]:
    """앱 상세의 Application 하나 읽기. 부르기 전에 valid_ref 로 본다."""
    return ["get", APPLICATIONS, name, "-n", namespace, "-o", "json"]


def valid_ref(namespace: str, name: str) -> bool:
    """URL 의 namespace·이름이 쿠버네티스 이름 꼴인가 — 아니면 그런 Application 은 없다. `-` 로 시작하는 값이 kubectl 옵션이 되지 않게도 막는다."""
    return bool(_NAMESPACE.fullmatch(namespace) and _APP_NAME.fullmatch(name))


def detail_failure(stderr: str, *, namespace: str, name: str) -> str:
    """앱 상세의 kubectl 실패 — failure_kind 의 셋에 not_found(그 Application 이 없다)를 더한다.
    namespace 가 없을 때도(지워졌거나 오래된 URL) kubectl 은 `namespaces "<ns>" not found` 를 낸다 — 앱이 없는 것과 같다 (agent#125 리뷰).
    kubectl 은 이름·namespace 를 따옴표로 찍는다 — 앱 이름이 "forbidden"·"unauthorized" 여도 갈래가 바뀌지 않게 지우고 본다."""
    if f'{APPLICATIONS} "{name}" not found' in stderr or f'namespaces "{namespace}" not found' in stderr:
        return "not_found"
    return failure_kind(stderr.replace(f'"{name}"', '""').replace(f'"{namespace}"', '""'))


def parse_applications(stdout: str) -> list[dict[str, Any]]:
    """Application 목록(kubectl -o json)의 items. 모양이 다르면 ValueError (json.JSONDecodeError 도 ValueError)."""
    data = json.loads(stdout or "")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("Application 목록 모양이 아니다")
    return [item for item in items if isinstance(item, dict) and (item.get("metadata") or {}).get("name")]


def _source(spec: dict[str, Any]) -> dict[str, Any]:
    """여러 소스(spec.sources)면 첫 것, 아니면 spec.source. ArgoCD 도 sources 가 비어 있지 않으면 그것을 쓴다."""
    sources = spec.get("sources")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        return sources[0]
    source = spec.get("source")
    return source if isinstance(source, dict) else {}


def sync_policy(item: dict[str, Any]) -> tuple[bool, bool, bool]:
    """(자동 sync, self-heal, prune). automated 가 있어야 자동이다 — ArgoCD 3.1 의 `enabled: false` 는 꺼진 것으로 본다."""
    policy = (item.get("spec") or {}).get("syncPolicy") or {}
    automated = policy.get("automated") if isinstance(policy, dict) else None
    if not isinstance(automated, dict) or automated.get("enabled") is False:
        return False, False, False
    return True, automated.get("selfHeal") is True, automated.get("prune") is True


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def application_row(item: dict[str, Any]) -> dict[str, Any]:
    """Application 하나 → 앱 목록 API 의 한 줄 (kukie-electron docs/api-spec.md `ArgoApplication`)."""
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    source = _source(spec)
    auto_sync, self_heal, _ = sync_policy(item)

    return {
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", ""),
        "project": spec.get("project") or "default",
        "repo_url": source.get("repoURL") or "",
        "path": _text(source.get("path")),
        "chart": _text(source.get("chart")),
        "target_revision": source.get("targetRevision") or "",
        "source_type": _text(status.get("sourceType")),
        "sync_status": (status.get("sync") or {}).get("status") or "Unknown",
        "health_status": (status.get("health") or {}).get("status") or "Unknown",
        "auto_sync": auto_sync,
        "self_heal": self_heal,
        "destination_namespace": _text((spec.get("destination") or {}).get("namespace")),
    }


def applications_response(stdout: str) -> list[dict[str, Any]]:
    """목록 API 의 applications — namespace·이름순. 모양이 다르면 ValueError."""
    rows = [application_row(item) for item in parse_applications(stdout)]
    rows.sort(key=lambda row: (row["namespace"], row["name"]))
    return rows


# ── 앱 상세 (DURO-145) ─────────────────────────────────────

def _dicts(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [entry for entry in value if isinstance(entry, str) and entry] if isinstance(value, list) else []


def _revisions(obj: Any) -> list[str]:
    """revision(소스 하나)·revisions(여러 소스) → 목록. git 소스면 커밋 sha, Helm 저장소 차트면 차트 버전이다."""
    if not isinstance(obj, dict):
        return []
    revisions = _strings(obj.get("revisions"))
    if revisions:
        return revisions
    revision = _text(obj.get("revision"))
    return [revision] if revision else []


def _initiator(obj: dict[str, Any]) -> dict[str, Any]:
    """initiatedBy → automated(ArgoCD 자동 sync)·initiated_by(사람이 눌렀으면 사용자 이름)."""
    initiated = obj.get("initiatedBy") if isinstance(obj.get("initiatedBy"), dict) else {}
    return {"automated": initiated.get("automated") is True, "initiated_by": _text(initiated.get("username"))}


def _detail_sources(spec: dict[str, Any], status: dict[str, Any]) -> list[dict[str, Any]]:
    """spec.sources(여러 소스) 또는 spec.source → 목록. source_type 은 status.sourceTypes[i]·status.sourceType."""
    multi = _dicts(spec.get("sources"))
    sources = multi or _dicts([spec.get("source")])
    types = status.get("sourceTypes") if multi else [status.get("sourceType")]
    types = types if isinstance(types, list) else []
    return [{
        "repo_url": source.get("repoURL") or "",
        "path": _text(source.get("path")),
        "chart": _text(source.get("chart")),
        "target_revision": source.get("targetRevision") or "",
        "ref": _text(source.get("ref")),
        "source_type": _text(types[index]) if index < len(types) else None,
    } for index, source in enumerate(sources)]


def _resource(entry: dict[str, Any]) -> dict[str, Any]:
    health = entry.get("health") if isinstance(entry.get("health"), dict) else {}
    return {
        "group": _text(entry.get("group")),
        "kind": entry.get("kind") or "",
        "namespace": _text(entry.get("namespace")),
        "name": entry.get("name") or "",
        "sync_status": entry.get("status") or "Unknown",
        "health_status": _text(health.get("status")),
        "health_message": _text(health.get("message")),
        "requires_pruning": entry.get("requiresPruning") is True,
    }


def _last_sync(state: Any) -> dict[str, Any] | None:
    """status.operationState → 마지막(또는 진행 중인) sync. 실패한 리소스는 syncResult 에서 SyncFailed·훅 실패만 고른다."""
    if not isinstance(state, dict) or not state:
        return None
    operation = state.get("operation") if isinstance(state.get("operation"), dict) else {}
    result = state.get("syncResult") if isinstance(state.get("syncResult"), dict) else {}
    sync = operation.get("sync") if isinstance(operation.get("sync"), dict) else {}
    failed = [{
        "group": _text(entry.get("group")),
        "kind": entry.get("kind") or "",
        "namespace": _text(entry.get("namespace")),
        "name": entry.get("name") or "",
        "status": _text(entry.get("status")),
        "message": _text(entry.get("message")),
    } for entry in _dicts(result.get("resources"))
        if entry.get("status") == "SyncFailed" or entry.get("hookPhase") in ("Failed", "Error")]
    return {
        "phase": state.get("phase") or "Unknown",
        "started_at": _text(state.get("startedAt")),
        "finished_at": _text(state.get("finishedAt")),
        "message": _text(state.get("message")),
        "revisions": _revisions(result) or _revisions(sync),    # 끝나기 전에는 syncResult 가 없다 — 요청한 revision
        "retry_count": state.get("retryCount") if isinstance(state.get("retryCount"), int) else 0,
        **_initiator(operation),
        "failed_resources": failed,
    }


def application_detail(item: dict[str, Any]) -> dict[str, Any]:
    """Application 하나 → 앱 상세 API 의 application. 목록 API 의 한 줄(application_row)에 상세 칸을 더한다.

    Application CR 에 있는 것만 싣는다 — ArgoCD API·라이브 리소스는 읽지 않는다. 그래서:
      - ArgoCD 3 은 리소스별 health 를 CR 이 아니라 app tree 에 둔다(status.resourceHealthSource = "appTree").
        그때 resources[].health_status 는 null 이고 resource_health 가 false 다 — 앱 전체 health 는 health_status 에 있다
      - revisions 는 sha 그대로다. 커밋 링크는 앱이 sources[i].repo_url 로 만든다 (Helm 저장소 차트면 차트 버전이라 링크가 아니다)
    아직 sync 하지 않아 status 가 비어 있으면 목록 칸은 Unknown·null, 나머지는 빈 목록·null 이다.
    """
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    _, _, prune = sync_policy(item)
    resources = [_resource(entry) for entry in _dicts(status.get("resources"))]
    history = _dicts(status.get("history"))[-HISTORY_LIMIT:]
    summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
    return {
        **application_row(item),
        "prune": prune,
        "health_message": _text((status.get("health") or {}).get("message")),
        "revisions": _revisions(status.get("sync")),
        "reconciled_at": _text(status.get("reconciledAt")),
        "sources": _detail_sources(spec, status),
        "resource_health": status.get("resourceHealthSource") != "appTree",
        "resources": resources,
        "out_of_sync": [entry for entry in resources if entry["sync_status"] == "OutOfSync"],
        "last_sync": _last_sync(status.get("operationState")),
        "history": [{
            "id": entry.get("id") if isinstance(entry.get("id"), int) else None,
            "revisions": _revisions(entry),
            "deploy_started_at": _text(entry.get("deployStartedAt")),
            "deployed_at": _text(entry.get("deployedAt")),
            **_initiator(entry),
        } for entry in reversed(history)],
        "images": _strings(summary.get("images")),
        "conditions": [{
            "type": entry.get("type") or "",
            "message": _text(entry.get("message")),
            "last_transition_time": _text(entry.get("lastTransitionTime")),
        } for entry in _dicts(status.get("conditions"))],
    }


def application_detail_response(stdout: str) -> dict[str, Any]:
    """상세 API 의 application (kubectl get <이름> -o json). 모양이 다르면 ValueError."""
    item = json.loads(stdout or "")
    if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict) or not item["metadata"].get("name"):
        raise ValueError("Application 모양이 아니다")
    return application_detail(item)


def find_application(items: list[dict[str, Any]], tracking: Tracking) -> dict[str, Any] | None:
    """추적 표시가 가리키는 Application. namespace 를 모르면 이름으로만 찾고, 여럿이면 모른다(None)."""
    found = [
        item for item in items
        if item["metadata"].get("name") == tracking.app
        and (tracking.app_namespace is None or item["metadata"].get("namespace") == tracking.app_namespace)
    ]
    return found[0] if len(found) == 1 else None


def manages(item: dict[str, Any], resource: dict[str, Any]) -> bool:
    """Application 의 status.resources 에 그 리소스가 있는가 — label 추적 표시를 믿어도 되는지 본다."""
    meta = resource.get("metadata") or {}
    for entry in (item.get("status") or {}).get("resources") or []:
        if (
            isinstance(entry, dict)
            and entry.get("kind") == resource.get("kind")
            and entry.get("name") == meta.get("name")
            and (entry.get("namespace") or "") == (meta.get("namespace") or "")
        ):
            return True
    return False


def _level(item: dict[str, Any] | None) -> str:
    if item is None:
        return "unknown"
    auto_sync, self_heal, _ = sync_policy(item)
    return "self_heal" if self_heal else "auto_sync" if auto_sync else "manual"


def _app_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Plan·승인 카드에 싣는 Application 칸 — 목록 API 의 한 줄에서 화면에 쓸 것만, prune 을 더해."""
    row = application_row(item)
    _, _, prune = sync_policy(item)
    keys = ("name", "namespace", "repo_url", "path", "chart", "target_revision", "source_type",
            "sync_status", "auto_sync", "self_heal")
    return {**{key: row[key] for key in keys}, "prune": prune}


# ── 판별 (가드레일·읽기 툴) ─────────────────────────────────

def inspect(
    targets: list[dict[str, str]], *, context: str, kubeconfig: Path | None = None,
    timeout: float = LOOKUP_TIMEOUT, budget: float = LOOKUP_BUDGET, clock=time.monotonic,
) -> dict[str, Any] | None:
    """대상 리소스 가운데 ArgoCD 가 관리하는 것이 있으면 Plan 의 argocd 칸, 없거나 모르면 None.

    targets 는 {kind, name, namespace?} — 가드레일의 PlanTarget 과 같은 모양. 앞에서 MAX_RESOURCES 개만 본다.
    없는 리소스(apply 로 새로 만드는 것)·못 읽는 리소스는 건너뛴다. Application 목록은 추적 표시가 하나라도 있을 때만 한 번 읽는다.
    어떤 실패도 올리지 않는다 — 판별은 경고를 붙이는 일이지 실행을 막는 일이 아니다.

        {"summary": SUMMARY, "level": LEVELS 의 키, "warning": 문구,
         "applications": [_app_summary …],                  # 읽은 Application (unknown 이면 비어 있을 수 있다)
         "resources": [{kind, name, namespace?, application}]}
    """
    started = clock()

    def run(args: list[str]) -> KubectlResult | None:
        # 한 번의 제한도 남은 시간으로 줄인다 — 아니면 끝 무렵에 시작한 한 번이 전체 시간을 timeout 만큼 넘긴다 (agent#123 리뷰)
        left = budget - (clock() - started)
        if left <= 0:
            return None
        return run_kubectl(args, context=context, kubeconfig=kubeconfig, timeout=min(timeout, left))

    tracked: list[tuple[dict[str, str], dict[str, Any], Tracking]] = []
    for target in targets[:MAX_RESOURCES]:
        args = ["get", target["kind"], target["name"], "-o", "json"]
        if target.get("namespace"):
            args += ["-n", target["namespace"]]
        result = run(args)
        if result is None or not result.success:
            continue
        try:
            resource = json.loads(result.stdout)
        except ValueError:
            continue
        tracking = tracking_of(resource) if isinstance(resource, dict) else None
        if tracking is not None:
            tracked.append((target, resource, tracking))
    if not tracked:
        return None

    result = run(LIST_COMMAND)
    if result is None:
        status, items = "error", []
    elif result.success:
        try:
            status, items = "ok", parse_applications(result.stdout)
        except ValueError:
            status, items = "error", []
    else:
        status, items = failure_kind(result.stderr or result.stdout), []
    if status == "forbidden":
        logger.debug("ArgoCD Application 을 읽을 권한이 없어 판별을 건너뛴다: context=%s", context)
        return None

    resources: list[dict[str, Any]] = []
    applications: dict[tuple[str, str], dict[str, Any]] = {}
    levels: list[str] = []
    for target, resource, tracking in tracked:
        item = find_application(items, tracking)
        if tracking.source == "label" and (item is None or not manages(item, resource)):
            continue     # Helm 의 instance 라벨일 뿐이다
        if item is not None:
            key = (item["metadata"].get("namespace", ""), item["metadata"]["name"])
            applications.setdefault(key, _app_summary(item))
        levels.append(_level(item))
        resources.append({**target, "application": tracking.app})
    if not resources:
        return None
    level = min(levels, key=list(LEVELS).index)
    return {
        "summary": SUMMARY,
        "level": level,
        "warning": LEVELS[level],
        "applications": list(applications.values()),
        "resources": resources,
    }


def note(found: dict[str, Any]) -> str:
    """읽기 툴 결과에 같이 싣는 한 줄 — 모델이 이 리소스를 바꾸기 전에 ArgoCD 가 관리한다는 것을 안다 (설계 6-1 ③)."""
    apps = found["applications"]
    if apps:
        parts = []
        for app in apps:
            source = " ".join(filter(None, (app["repo_url"], app["path"] or app["chart"])))
            policy = "self-heal 켜짐" if app["self_heal"] else "자동 sync" if app["auto_sync"] else "수동 sync"
            parts.append(f"{app['name']} (소스 {source} @ {app['target_revision'] or 'HEAD'}, {policy})")
        owners = ", ".join(parts)
    else:
        owners = ", ".join(sorted({r["application"] for r in found["resources"]}))
    return f"ArgoCD 앱 {owners} 가 관리하는 리소스다. kubectl 로 바꾸면: {found['warning']}"
