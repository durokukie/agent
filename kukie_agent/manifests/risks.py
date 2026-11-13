"""리소스 하나의 렌더 결과에서 봐 둘 위험을 찾는다 — 새 리소스(DURO-154)와 경로 지정 변경(DURO-249)이 같이 쓴다.

막지 않고 승인 카드에 경고로 올리는 목록이다: 클러스터 범위 · 다른 네임스페이스 · privileged · 권한 상승 · capabilities ·
root · hostPath · hostNetwork·PID·IPC · 서비스 계정 · 토큰 · latest·태그 없는 이미지 · 비밀처럼 보이는 평문 환경변수.
막지 않는 이유: 전체 권한 클러스터에서는 같은 변경을 kubectl 로도 할 수 있어, PR 길만 막으면 기록이 남지 않는 길로 빠진다.
평문 비밀 값은 예외다 — git 에 남으면 지울 수 없어 change.py 가 이 목록의 plaintext_env 로 막는다.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from kukie_agent.manifests import compare
from kukie_agent.manifests.compare import FieldPath, Key
from kukie_agent.manifests.models import RiskWarning, WarningLocation
from kukie_agent.manifests.workload import split_image

# 값이 비밀번호·키처럼 보이는 환경변수 이름 — git 에 평문으로 남기지 않는다
SECRETISH = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL", re.I)
#: 네임스페이스가 없다고 아는 종류. 이름으로 짐작하지 않는다 — ClusterServiceVersion 처럼 네임스페이스가 있는 종류도 있고,
#: 틀리면 namespace 칸이 비어 같은 이름을 못 찾는다(agent#132 리뷰). 모르는 커스텀 리소스는 경고하지 않고, 막는 데도 쓰지 않는다
CLUSTER_SCOPED = frozenset({
    "ClusterRole", "ClusterRoleBinding", "ClusterIssuer", "ClusterSecretStore", "ClusterExternalSecret",
    "CustomResourceDefinition", "Namespace", "Node", "PersistentVolume", "StorageClass", "PriorityClass",
    "IngressClass", "RuntimeClass", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration",
    "APIService", "CSIDriver", "VolumeSnapshotClass",
})
CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")
VALUE_LIMIT = 200


def risk(code: str, file: str, resource: str, field: str | None, value: str | None, risk: str,
          suggestion: str) -> RiskWarning:
    if value is not None and len(value) > VALUE_LIMIT:
        value = value[:VALUE_LIMIT] + "…"
    where = " · ".join(part for part in (resource, field, value) if part)
    return RiskWarning(code=code, location=WarningLocation(file=file, resource=resource, field=field, value=value),
                       risk=risk, suggestion=suggestion, text=f"{where}: {risk} {suggestion}")


def walk(node: Any, path: FieldPath = ()) -> Iterator[tuple[FieldPath, Any]]:
    """객체의 모든 칸 — 이름 있는 목록 항목은 `[이름]` 조각으로. 파드 스펙 위치를 종류로 고르지 않으니 커스텀 리소스도 같이 본다."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield (*path, str(key)), value
            yield from walk(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            named = isinstance(value, dict) and isinstance(value.get("name"), str)
            yield from walk(value, (*path, compare.item(value["name"]) if named else index))


def resource_risks(obj: dict, key: Key, file: str, destination: str | None, *, project: str | None = None) -> list[RiskWarning]:
    """project 는 이 네임스페이스를 허락한다고 확인한 ArgoCD 프로젝트 이름이다. None 이면 프로젝트 규칙을 확인하지 못했다."""
    resource = compare.label(key)
    kind, namespace = key[1], key[2]
    found: list[RiskWarning] = []
    # 경고는 틀려도 막지 않으니 이름 짐작도 쓴다 (ClusterPolicy·ClusterTask …). namespace 칸은 아는 종류만 비운다 (plan_new_resource)
    if kind in CLUSTER_SCOPED or kind.startswith("Cluster"):
        found.append(risk("cluster_scoped", file, resource, "kind", kind,
                           "클러스터 전체에 영향을 줘요. 이 앱 밖의 리소스와 권한까지 바뀔 수 있어요.",
                           "꼭 클러스터 전체에 필요한지 확인하세요. 한 네임스페이스면 충분하면 Role·RoleBinding 같은 네임스페이스 범위로 바꾸세요."))
    if namespace and destination and namespace != destination:
        project_note = (f"ArgoCD 프로젝트 '{project}' 는 이 네임스페이스를 허락해요." if project is not None
                        else "ArgoCD 프로젝트 규칙은 확인하지 못했어요 — 프로젝트가 막으면 sync 가 실패해요.")
        found.append(risk("other_namespace", file, resource, "metadata.namespace", namespace,
                           f"이 앱의 네임스페이스({destination})가 아닌 곳에 만들어져요. 다른 팀의 공간일 수 있어요. {project_note}",
                           f"metadata.namespace 를 지우거나 {destination} 로 바꾸세요. 그 네임스페이스가 맞다면 그곳을 보는 앱을 고르는 것도 방법이에요."))
    for path, value in walk(obj):
        field = compare.show(path)
        leaf = path[-1]
        parent = path[-2] if len(path) > 1 else None
        holder = path[-3] if len(path) > 2 else None
        if leaf == "privileged" and value is True and parent == "securityContext":
            found.append(risk("privileged", file, resource, field, "true",
                               "이 컨테이너가 노드의 거의 모든 권한을 가져요. 뚫리면 노드 전체와 그 위의 다른 파드까지 위험해요.",
                               "privileged 를 빼고, 꼭 필요한 권한만 securityContext.capabilities.add 로 주세요."))
        elif leaf == "allowPrivilegeEscalation" and value is True and parent == "securityContext":
            found.append(risk("privilege_escalation", file, resource, field, "true",
                               "컨테이너 안의 프로세스가 처음보다 큰 권한(setuid 등)을 얻을 수 있어요.",
                               "allowPrivilegeEscalation 을 false 로 두세요."))
        elif leaf == "add" and parent == "capabilities" and isinstance(value, list) and value:
            found.append(risk("capabilities", file, resource, field, ", ".join(map(str, value)),
                               "컨테이너에 리눅스 권한(capability)을 더 줘요. SYS_ADMIN · NET_ADMIN 같은 것은 노드까지 닿을 수 있어요.",
                               "정말 필요한 것만 남기고, 가능하면 drop: [ALL] 과 함께 쓰세요."))
        elif leaf == "runAsUser" and value == 0 and parent == "securityContext":
            found.append(risk("run_as_root", file, resource, field, "0",
                               "컨테이너가 root(UID 0)로 돌아요. 컨테이너가 뚫리면 노드 쪽 피해가 커져요.",
                               "root 가 아닌 UID 로 돌리고 runAsNonRoot: true 를 함께 두세요."))
        elif leaf in ("hostNetwork", "hostPID", "hostIPC") and value is True:
            what = {"hostNetwork": "네트워크를", "hostPID": "프로세스 목록을", "hostIPC": "프로세스 간 통신(IPC)을"}[leaf]
            found.append(risk({"hostNetwork": "host_network", "hostPID": "host_pid", "hostIPC": "host_ipc"}[leaf],
                               file, resource, field, "true",
                               f"파드가 노드의 {what} 그대로 써요. 노드와 다른 파드와의 격리를 받지 못해요.",
                               "hostNetwork 를 빼고 Service 로 열어 주세요." if leaf == "hostNetwork"
                               else f"{leaf} 를 빼 주세요."))
        elif leaf == "serviceAccountName" and isinstance(value, str) and value and value != "default":
            found.append(risk("service_account", file, resource, field, value,
                               "파드가 이 서비스 계정의 권한으로 쿠버네티스 API 를 불러요.",
                               "그 서비스 계정에 붙은 Role·ClusterRole 이 꼭 필요한 만큼인지 확인하세요."))
        elif leaf == "automountServiceAccountToken" and value is True:
            found.append(risk("service_account_token", file, resource, field, "true",
                               "파드 안에 쿠버네티스 API 토큰이 들어가요. 컨테이너가 뚫리면 그 토큰의 권한을 쓸 수 있어요.",
                               "API 를 부르지 않는 앱이면 false 로 두세요."))
        elif leaf == "hostPath" and isinstance(value, dict) and holder == "volumes":
            found.append(risk("host_path", file, resource, field, str(value.get("path") or ""),
                               "파드가 노드의 파일을 직접 읽고 써요. 노드의 설정이나 다른 파드의 데이터가 노출될 수 있어요.",
                               "PersistentVolumeClaim·emptyDir·ConfigMap 으로 바꿀 수 있는지 보세요."))
        elif leaf == "image" and isinstance(value, str) and holder in CONTAINER_LISTS:
            _, tag, digest = split_image(value)
            if not digest and tag in (None, "latest"):
                found.append(risk("latest_image" if tag else "untagged_image", file, resource, field, value,
                                   "어느 버전이 뜰지 정해지지 않아요. 파드가 다시 뜰 때 다른 버전이 올라올 수 있고, 되돌리기도 어려워요.",
                                   "1.29.0 처럼 버전을 고정한 태그나 digest 를 쓰세요."))
        elif _plaintext(path, value):
            # 값은 싣지 않는다 — 비밀 값일 수 있다
            found.append(risk("plaintext_env", file, resource, field, None,
                               "비밀번호나 키처럼 보이는 환경변수 값이 git 에 평문으로 남아요.",
                               "값이 비밀이면 Secret 을 따로 두고 valueFrom.secretKeyRef 로 가리키세요."))
    return found


def _plaintext(path: FieldPath, value: Any) -> bool:
    """비밀처럼 보이는 이름의 환경변수에 평문 값 — 어느 컨테이너 목록(init · ephemeral 포함)·어느 종류든."""
    return (len(path) > 2 and path[-1] == "value" and path[-3] == "env" and isinstance(path[-2], str)
            and bool(SECRETISH.search(path[-2])) and bool(value))


def plaintext_env(obj: Any) -> dict[FieldPath, Any]:
    """평문 비밀처럼 보이는 환경변수 값의 자리 → 값."""
    return {path: value for path, value in walk(obj) if _plaintext(path, value)}


def secret_refs(obj: Any) -> list[FieldPath]:
    """Secret 에서 값을 가져오는(valueFrom.secretKeyRef) 환경변수 항목의 자리."""
    return [path[:-1] for path, value in walk(obj) if _secret_source(path, value)]


def _secret_source(path: FieldPath, value: Any) -> bool:
    return (len(path) > 2 and path[-1] == "valueFrom" and path[-3] == "env" and isinstance(value, dict)
            and "secretKeyRef" in value)
