"""Plan 기반 Electron 승인 DTO와 대기 요청 응답 검증."""
from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import unquote_plus

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ToolCallPart

from kukie.guardrail.action_plan import ActionPlan, PlanTarget
from kukie.guardrail.mutation_request import canonicalize_mutation_args

_REDACTED = "<redacted>"
_RECURSIVE_REFERENCE = "<recursive-reference>"
_MAX_PREVIEW_DEPTH = 100
_MAX_PREVIEW_NODES = 20_000
_SENSITIVE_MARKERS = (
    "accesskey",
    "apikey",
    "authorization",
    "bearer",
    "clientsecret",
    "connectionstring",
    "credential",
    "databaseurl",
    "dsn",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "signature",
    "token",
)
_SENSITIVE_QUERY_KEYS = frozenset({"auth", "code", "key", "sig"})
_SENSITIVE_VALUE_KEYS = frozenset({"material", "value"})
_SAFE_REFERENCE_KEYS = frozenset({
    "automountserviceaccounttoken",
    "imagepullsecrets",
    "secretkeyref",
    "secretname",
    "secretref",
    "serviceaccounttoken",
})
_SAFE_SCALAR_SUFFIXES = (
    "endpoint",
    "file",
    "mode",
    "path",
    "policy",
    "ttl",
    "url",
    "uri",
)
_SENSITIVE_ANNOTATIONS = frozenset({
    "kubectl.kubernetes.io/last-applied-configuration",
})
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\b(?:Basic|Bearer)\s+\S+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"^\s*kind:\s*Secret\s*$|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b)",
    re.IGNORECASE | re.MULTILINE,
)
_URL_CREDENTIAL_PATTERN = re.compile(r"//[^/@\s]+@")
_URL_PARAMETER_PATTERN = re.compile(r"([?&#])([^=&#]+)=([^&#]*)")


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_sensitive_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return normalized not in _SAFE_REFERENCE_KEYS and any(
        marker in normalized for marker in _SENSITIVE_MARKERS
    )


def _redact_url(value: str) -> str:
    redacted = _URL_CREDENTIAL_PATTERN.sub(f"//{_REDACTED}@", value)

    def redact_parameter(match: re.Match[str]) -> str:
        key = unquote_plus(match.group(2))
        if _is_sensitive_key(key) or _normalized_key(key) in _SENSITIVE_QUERY_KEYS:
            return f"{match.group(1)}{match.group(2)}={_REDACTED}"
        return match.group(0)

    return _URL_PARAMETER_PATTERN.sub(redact_parameter, redacted)


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<binary: {len(value)} bytes>"
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "<non-finite: nan>"
        sign = "-" if value < 0 else ""
        return f"<non-finite: {sign}inf>"
    if isinstance(value, str):
        redacted = _redact_url(value)
        return _REDACTED if _SENSITIVE_VALUE_PATTERN.search(redacted) else redacted
    return value


def _redact_sensitive_scalar(key: Any, value: Any) -> Any:
    safe_value = _safe_scalar(value)
    normalized = _normalized_key(str(key))
    if normalized.endswith(_SAFE_SCALAR_SUFFIXES):
        return safe_value
    if isinstance(value, str) and safe_value != value:
        return safe_value
    return _REDACTED


def _consume_preview_budget(budget: list[int], count: int = 1) -> None:
    budget[0] -= count
    if budget[0] < 0:
        raise ValueError("pending approval mismatch: manifest is too large")


