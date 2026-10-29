"""PR 길 판단 — 리소스마다 PR 로 바꿀지 kubectl 로 바꿀지 정한다 (설계 "[GitOps] PR 길 판단 · kubectl 경고" 1절).

| 결과                                            | 길              |
| ArgoCD 관리 + 레포 연결됨 + 지원 형식·요청       | PR              |
| ArgoCD 관리 + 레포 미연결, 또는 지원 안 하는 것   | kubectl + 경고  |
| ArgoCD 관리 아님                                 | kubectl         |

클러스터 설정은 없다 — 레포 연결이 끊기거나 새로 생기면 다음 판단부터 달라질 뿐이다. 그래서 승인 순간에도 다시 부른다
(gitops_hook — 길이 바뀌었으면 STALE). kubectl 조회와 kukie-server 의 연결 목록으로만 판단한다. ArgoCD API 토큰은 쓰지 않는다.

지원 형식은 plain YAML(Directory)·Kustomize, 요청은 manifests 의 넷(레플리카·이미지 태그·resources·환경변수)이다.
targetRevision 이 브랜치인지(태그·커밋이면 PR 길이 없다)는 GitHub 에 물어야 알아서 여기서 보지 않는다 — PR 을 만들 때 본다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal

from anyio import to_thread

from kukie_agent import argocd, server_api
from kukie_agent.gitops.repo import repo_name
from kukie_agent.kubectl import KubectlResult, run_kubectl, thread_limiter
from kukie_agent.manifests import AppSource, Change, Other, Target

logger = logging.getLogger(__name__)

#: PR 로 고칠 수 있는 ArgoCD 형식 (status.sourceType). Helm 은 DURO-137
SUPPORTED_SOURCES = ("Directory", "Kustomize")
LOOKUP_TIMEOUT = 10
#: 동시에 도는 판단 조회 상한 — argocd 판별(ARGOCD_LOOKUP_WORKERS)과 같은 이유로 따로 묶는다
LOOKUP_WORKERS = 8

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

    def fingerprint(self) -> dict[str, Any]:
        """승인 순간에 다시 판단한 것과 맞춰 볼 칸 — 하나라도 다르면 승인한 전제가 깨졌다 (STALE)."""
        return {
            "path": self.path,
            "repository": self.repository,
            "application": self.app_key,
            "source_path": self.source.path if self.source else None,
            "source_type": self.source.source_type if self.source else None,
            "revision": self.revision,
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
    return AppSource(
        source_type=source_type,
        path=str(source.get("path") or ""),
        directory_recurse=bool((source.get("directory") or {}).get("recurse")),
        ignore_differences=[entry for entry in spec.get("ignoreDifferences") or [] if isinstance(entry, dict)],
        respect_ignore_differences=any(
            isinstance(option, str) and option.replace(" ", "") == "RespectIgnoreDifferences=true" for option in options
        ),
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
    """
    deadline = time.monotonic() + budget if budget is not None else None
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
            return Route("kubectl", f"{label} 가 클러스터에 없어요.")
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

    meta = item.get("metadata") or {}
    app_label = f"ArgoCD 앱 {meta.get('name')}"
    target = Target(api_version=str(resource.get("apiVersion") or ""), kind=str(resource.get("kind") or kind),
                    name=name, namespace=(resource.get("metadata") or {}).get("namespace") or namespace)
    source, refused = _source_of(item)
    if source is None:
        return Route("kubectl", refused or "", managed=True, application=item, target=target)
    repository = repo_name(source.get("repoURL"))
    common = dict(managed=True, repository=repository, application=item, target=target,
                  source=app_source(item, source), revision=str(source.get("targetRevision") or ""))
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
                                "CPU·메모리 · 환경변수만 바꿔요.", **common)
    return Route("pr", f"{app_label} 이 관리하고 레포 {repository} 가 연결돼 있어요 — 변경은 PR 로 올려요.", **common)


def find_hpa(kind: str, name: str, namespace: str | None, *, context: str, kubeconfig: Path | None) -> str | None:
    """그 리소스를 맡은 HPA 이름 — git 에 없어도 레플리카를 정한다. 못 읽으면 None (manifests 가 git 안의 HPA 는 따로 본다)."""
    data = _read(["get", "horizontalpodautoscalers", "-o", "json", *(["-n", namespace] if namespace else [])],
                 context=context, kubeconfig=kubeconfig)
    for item in (data or {}).get("items") or []:
        ref = (item.get("spec") or {}).get("scaleTargetRef") or {}
        if ref.get("kind") == kind and ref.get("name") == name:
            return str((item.get("metadata") or {}).get("name") or "")
    return None
