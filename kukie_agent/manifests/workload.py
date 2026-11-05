"""워크로드 객체에서 파드 스펙·컨테이너·이미지를 읽는 작은 도구들. 렌더 결과(dict)와 ruamel 문서에 같이 쓴다."""
from __future__ import annotations

from typing import Any

from kukie_agent.manifests.compare import FieldPath

POD_SPEC: dict[str, FieldPath] = {
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}
SCALABLE = frozenset({"Deployment", "StatefulSet", "ReplicaSet"})


def dig(obj: Any, path: FieldPath) -> Any:
    for part in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def containers(obj: Any, kind: str) -> list[dict]:
    found = dig(obj, (*POD_SPEC[kind], "containers"))
    return [c for c in found if isinstance(c, dict)] if isinstance(found, list) else []


def container(obj: Any, kind: str, name: str) -> dict | None:
    return next((c for c in containers(obj, kind) if c.get("name") == name), None)


def split_image(image: str) -> tuple[str, str | None, str | None]:
    """`repo[:tag][@digest]` → (repo, tag, digest). 레지스트리 포트(`host:5000/x`)의 콜론은 태그가 아니다."""
    name, _, digest = image.partition("@")
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1:], digest or None
    return name, None, digest or None


def with_tag(image: str, tag: str) -> str:
    """태그를 바꾸고 digest 는 뗀다 — digest 가 남으면 태그보다 먼저라 새 태그가 쓰이지 않는다."""
    repo, _, _ = split_image(image)
    return f"{repo}:{tag}"
