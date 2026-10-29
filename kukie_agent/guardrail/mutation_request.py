from __future__ import annotations

import hashlib
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
    return CanonicalMutationArgs(normalized=normalized, plan=values)
