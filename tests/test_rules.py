"""Parsed-command policy tests for the kubectl rule engine."""
from pathlib import Path

import pytest

from kukie.guardrail.rules import (
    Risk,
    RuleEngine,
    classify_kubectl_command_risk,
)


engine = RuleEngine()


@pytest.mark.parametrize("cmd,expected,rule_id", [
    ({"verb": "get", "resource": "pod", "flags": set(),
      "options": {}, "sensitive_output": False}, Risk.SAFE, "GET-BASE"),
    ({"verb": "logs", "resource": "pod", "flags": {"--follow"},
      "options": {}, "sensitive_output": False}, Risk.SAFE, "LOGS"),
    ({"verb": "apply", "dry_run": "none", "flags": set(),
      "options": {}, "sensitive_output": False}, Risk.CAUTION, "APPLY"),
    ({"verb": "config", "subcommand": "use-context", "flags": set(),
      "options": {}, "sensitive_output": False}, Risk.CAUTION, "CONFIG-SWITCH"),
    ({"verb": "delete", "resource": "pod", "flags": set(),
      "options": {}, "sensitive_output": False}, Risk.DESTRUCTIVE, "DELETE-POD"),
])
def test_classify_parsed_command(cmd, expected, rule_id):
    risk, matched = engine.classify(cmd)
    assert risk == expected
    assert rule_id in matched


def test_unknown_parsed_command_requires_review():
    assert engine.classify({"verb": "frobnicate"}) == (
        Risk.REVIEW_REQUIRED,
        ["no-match"],
    )


def test_sensitive_secret_output_does_not_match_general_get():
    risk, matched = engine.classify({
        "verb": "get",
        "resource": "secret",
        "flags": set(),
        "options": {"--output": "yaml"},
        "sensitive_output": True,
    })
    assert risk == Risk.REVIEW_REQUIRED
    assert matched == ["no-match"]


def test_invalid_rule_risk_fails_loading(tmp_path: Path):
    path = tmp_path / "rules.yaml"
    path.write_text("version: 1\nfallback:\n  no_match: review_required\n"
                    "rules:\n  - id: BAD\n    risk: unknown\n    match: {}\n")
    with pytest.raises(ValueError, match="unknown risk"):
        RuleEngine(path)


@pytest.mark.parametrize("raw,expected,rule_id", [
    ("kubectl get pods -A -w", Risk.SAFE, "GET-BASE"),
    ("kubectl logs -f api --tail=200", Risk.SAFE, "LOGS"),
    ("kubectl apply -f app.yaml", Risk.CAUTION, "APPLY"),
    ("kubectl config use-context production", Risk.CAUTION, "CONFIG-SWITCH"),
    ("kubectl delete pod api", Risk.DESTRUCTIVE, "DELETE-POD"),
    ("/usr/local/bin/kubectl delete deployment api",
     Risk.DESTRUCTIVE, "DELETE-DEPLOYMENT"),
])
def test_classify_single_kubectl_command(raw, expected, rule_id):
    result = classify_kubectl_command_risk(raw)
    assert result.level == expected
    assert rule_id in result.matched_rules
    assert len(result.analyzed_commands) == 1


@pytest.mark.parametrize("raw", [
    "kubectl frobnicate pods",
    "kubectl get secret token -o yaml",
    "kubectl apply -f app.yaml --dry-run=server",
    "kubectl exec api -- sh",
])
def test_unresolved_kubectl_command_requires_review(raw):
    result = classify_kubectl_command_risk(raw)
    assert result.level == Risk.REVIEW_REQUIRED


def test_missing_kubectl_is_out_of_scope():
    with pytest.raises(ValueError, match="kubectl command not found"):
        classify_kubectl_command_risk("echo kubectl")


def test_unknown_inline_kubectl_option_requires_review():
    assert classify_kubectl_command_risk(
        "kubectl get pods --unrecognized=value"
    ).level == Risk.REVIEW_REQUIRED


@pytest.mark.parametrize("raw,expected,count", [
    ("kubectl get pods | grep api", Risk.SAFE, 1),
    ("echo start && kubectl apply -f app.yaml", Risk.CAUTION, 1),
    ("kubectl get pods && kubectl delete pod api", Risk.DESTRUCTIVE, 2),
    ("kubectl frobnicate pods ; kubectl get pods", Risk.REVIEW_REQUIRED, 2),
    ("kubectl frobnicate pods ; kubectl delete pod api", Risk.DESTRUCTIVE, 2),
])
def test_classifies_all_top_level_kubectl_segments(raw, expected, count):
    result = classify_kubectl_command_risk(raw)
    assert result.level == expected
    assert len(result.analyzed_commands) == count


@pytest.mark.parametrize("raw", [
    "sudo kubectl get pods",
    "env KUBECONFIG=test kubectl get pods",
    "kubectl delete pod $(cat name)",
])
def test_ambiguous_kubectl_invocation_requires_review(raw):
    assert classify_kubectl_command_risk(raw).level == Risk.REVIEW_REQUIRED


def test_exec_payload_pipe_is_not_a_top_level_separator():
    result = classify_kubectl_command_risk(
        "kubectl exec api -- sh -c 'echo hi | grep h'"
    )
    assert result.level == Risk.REVIEW_REQUIRED
    assert len(result.analyzed_commands) == 1
