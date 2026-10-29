"""렌더 결과 비교 — 바뀐 것이 요청한 리소스의 요청한 칸뿐인지 (DURO-133).

객체는 (apiVersion, kind, namespace, name) 으로 맞추고, 칸은 경로(튜플)로 센다.
이름(name)이 있는 항목의 목록(containers · env …)은 순서가 아니라 이름으로 맞춘다 — 경로 조각이 `[api]` 처럼 된다.
"""
from __future__ import annotations

import difflib
from typing import Any

import yaml

from kukie_agent.manifests.models import Target
from kukie_agent.manifests.render import RenderError

Key = tuple[str, str, str, str]
FieldPath = tuple[str | int, ...]


def key_of(obj: dict) -> Key:
    meta = obj.get("metadata") or {}
    return (str(obj.get("apiVersion")), str(obj.get("kind")),
            str(meta.get("namespace") or ""), str(meta.get("name") or ""))


def index(objects: list[dict]) -> dict[Key, dict]:
    """같은 리소스가 두 번 나오면 렌더링 실패로 본다 — 덮어쓰면 하나가 안 보여 비교를 빠져나가고, ArgoCD 도 sync 하지 못한다."""
    found: dict[Key, dict] = {}
    for obj in objects:
        key = key_of(obj)
        if key in found:
            raise RenderError(f"같은 리소스가 두 번 나와요: {label(key)}")
        found[key] = obj
    return found


def matches(key: Key, target: Target) -> bool:
    """namespace 가 없는 문서는 Application 의 destination namespace 로 들어가므로 이름만으로 맞춘다."""
    api_version, kind, namespace, name = key
    if (api_version, kind, name) != (target.api_version, target.kind, target.name):
        return False
    return not target.namespace or not namespace or namespace == target.namespace


def find(objects: dict[Key, dict], target: Target) -> list[Key]:
    return [key for key in objects if matches(key, target)]


def label(key: Key) -> str:
    _, kind, namespace, name = key
    return f"{kind} {namespace + '/' if namespace else ''}{name}"


def item(name: str) -> str:
    return f"[{name}]"


def _named(items: list) -> dict[str, Any] | None:
    if not all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in items):
        return None
    names = [i["name"] for i in items]
    return dict(zip(names, items)) if len(set(names)) == len(names) else None


def diff_paths(before: Any, after: Any, path: FieldPath = ()) -> list[FieldPath]:
    """두 값에서 다른 곳의 경로 — 가장 깊은 곳까지 내려가 적는다."""
    if isinstance(before, dict) and isinstance(after, dict):
        found: list[FieldPath] = []
        for key in sorted(set(before) | set(after), key=str):
            if key not in before or key not in after:
                found.append((*path, key))
            else:
                found += diff_paths(before[key], after[key], (*path, key))
        return found
    if isinstance(before, list) and isinstance(after, list):
        named_before, named_after = _named(before), _named(after)
        if named_before is not None and named_after is not None:
            found = []
            for name in sorted(set(named_before) | set(named_after)):
                if name not in named_before or name not in named_after:
                    found.append((*path, item(name)))
                else:
                    found += diff_paths(named_before[name], named_after[name], (*path, item(name)))
            return found
        if len(before) != len(after):
            return [path]
        found = []
        for i, (b, a) in enumerate(zip(before, after)):
            found += diff_paths(b, a, (*path, i))
        return found
    return [] if before == after else [path]


MISSING = object()


def value_at(obj: Any, path: FieldPath) -> Any:
    """diff_paths 가 돌려준 경로의 값 — `[이름]` 조각은 그 이름의 항목. 없으면 MISSING."""
    for part in path:
        if isinstance(part, str) and part.startswith("[") and isinstance(obj, list):
            obj = next((i for i in obj if isinstance(i, dict) and f"[{i.get('name')}]" == part), MISSING)
        elif isinstance(part, int) and isinstance(obj, list):
            obj = obj[part] if part < len(obj) else MISSING
        elif isinstance(obj, dict):
            obj = obj.get(part, MISSING)
        else:
            return MISSING
        if obj is MISSING:
            return MISSING
    return obj


def show(path: FieldPath) -> str:
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        elif part.startswith("["):
            text += part
        else:
            text += f".{part}" if text else part
    return text


def under(path: FieldPath, prefix: FieldPath) -> bool:
    return path[:len(prefix)] == prefix


def pointer_path(pointer: str, obj: Any) -> FieldPath | None:
    """ignoreDifferences 의 JSON Pointer(/spec/template/spec/containers/0/env) 를 이 모듈의 경로로.

    숫자 조각은 그 자리 항목에 이름이 있으면 `[이름]` 으로 바꾼다. 없는 항목 번호를 가리키면 None — 아무 칸도 가리지 않는다
    (그 앞까지로 자르면 경로가 넓어져, 고치려는 칸을 덮는다고 잘못 볼 수 있다).
    """
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    path: list[str | int] = []
    here = obj
    for part in parts:
        if isinstance(here, list) and part.isdigit():
            index_ = int(part)
            if index_ >= len(here):
                return None
            here = here[index_]
            path.append(item(here["name"]) if isinstance(here, dict) and isinstance(here.get("name"), str)
                        else index_)
        elif isinstance(here, dict):
            path.append(part)
            here = here.get(part)
        else:
            path.append(part)
            here = None
    return tuple(path)


def dump(obj: dict) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=4096)


def unified(before: str | None, after: str, before_name: str, after_name: str) -> str:
    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=before_name, tofile=after_name)
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
