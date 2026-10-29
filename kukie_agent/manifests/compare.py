"""렌더 결과 비교 — 바뀐 것이 요청한 리소스의 요청한 칸뿐인지 (DURO-133).

객체는 (apiVersion, kind, namespace, name) 으로 맞추고, 칸은 경로(튜플)로 센다.
이름(name)이 있는 항목의 목록(containers · env …)은 순서가 아니라 이름으로 맞춘다 — 경로 조각이 `[api]` 처럼 된다.
"""
from __future__ import annotations

import difflib
from typing import Any

import yaml

from kukie_agent.manifests.models import Target

Key = tuple[str, str, str, str]
FieldPath = tuple[str | int, ...]


def key_of(obj: dict) -> Key:
    meta = obj.get("metadata") or {}
    return (str(obj.get("apiVersion")), str(obj.get("kind")),
            str(meta.get("namespace") or ""), str(meta.get("name") or ""))


def index(objects: list[dict]) -> dict[Key, dict]:
    return {key_of(obj): obj for obj in objects}


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

    숫자 조각은 그 자리 항목에 이름이 있으면 `[이름]` 으로 바꾼다. 객체에 없는 곳까지 가면 그 앞까지만 돌려준다.
    """
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    path: list[str | int] = []
    here = obj
    for part in parts:
        if isinstance(here, list) and part.isdigit():
            index_ = int(part)
            if index_ >= len(here):
                return tuple(path)
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
