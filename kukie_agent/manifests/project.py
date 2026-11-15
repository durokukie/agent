"""앱의 ArgoCD 프로젝트가 렌더 결과의 리소스 **종류**를 허락하나 (DURO-254).

네임스페이스 허락(destinations, DURO-240)과 짝이다. 프로젝트가 막은 종류가 git 에 들어가면 머지해도 ArgoCD sync 가
"resource … is not permitted in project" 로 거부되고, 그 앱의 다른 변경까지 반영되지 않는다. 그래서 PR 을 열기 전에 막는다.

규칙은 argocd.kind_permitted(ArgoCD IsGroupKindPermitted 와 같다). 볼 것은 렌더 결과에 생기거나 바뀌는 객체다 —
새 리소스는 만들어지는 것 전부, 그 밖의 변경은 고친 뒤 내용이 달라지거나 새로 생긴 키(네임스페이스 옮기기의 새 키)다.
빠지기만 하는 객체(삭제)는 sync 할 것이 없어 보지 않는다.

범위(네임스페이스 · 클러스터)는 아는 종류만 정한다. 모르는 커스텀 리소스는 두 범위 모두에서 막혀야 막는다 —
범위를 잘못 짐작해 허락된 것을 막지 않게.
"""
from __future__ import annotations

from collections.abc import Iterable

from kukie_agent import argocd
from kukie_agent.manifests import compare
from kukie_agent.manifests.compare import Key
from kukie_agent.manifests.models import AppSource, Refused
from kukie_agent.manifests.risks import CLUSTER_SCOPED

_SHOW_LIMIT = 5
#: 네임스페이스가 있다고 아는 기본 종류 — 렌더 결과에 namespace 가 안 적힌 문서(앱 destination 으로 들어간다)의 범위를 정한다
NAMESPACED = frozenset({
    "Pod", "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob", "Service", "Endpoints",
    "EndpointSlice", "ConfigMap", "Secret", "ServiceAccount", "PersistentVolumeClaim", "Ingress", "NetworkPolicy",
    "Role", "RoleBinding", "HorizontalPodAutoscaler", "PodDisruptionBudget", "LimitRange", "ResourceQuota",
    "Event", "Lease", "PodTemplate", "ReplicationController", "ControllerRevision",
})


def namespaced(kind: str, namespace: str) -> bool | None:
    """True 면 네임스페이스 범위, False 면 클러스터 범위, None 이면 모른다."""
    if kind in CLUSTER_SCOPED:
        return False
    if namespace or kind in NAMESPACED:
        return True
    return None


def group_of(api_version: str) -> str:
    return api_version.partition("/")[0] if "/" in api_version else ""


def kind_denied(rules: dict[str, list[dict]], *, api_version: str, kind: str, namespace: str, name: str) -> bool:
    """이 객체를 프로젝트가 막나. 범위를 모르면 두 범위 모두에서 막혀야 막는다."""
    group = group_of(api_version)
    scope = namespaced(kind, namespace)
    scopes = (True, False) if scope is None else (scope,)
    return not any(argocd.kind_permitted(rules, group=group, kind=kind, namespaced=each, name=name) for each in scopes)


def changed(before: dict[Key, dict], after: dict[Key, dict]) -> list[Key]:
    """렌더 결과에 생기거나 내용이 바뀌는 객체 — sync 가 클러스터에 적용할 것들."""
    return [key for key, obj in after.items() if before.get(key) != obj]


def denied(app: AppSource, keys: Iterable[Key]) -> list[Key]:
    """프로젝트가 막는 객체. 프로젝트 규칙을 못 읽었으면 없음 — 막지 않는다."""
    if app.project_kinds is None:
        return []
    return [key for key in keys
            if kind_denied(app.project_kinds, api_version=key[0], kind=key[1], namespace=key[2], name=key[3])]


def refusal(app: AppSource, keys: list[Key], *, retry: bool, fix: str = "") -> Refused:
    """막을 때의 안내 — 네임스페이스 거부(project_denied)와 같은 모양이다."""
    assert app.project_kinds is not None
    kinds = ", ".join(dict.fromkeys(key[1] for key in keys))
    labels = ", ".join(compare.label(key) for key in keys[:_SHOW_LIMIT])
    rules = app.project_kinds
    return Refused(code="project_denied", retry=retry, message=(
        f"이 앱의 ArgoCD 프로젝트 '{app.project}' 가 {kinds} 를 허락하지 않아 sync 가 거부돼요({labels}). "
        "머지해도 반영되지 않고, 이 앱의 다른 변경까지 반영되지 않아요. "
        f"프로젝트가 허락하는 종류는 네임스페이스 범위 {argocd.allowed_kinds(rules, namespaced=True)}, "
        f"클러스터 범위 {argocd.allowed_kinds(rules, namespaced=False)} 예요. "
        f"{fix}그 종류가 꼭 필요하면 ArgoCD 프로젝트 설정을 먼저 바꿔 주세요."))
