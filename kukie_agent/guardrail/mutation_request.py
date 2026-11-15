from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from kukie_agent.manifests import Change

_CHANGE = TypeAdapter(Change)
_DESCRIPTION_FIELDS = frozenset({"intent", "expected_effects", "side_effects"})


@dataclass(frozen=True)
class CanonicalMutationArgs:
    normalized: dict[str, Any]
    plan: dict[str, object]


def canonicalize_mutation_args(
    tool_name: str,
    args: dict[str, Any],
    default_namespace: str,
) -> CanonicalMutationArgs:
    normalized = dict(args)
    if tool_name in ("apply_manifest", "propose_git_change"):
        normalized["namespace"] = normalized.get("namespace") or default_namespace
    if tool_name == "propose_git_change":
        # 훅은 검증된 모델을, 승인 카드는 날 JSON 을 받는다 — 한 모양(기본값까지 채운 JSON)으로 맞춰야 Plan 의 args 와 같다
        try:
            normalized["change"] = _CHANGE.validate_python(normalized.get("change")).model_dump(mode="json")
        except ValidationError:
            raise ValueError("pending approval mismatch: change") from None
    values = {
        key: value
        for key, value in normalized.items()
        if key not in _DESCRIPTION_FIELDS
    }
    if tool_name == "apply_manifest":
        manifest = values.pop("manifest_yaml", None)
        if not isinstance(manifest, str):
            raise ValueError("pending approval mismatch: manifest_yaml")
        values["manifest_sha256"] = hashlib.sha256(
            manifest.encode("utf-8")
        ).hexdigest()
    if tool_name == "propose_git_change" and values["change"].get("what") == "new_resource":
        # apply_manifest 와 같다 — Plan 의 args 에는 내용 대신 해시만. 내용은 PR(gitops.files 의 blob)에 있다 (DURO-154)
        change = dict(values["change"])
        change["manifest_sha256"] = hashlib.sha256(change.pop("manifest_yaml").encode("utf-8")).hexdigest()
        values["change"] = change
    if tool_name == "propose_git_change" and values["change"].get("what") == "patch":
        # 경로는 그대로 두고 큰 값만 해시로 — 경로 목록은 승인 카드·판단 가이드가 읽고, 값은 PR 과 render_diff 에 있다 (DURO-249)
        change = dict(values["change"])
        change["operations"] = [_patch_operation(operation) for operation in change["operations"]]
        values["change"] = change
    return CanonicalMutationArgs(normalized=normalized, plan=values)


#: 이보다 긴(JSON) 경로 지정 변경 값은 Plan 의 args 에 해시로 남긴다
PATCH_VALUE_INLINE = 200


def _patch_operation(operation: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(operation.get("value"), ensure_ascii=False, sort_keys=True)
    if len(text) <= PATCH_VALUE_INLINE:
        return operation
    rest = {key: value for key, value in operation.items() if key != "value"}
    return {**rest, "value_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
