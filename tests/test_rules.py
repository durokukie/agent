"""Parsed-command policy tests for the kubectl rule engine."""
from pathlib import Path

import pytest

from kukie.guardrail.rules import Risk, RuleEngine


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
