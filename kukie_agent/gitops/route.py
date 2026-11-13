"""PR 길 판단 — 리소스마다 PR 로 바꿀지 kubectl 로 바꿀지 정한다 (설계 "[GitOps] PR 길 판단 · kubectl 경고" 1절).

| 결과                                            | 길              |
| ArgoCD 관리 + 레포 연결됨 + 지원 형식·요청       | PR              |
| ArgoCD 관리 + 레포 미연결, 또는 지원 안 하는 것   | kubectl + 경고  |
| ArgoCD 관리 아님                                 | kubectl         |
| 새 리소스 — 고른 앱 + 레포 연결됨 + 지원 형식     | PR              |
| 새 리소스 — 앱 없음·레포 미연결·Helm 등           | 이유와 다른 길   |
| 클러스터에 없음 — 앱 하나의 status.resources 에 있음 | 그 앱으로 위 규칙 |
| 클러스터에 없음 — 어느 앱에도 없음·여러 앱        | kubectl         |

새 리소스(DURO-154)는 아직 클러스터에 없어 추적 표시로 길을 못 정한다. 요청이 고른 ArgoCD 앱을 읽어 같은 규칙으로 판단한다.
git 에는 있는데 sync 가 실패해 클러스터에 없는 리소스(DURO-248)도 추적 표시가 없다. Application 의 status.resources 에서 찾는다.

클러스터 설정은 없다 — 레포 연결이 끊기거나 새로 생기면 다음 판단부터 달라질 뿐이다. 그래서 승인 순간에도 다시 부른다
(gitops_hook — 길이 바뀌었으면 STALE). kubectl 조회와 kukie-server 의 연결 목록으로만 판단한다. ArgoCD API 토큰은 쓰지 않는다.

지원 형식은 plain YAML(Directory)·Kustomize, 요청은 manifests 의 넷(레플리카·이미지 태그·resources·환경변수)과
경로 지정 변경(patch — plain YAML 은 문서를 고치고 Kustomize 는 overlay 에 patch 파일을 건다, DURO-249·251)이다.
targetRevision 이 브랜치인지(태그·커밋이면 PR 길이 없다)는 GitHub 에 물어야 알아서 여기서 보지 않는다 — PR 을 만들 때 본다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

from anyio import to_thread

from kukie_agent import argocd, server_api
from kukie_agent.gitops.repo import repo_name
from kukie_agent.kubectl import KubectlResult, run_kubectl, thread_limiter
from kukie_agent.manifests import AppSource, Change, NewResource, Other, Target
from kukie_agent.manifests.models import KIND_SHAPE, NAME_SHAPE

logger = logging.getLogger(__name__)

#: PR 로 고칠 수 있는 ArgoCD 형식 (status.sourceType). Helm 은 DURO-137
SUPPORTED_SOURCES = ("Directory", "Kustomize")
LOOKUP_TIMEOUT = 10
#: 동시에 도는 판단 조회 상한 — argocd 판별(ARGOCD_LOOKUP_WORKERS)과 같은 이유로 따로 묶는다
LOOKUP_WORKERS = 8
#: 앱을 찾지 못했을 때 문구에 싣는 앱 이름 수 · 새 리소스가 클러스터에 이미 있는지 보는 수·전체 시간(초)
SHOW_APPS = 20
LIVE_CHECKS = 20
LIVE_BUDGET = 10

RoutePath = Literal["pr", "kubectl"]


@dataclass(frozen=True)
class Route:
    path: RoutePath
    reason: str                                    # 사람과 AI 가 읽는 이유 — ModelRetry·승인 카드·메모에 그대로 쓴다
    managed: bool = False                          # ArgoCD 가 관리하는 리소스인가
    repository: str | None = None                  # Application repoURL 의 `owner/repo` (GitHub 일 때)
    application: dict[str, Any] | None = None      # 그 Application (kubectl -o json 한 개)
    target: Target | None = None                   # 고칠 리소스 (apiVersion 포함)
    source: AppSource | None = None
    revision: str = ""                             # spec.source.targetRevision
    # False 면 길을 **정하지 못했다**(조회 실패·시간 초과·권한) — kubectl 길은 "모르니 지금처럼" 이라는 뜻일 뿐, 길이 바뀐
    # 증거가 아니다. 승인 순간 다시 판단할 때 이걸 STALE 로 읽으면 일시적 실패로 승인한 변경을 버린다 (agent#126 리뷰)
    known: bool = True

    @property
    def app_key(self) -> dict[str, str] | None:
        if self.application is None:
            return None
        meta = self.application.get("metadata") or {}
        return {"name": str(meta.get("name") or ""), "namespace": str(meta.get("namespace") or "")}

    @property
    def destination_key(self) -> dict[str, str] | None:
        """spec.destination 전체 — 네임스페이스만 보면 다른 클러스터로 바뀐 것을 놓친다 (agent#132 리뷰)."""
        if self.application is None:
            return None
        destination = (self.application.get("spec") or {}).get("destination") or {}
        return {key: str(destination.get(key) or "") for key in ("server", "name", "namespace")}

    def fingerprint(self) -> dict[str, Any]:
        """승인 순간에 다시 판단한 것과 맞춰 볼 칸 — 하나라도 다르면 승인한 전제가 깨졌다 (STALE)."""
        return {
            "path": self.path,
            "repository": self.repository,
            "application": self.app_key,
            "source_path": self.source.path if self.source else None,
            "source_type": self.source.source_type if self.source else None,
            "revision": self.revision,
            "destination": self.destination_key,
        }


_TIMED_OUT = KubectlResult(command="", stdout="", stderr="PR 길 판단 시간이 다 됐다", success=False)


def _kubectl(args: list[str], *, context: str, kubeconfig: Path | None, deadline: float | None = None) -> KubectlResult:
    """한 번의 제한을 남은 시간으로 줄인다 — 스레드 안의 kubectl 은 밖에서 취소할 수 없어 예산은 여기서 지킨다."""
    timeout: float = LOOKUP_TIMEOUT
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            return _TIMED_OUT
        timeout = min(timeout, left)
    return run_kubectl(args, context=context, kubeconfig=kubeconfig, timeout=timeout)


def _read(args: list[str], *, context: str, kubeconfig: Path | None,
          deadline: float | None = None) -> dict[str, Any] | None:
    result = _kubectl(args, context=context, kubeconfig=kubeconfig, deadline=deadline)
    if not result.success:
        if "notfound" in (result.stderr or "").lower().replace(" ", ""):
            return {}                                  # 없다 — 읽기는 됐다
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _source_of(item: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(spec.source, 못 쓰는 이유). 여러 소스(spec.sources)는 아직 PR 길이 없다."""
    spec = item.get("spec") or {}
    if isinstance(spec.get("sources"), list) and spec["sources"]:
        return None, "소스가 여러 개인 ArgoCD 앱이라 PR 로 못 바꿔요."
    source = spec.get("source")
    if not isinstance(source, dict):
        return None, "ArgoCD 앱에 소스(spec.source)가 없어요."
    return source, None


def app_source(item: dict[str, Any], source: dict[str, Any]) -> AppSource:
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    options = ((spec.get("syncPolicy") or {}).get("syncOptions") or [])
    source_type = str(status.get("sourceType") or ("Helm" if source.get("chart") else ""))
    destination = spec.get("destination") or {}
    return AppSource(
        source_type=source_type,
        path=str(source.get("path") or ""),
        directory_recurse=bool((source.get("directory") or {}).get("recurse")),
        directory_filtered=bool((source.get("directory") or {}).get("include")
                                or (source.get("directory") or {}).get("exclude")),
        ignore_differences=[entry for entry in spec.get("ignoreDifferences") or [] if isinstance(entry, dict)],
        respect_ignore_differences=any(
            isinstance(option, str) and option.replace(" ", "") == "RespectIgnoreDifferences=true" for option in options
        ),
        project=str(spec.get("project") or "default"),
        destination_server=str(destination.get("server") or ""),
        destination_name=str(destination.get("name") or ""),
    )


def _locate(kind: str, name: str, namespace: str | None, *, context: str,
            kubeconfig: Path | None, deadline: float | None = None) -> tuple[dict | None, argocd.Tracking | None, dict | None, str]:
    """(리소스, 추적 표시, Application, Application 을 못 읽은 이유). 블로킹 — 스레드에서 돈다."""
    args = ["get", kind, name, "-o", "json", *(["-n", namespace] if namespace else [])]
    resource = _read(args, context=context, kubeconfig=kubeconfig, deadline=deadline)
    if not resource:
        return None, None, None, "missing" if resource == {} else "error"
    tracking = argocd.tracking_of(resource)
    if tracking is None:
        return resource, None, None, ""
    listed = _kubectl(argocd.LIST_COMMAND, context=context, kubeconfig=kubeconfig, deadline=deadline)
    if not listed.success:
        why = argocd.failure_kind(listed.stderr or listed.stdout)
        return resource, tracking, None, why
    try:
        items = argocd.parse_applications(listed.stdout)
    except ValueError:
        return resource, tracking, None, "error"
    item = argocd.find_application(items, tracking)
    if tracking.source == "label" and (item is None or not argocd.manages(item, resource)):
        return resource, None, None, ""            # Helm 의 instance 라벨일 뿐이다
    return resource, tracking, item, "" if item is not None else "absent"


async def decide(
    *, kind: str, name: str, namespace: str | None, context: str, kubeconfig: Path | None,
    cluster_id: str | None, change: Change | None = None, connected: set[str] | None = None,
    budget: float | None = None,
) -> Route:
    """그 리소스를 지금 어느 길로 바꿀지. 어떤 실패도 올리지 않는다 — 모르면 kubectl 길(지금과 같음)이다.

    budget 을 주면 kubectl 조회 전체를 그 초 안에 끝낸다(남은 시간으로 한 번의 제한을 줄인다). 넘으면 모름(known=False).
    change 가 새 리소스면 리소스 대신 그 요청이 고른 앱으로 판단한다.
    """
    deadline = time.monotonic() + budget if budget is not None else None
    if isinstance(change, NewResource):
        return await _decide_new(change, kind=kind, name=name, context=context, kubeconfig=kubeconfig,
                                 cluster_id=cluster_id, connected=connected, deadline=deadline)
    try:
        resource, tracking, item, why = await to_thread.run_sync(
            partial(_locate, kind, name, namespace, context=context, kubeconfig=kubeconfig, deadline=deadline),
            limiter=thread_limiter("gitops_route", LOOKUP_WORKERS),
        )
    except Exception:
        logger.exception("PR 길 판단의 조회를 건너뛴다")
        return Route("kubectl", "리소스를 읽지 못해 PR 길을 정할 수 없어요.", known=False)
    label = f"{kind} {namespace + '/' if namespace else ''}{name}"
    if resource is None:
        if why == "missing":
            return await _decide_missing(kind, name, namespace, label, context=context, kubeconfig=kubeconfig,
                                         cluster_id=cluster_id, change=change, connected=connected, deadline=deadline)
        return Route("kubectl", f"{label} 를 클러스터에서 읽지 못했어요.", known=False)
    if tracking is None:
        return Route("kubectl", f"{label} 는 ArgoCD 가 관리하지 않아요.")
    if item is None:
        if why == "forbidden":
            return Route("kubectl", "ArgoCD 앱을 읽을 권한이 없어 PR 길을 정할 수 없어요.", managed=True, known=False)
        if why == "error":
            return Route("kubectl", "ArgoCD 앱 목록을 읽지 못해 PR 길을 정할 수 없어요.", managed=True, known=False)
        return Route("kubectl", f"{label} 를 관리하는 ArgoCD 앱({tracking.app})을 이 클러스터에서 찾지 못했어요.",
                     managed=True)

    target = Target(api_version=str(resource.get("apiVersion") or ""), kind=str(resource.get("kind") or kind),
                    name=name, namespace=(resource.get("metadata") or {}).get("namespace") or namespace)
    return await _route_of_app(item, target, cluster_id=cluster_id, change=change, connected=connected)


async def _route_of_app(item: dict[str, Any], target: Target, *, cluster_id: str | None, change: Change | None,
                        connected: set[str] | None, project_destinations: list[dict] | None = None) -> Route:
    """이 앱의 레포·형식으로 PR 길인지. project_destinations 는 새 리소스 길에서만 읽어 넘긴다 (DURO-240)."""
    meta = item.get("metadata") or {}
    app_label = f"ArgoCD 앱 {meta.get('name')}"
    source, refused = _source_of(item)
    if source is None:
        return Route("kubectl", refused or "", managed=True, application=item, target=target)
    repository = repo_name(source.get("repoURL"))
    common = dict(managed=True, repository=repository, application=item, target=target,
                  source=app_source(item, source).model_copy(update={"project_destinations": project_destinations}),
                  revision=str(source.get("targetRevision") or ""))
    if repository is None:
        return Route("kubectl", f"{app_label} 의 소스가 GitHub 레포가 아니라 PR 로 못 바꿔요.", **common)

    # connected 를 주면 그걸 쓴다 — 한 호출에서 리소스 여럿을 볼 때 연결 목록을 한 번만 읽으려고 (kubectl 훅의 _pr_path)
    if connected is None and cluster_id is not None:
        try:
            connected = {repo.repository for repo in await server_api.manifest_repos(cluster_id)}
        except Exception:
            logger.exception("연결된 레포 목록을 읽지 못해 kubectl 길로 둔다: cluster=%s", cluster_id)
            return Route("kubectl", "레포 연결 상태를 확인하지 못해 PR 길을 정할 수 없어요.", known=False, **common)
    if repository not in (connected or set()):
        return Route("kubectl", f"{app_label} 의 레포 {repository} 가 Kukie 에 연결돼 있지 않아요. "
                                "레포를 연결하면 PR 로 바꿀 수 있어요.", **common)
    source_type = common["source"].source_type
    if source_type not in SUPPORTED_SOURCES:
        what = "Helm" if source_type == "Helm" else source_type or "알 수 없는 형식"
        return Route("kubectl", f"{app_label} 은 {what} 앱이라 아직 PR 로 못 바꿔요. "
                                "plain YAML 과 Kustomize 만 돼요.", **common)
    if isinstance(change, Other):
        return Route("kubectl", f"'{change.description}' 은 PR 로 못 바꿔요. PR 로는 레플리카 수 · 이미지 태그 · "
                                "CPU·메모리 · 환경변수를 바꾸고, 그 밖의 칸은 경로 지정 변경(what=patch)으로 "
                                "바꿔요.", **common)
    return Route("pr", f"{app_label} 이 관리하고 레포 {repository} 가 연결돼 있어요 — 변경은 PR 로 올려요.", **common)


def _list_applications(*, context: str, kubeconfig: Path | None,
                       deadline: float | None) -> tuple[list[dict[str, Any]] | None, str]:
    """(이 클러스터의 ArgoCD 앱들, 못 읽은 이유). 블로킹 — 스레드에서 돈다."""
    listed = _kubectl(argocd.LIST_COMMAND, context=context, kubeconfig=kubeconfig, deadline=deadline)
    if not listed.success:
        return None, argocd.failure_kind(listed.stderr or listed.stdout)
    try:
        return argocd.parse_applications(listed.stdout), ""
    except ValueError:
        return None, "error"


#: kubectl 짧은 이름 → kind. 요청한 kind 가 이 모양이어도 status.resources 의 kind 와 맞춰 본다 (DURO-248)
_SHORT_KINDS = {
    "po": "pod", "deploy": "deployment", "rs": "replicaset", "sts": "statefulset", "ds": "daemonset",
    "svc": "service", "cm": "configmap", "pvc": "persistentvolumeclaim", "sa": "serviceaccount",
    "ing": "ingress", "hpa": "horizontalpodautoscaler", "cj": "cronjob", "ns": "namespace",
}


def _same_kind(entry_kind: str, requested: str) -> bool:
    """status.resources 의 kind(`Deployment`)와 요청의 kind(`deployment`·`deployments`·`deploy`)가 같은가."""
    want = requested.lower()
    want = _SHORT_KINDS.get(want, want)
    have = entry_kind.lower()
    return want in (have, f"{have}s", f"{have}es") or (have.endswith("y") and want == f"{have[:-1]}ies")


def _missing_entry(entry: Any, kind: str, name: str, namespace: str | None) -> bool:
    """git 에는 있는데 클러스터에 없는 항목 — sync 가 실패했거나 아직 안 됐다.

    ArgoCD 는 이런 항목에 health `Missing` 을 달기도 하고, sync 가 거부되면(프로젝트가 막은 네임스페이스 등) health 없이
    status `OutOfSync` 만 남긴다 (kukie-gitops-test sandbox-plain, 2026-11-13). 클러스터에 없다는 건 kubectl 이 이미 확인했다.
    """
    if not isinstance(entry, dict) or entry.get("name") != name or not _same_kind(str(entry.get("kind") or ""), kind):
        return False
    if namespace and (entry.get("namespace") or "") not in ("", namespace):
        return False
    if entry.get("requiresPruning"):
        return False                               # 반대로 클러스터에만 있고 git 에는 없는 항목이다 (agent#155 리뷰)
    health = entry.get("health") if isinstance(entry.get("health"), dict) else {}
    return health.get("status") == "Missing" or entry.get("status") == "OutOfSync"


#: 이 클러스터를 가리키는 Application destination — ArgoCD 가 자기 클러스터에 배포할 때 쓰는 값
IN_CLUSTER_SERVER = "https://kubernetes.default.svc"
IN_CLUSTER_NAME = "in-cluster"


def _in_cluster(item: dict[str, Any]) -> bool:
    """앱이 지금 보는 클러스터에 배포하나. 다른 클러스터로 배포하는 앱의 같은 이름 리소스를 고르면 엉뚱한 환경의 레포를
    고친다 (agent#155 리뷰). 클러스터 객체의 추적 표시가 없는 길이라 destination 으로만 잇는다 — 모르면 후보가 아니다."""
    destination = (item.get("spec") or {}).get("destination") or {}
    return destination.get("server") == IN_CLUSTER_SERVER or destination.get("name") == IN_CLUSTER_NAME


def _api_version(entry: dict[str, Any]) -> str:
    group, version = str(entry.get("group") or ""), str(entry.get("version") or "")
    return f"{group}/{version}" if group else version


async def _decide_missing(kind: str, name: str, namespace: str | None, label: str, *, context: str,
                          kubeconfig: Path | None, cluster_id: str | None, change: Change | None,
                          connected: set[str] | None, deadline: float | None) -> Route:
    """클러스터에 없는 리소스 — git 에만 있는(sync 가 실패한) ArgoCD 리소스면 그 앱으로 판단한다 (DURO-248).

    추적 표시는 클러스터 객체에 붙으므로 sync 가 한 번도 안 된 리소스에는 없다. 대신 Application 의 status.resources 가
    git 에서 렌더한 리소스를 모두 갖고 있어 거기서 찾는다. 고칠 곳 찾기(manifests.change.prepare)는 원래 클러스터가 아니라
    렌더 결과에서 대상을 찾으므로 그대로 쓴다.
    """
    absent = Route("kubectl", f"{label} 가 클러스터에 없어요.")
    try:
        items, why = await to_thread.run_sync(
            partial(_list_applications, context=context, kubeconfig=kubeconfig, deadline=deadline),
            limiter=thread_limiter("gitops_route", LOOKUP_WORKERS),
        )
    except Exception:
        logger.exception("클러스터에 없는 리소스의 ArgoCD 앱 조회를 건너뛴다")
        items, why = None, "error"
    if items is None:
        if why == "absent":
            return absent                          # ArgoCD 가 없다 — git 에만 있을 수가 없다
        # 정하지 못했다 — 승인 순간 다시 판단할 때 STALE 이 아니라 다시 시도하게 (Route.known)
        return Route("kubectl", f"{label} 가 클러스터에 없어요. ArgoCD 앱 목록을 읽지 못해 git 에만 있는 리소스인지 "
                                "확인하지 못했어요.", known=False)
    found = [(item, entry) for item in items if _in_cluster(item)
             for entry in (item.get("status") or {}).get("resources") or []
             if _missing_entry(entry, kind, name, namespace)]
    if not found:
        return Route("kubectl", f"{label} 가 클러스터에 없고, 이 리소스를 가진 ArgoCD 앱도 없어요.")
    if len(found) > 1:
        where = ", ".join(sorted({
            f"{(item.get('metadata') or {}).get('name')}({entry.get('namespace') or '-'})" for item, entry in found
        }))
        return Route("kubectl", f"{label} 가 클러스터에 없고, 여러 ArgoCD 앱·네임스페이스에 같은 리소스가 있어요({where}) — "
                                "어느 것인지 정할 수 없어요. namespace 를 알려 주세요.", managed=True)
    item, entry = found[0]
    target = Target(api_version=_api_version(entry), kind=str(entry.get("kind") or kind), name=name,
                    namespace=str(entry.get("namespace") or "") or namespace)
    app = (item.get("metadata") or {}).get("name")
    if not namespace and target.namespace:
        # 처음 조회는 namespace 없이(context 기본 ns) 했다 — 앱이 적은 ns 에 실제로 있으면 git 에만 있는 게 아니다
        # (drift 로 OutOfSync 인 리소스). 그 ns 로 다시 본다 (agent#155 리뷰)
        try:
            live = await to_thread.run_sync(
                partial(_read, ["get", target.kind, name, "-o", "json", "-n", target.namespace],
                        context=context, kubeconfig=kubeconfig, deadline=deadline),
                limiter=thread_limiter("gitops_route", LOOKUP_WORKERS),
            )
        except Exception:
            logger.exception("다른 namespace 의 리소스를 다시 보지 못했다")
            live = None
        if live is None:
            return Route("kubectl", f"{target.label()} 를 클러스터에서 읽지 못했어요.", known=False)
        if live:
            return Route("kubectl", f"{label} 는 {target.namespace} 네임스페이스에 있어요 — namespace 를 알려 주세요.",
                         managed=True)
    denied = await _project_denies(item, target, context=context, kubeconfig=kubeconfig, deadline=deadline)
    if denied:
        # sync 가 거부된 까닭이 바로 이것이면, 값을 고치는 PR 을 머지해도 다시 거부돼 반영되지 않는다 (agent#155 리뷰)
        return Route("kubectl", f"{target.label()} 는 클러스터에 없고 git 에만 있어요. ArgoCD 앱 {app} 의 프로젝트 "
                                f"'{denied}' 가 {target.namespace} 네임스페이스를 허락하지 않아 sync 가 거부돼요 — 값을 고치는 PR 을 "
                                "머지해도 반영되지 않아요. git 의 네임스페이스를 프로젝트가 허락하는 곳으로 고치거나 프로젝트 설정을 "
                                "바꿔야 해요(Kukie PR 로는 아직 네임스페이스를 못 바꿔요).",
                     managed=True, application=item, target=target)
    route = await _route_of_app(item, target, cluster_id=cluster_id, change=change, connected=connected)
    # 사람과 AI 가 "왜 클러스터에 없는데 PR 인가" 를 알게 — sync 실패의 까닭은 앱 상태(conditions)에 있다
    return replace(route, reason=f"{target.label()} 는 클러스터에 없고 git 에만 있어요 — ArgoCD 앱 {app} 의 "
                                 f"sync 가 실패했거나 아직이에요. {route.reason}")


async def _project_denies(item: dict[str, Any], target: Target, *, context: str, kubeconfig: Path | None,
                          deadline: float | None) -> str | None:
    """앱의 ArgoCD 프로젝트가 target 의 네임스페이스를 막으면 그 프로젝트 이름. 허락하거나 모르면 None —
    새 리소스 길(DURO-240)처럼 프로젝트를 못 읽으면 막지 않는다."""
    if not target.namespace:
        return None
    try:
        destinations = await to_thread.run_sync(
            partial(_project_destinations, item, context=context, kubeconfig=kubeconfig, deadline=deadline),
            limiter=thread_limiter("gitops_route", LOOKUP_WORKERS),
        )
    except Exception:
        logger.exception("ArgoCD 프로젝트를 읽지 못해 네임스페이스 허락을 건너뛴다")
        return None
    if destinations is None:
        return None
    destination = (item.get("spec") or {}).get("destination") or {}
    permitted = argocd.destination_permitted(destinations, server=str(destination.get("server") or ""),
                                             name=str(destination.get("name") or ""), namespace=target.namespace)
    return str((item.get("spec") or {}).get("project") or "default") if permitted is False else None


async def _decide_new(change: NewResource, *, kind: str, name: str, context: str, kubeconfig: Path | None,
                      cluster_id: str | None, connected: set[str] | None, deadline: float | None) -> Route:
    """새 리소스를 더할 앱으로 길을 정한다. 어떤 실패도 올리지 않는다 — decide 와 같다."""
    try:
        items, why = await to_thread.run_sync(
            partial(_list_applications, context=context, kubeconfig=kubeconfig, deadline=deadline),
            limiter=thread_limiter("gitops_route", LOOKUP_WORKERS),
        )
    except Exception:
        logger.exception("새 리소스의 ArgoCD 앱 조회를 건너뛴다")
        return Route("kubectl", "ArgoCD 앱 목록을 읽지 못해 PR 길을 정할 수 없어요.", known=False)
    if items is None:
        if why == "absent":
            return Route("kubectl", "이 클러스터에 ArgoCD 가 없어요.")
        if why == "forbidden":
            return Route("kubectl", "ArgoCD 앱을 읽을 권한이 없어 PR 길을 정할 수 없어요.", known=False)
        return Route("kubectl", "ArgoCD 앱 목록을 읽지 못해 PR 길을 정할 수 없어요.", known=False)

    def meta(item: dict[str, Any]) -> dict[str, Any]:
        return item.get("metadata") or {}

    same_name = [item for item in items if meta(item).get("name") == change.application]
    found = [item for item in same_name
             if not change.application_namespace or meta(item).get("namespace") == change.application_namespace]
    if not found:
        names = ", ".join(str(meta(item).get("name")) for item in items[:SHOW_APPS]) or "없음"
        return Route("kubectl", f"ArgoCD 앱 '{change.application}' 를 이 클러스터에서 찾지 못했어요. 있는 앱: {names}.")
    if len(found) > 1:
        places = ", ".join(str(meta(item).get("namespace")) for item in found)
        return Route("kubectl", f"ArgoCD 앱 '{change.application}' 이 여러 네임스페이스({places})에 있어요 — "
                                "application_namespace 를 알려 주세요.")
    item = found[0]
    destination = ((item.get("spec") or {}).get("destination") or {}).get("namespace") or None
    target = Target(api_version="", kind=kind, name=name, namespace=destination)
    try:
        destinations = await to_thread.run_sync(
            partial(_project_destinations, item, context=context, kubeconfig=kubeconfig, deadline=deadline),
            limiter=thread_limiter("gitops_route", LOOKUP_WORKERS),
        )
    except Exception:
        logger.exception("ArgoCD 프로젝트 조회를 건너뛴다")
        destinations = None
    return await _route_of_app(item, target, cluster_id=cluster_id, change=change, connected=connected,
                               project_destinations=destinations)


def _project_destinations(item: dict[str, Any], *, context: str, kubeconfig: Path | None,
                          deadline: float | None) -> list[dict] | None:
    """앱이 속한 AppProject 의 spec.destinations. 못 읽으면(없음·권한·시간) None — 새 리소스를 막지 않고 경고만 한다 (DURO-240).

    AppProject 는 ArgoCD 제어 namespace 에 있다. 보통 Application 과 같은 namespace(argocd)다 — "apps in any namespace" 로
    다른 namespace 에 둔 앱이면 거기엔 없어 None 이 된다(모름).
    """
    meta = item.get("metadata") or {}
    project = str((item.get("spec") or {}).get("project") or "default")
    namespace = str(meta.get("namespace") or "argocd")
    if not argocd.valid_ref(namespace, project):
        return None
    data = _read(argocd.project_command(namespace, project), context=context, kubeconfig=kubeconfig, deadline=deadline)
    destinations = ((data or {}).get("spec") or {}).get("destinations")
    if not isinstance(destinations, list):
        return None
    return [entry for entry in destinations if isinstance(entry, dict)]


def live_resources(resources: list[dict[str, str]], *, context: str, kubeconfig: Path | None) -> list[dict[str, str]]:
    """새로 만들려는 것 중 클러스터에 이미 있는 것. 못 읽으면 없는 셈 치고 경고만 빠진다 — 블로킹이라 스레드에서 돈다."""
    deadline = time.monotonic() + LIVE_BUDGET
    found = []
    for resource in resources[:LIVE_CHECKS]:
        namespace = resource["namespace"]
        if not (KIND_SHAPE.match(resource["kind"]) and NAME_SHAPE.match(resource["name"])
                and (not namespace or NAME_SHAPE.match(namespace))):
            continue
        args = ["get", resource["kind"], resource["name"], "-o", "json", *(["-n", namespace] if namespace else [])]
        if _read(args, context=context, kubeconfig=kubeconfig, deadline=deadline):
            found.append(resource)
    return found


def find_hpa(kind: str, name: str, namespace: str | None, *, context: str, kubeconfig: Path | None) -> str | None:
    """그 리소스를 맡은 HPA 이름 — git 에 없어도 레플리카를 정한다. 못 읽으면 None (manifests 가 git 안의 HPA 는 따로 본다)."""
    data = _read(["get", "horizontalpodautoscalers", "-o", "json", *(["-n", namespace] if namespace else [])],
                 context=context, kubeconfig=kubeconfig)
    for item in (data or {}).get("items") or []:
        ref = (item.get("spec") or {}).get("scaleTargetRef") or {}
        if ref.get("kind") == kind and ref.get("name") == name:
            return str((item.get("metadata") or {}).get("name") or "")
    return None
