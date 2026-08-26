from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

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
    if tool_name == "apply_manifest":
        normalized["namespace"] = normalized.get("namespace") or default_namespace
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