def _redact_manifest_value(
    value: Any,
    ancestors: set[int] | None = None,
    budget: list[int] | None = None,
    redact_scalars: bool = False,
    depth: int = 0,
) -> Any:
    budget = [_MAX_PREVIEW_NODES] if budget is None else budget
    _consume_preview_budget(budget)
    if not isinstance(value, (dict, list)):
        return _REDACTED if redact_scalars else _safe_scalar(value)
    if depth >= _MAX_PREVIEW_DEPTH:
        raise ValueError("pending approval mismatch: manifest is too deeply nested")

    ancestors = set() if ancestors is None else ancestors
    value_id = id(value)
    if value_id in ancestors:
        return _RECURSIVE_REFERENCE
    ancestors.add(value_id)
    try:
        if isinstance(value, list):
            return [
                _redact_manifest_value(
                    item,
                    ancestors,
                    budget,
                    redact_scalars,
                    depth + 1,
                )
                for item in value
            ]

        is_secret = value.get("kind") == "Secret"
        named_value = isinstance(value.get("name"), str)
        redacted = {}
        for key, item in value.items():
            if is_secret and key in {"data", "stringData", "binaryData"}:
                _consume_preview_budget(
                    budget,
                    len(item) + 1 if isinstance(item, dict) else 1,
                )
                redacted[key] = (
                    {item_key: _REDACTED for item_key in item}
                    if isinstance(item, dict)
                    else _REDACTED
                )
            elif key in _SENSITIVE_ANNOTATIONS:
                _consume_preview_budget(budget)
                redacted[key] = _REDACTED
            elif named_value and key == "value":
                _consume_preview_budget(budget)
                redacted[key] = _REDACTED
            elif isinstance(item, (dict, list)):
                normalized = _normalized_key(str(key))
                child_redacts_scalars = (
                    False
                    if normalized == "secret" or normalized.endswith("policy")
                    else redact_scalars or _is_sensitive_key(str(key))
                )
                redacted[key] = _redact_manifest_value(
                    item,
                    ancestors,
                    budget,
                    child_redacts_scalars,
                    depth + 1,
                )
            elif (
                _is_sensitive_key(str(key))
                or _normalized_key(str(key)) in _SENSITIVE_VALUE_KEYS
                or redact_scalars
            ):
                _consume_preview_budget(budget)
                redacted[key] = _redact_sensitive_scalar(key, item)
            else:
                redacted[key] = _redact_manifest_value(item, ancestors, budget)
        return redacted
    finally:
        ancestors.remove(value_id)


def _manifest_preview(manifest_yaml: str) -> list[dict[str, Any]]:
    try:
        documents = [
            document
            for document in yaml.safe_load_all(manifest_yaml)
            if document is not None
        ]
    except yaml.YAMLError as exc:
        raise ValueError("pending approval mismatch: manifest_yaml") from exc
    if not documents or any(not isinstance(document, dict) for document in documents):
        raise ValueError("pending approval mismatch: manifest_yaml")
    budget = [_MAX_PREVIEW_NODES]
    return [
        _redact_manifest_value(document, budget=budget)
        for document in documents
    ]


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    plan_id: str
    tool: str
    target: PlanTarget
    command: list[str]
    risk: str
    intent: str
    expected_effects: list[str]
    side_effects: list[str]
    dry_run_result: dict[str, object]
    decision_guidance: str
    manifest_preview: list[dict[str, Any]] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    manifest_sha256: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


def build_approval_request(
    call: ToolCallPart,
    metadata: dict[str, Any],
    *,
    default_namespace: str,
    run_id: str | None = None,
) -> ApprovalRequest:
    call_id = call.tool_call_id
    if not call_id:
        raise ValueError("pending approval mismatch: missing call_id")
    plan = ActionPlan.find_by_call_id(call_id, run_id=run_id)
    canonical = canonicalize_mutation_args(
        call.tool_name,
        call.args_as_dict(raise_if_invalid=True),
        default_namespace,
    )
    if (
        metadata.get("plan_id") != plan.id
        or call.tool_name != plan.tool
        or canonical.plan != plan.args
        or canonical.normalized.get("intent") != plan.intent
        or canonical.normalized.get("expected_effects") != plan.expected_effects
        or canonical.normalized.get("side_effects") != plan.side_effects
        or plan.status != "WAITING_APPROVAL"
        or not isinstance(plan.dry_run_result, dict)
        or plan.dry_run_result.get("status") not in {"succeeded", "unsupported"}
        or not plan.decision_guidance
    ):
        raise ValueError("pending approval mismatch")
    manifest_yaml = (
        canonical.normalized["manifest_yaml"]
        if call.tool_name == "apply_manifest"
        else None
    )
    return ApprovalRequest(
        tool_call_id=call_id,
        plan_id=plan.id,
        tool=plan.tool,
        target=plan.target,
        command=plan.command,
        risk=plan.risk_level,
        intent=plan.intent,
        expected_effects=plan.expected_effects,
        side_effects=plan.side_effects,
        dry_run_result=plan.dry_run_result,
        decision_guidance=plan.decision_guidance,
        manifest_preview=(
            _manifest_preview(manifest_yaml)
            if manifest_yaml is not None
            else None
        ),
        manifest_sha256=(
            canonical.plan["manifest_sha256"]
            if call.tool_name == "apply_manifest"
            else None
        ),
    )
