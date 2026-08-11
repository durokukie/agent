"""Parsed-command policy tests for the kubectl rule engine."""
from pathlib import Path

import pytest

import kukie.guardrail.rules as rules_module
from kukie.guardrail.rules import (
    Risk,
    RuleEngine,
    classify_kubectl_command_risk,
)


engine = RuleEngine()


def test_yaml_규칙을_별도_함수로_로드하고_검증한다(tmp_path: Path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "version: 1\n"
        "fallback:\n"
        "  parse_error: review_required\n"
        "  no_match: review_required\n"
        "  dynamic_argument: review_required\n"
        "rules:\n"
        "  - id: GET\n"
        "    risk: safe\n"
        "    match: {verb: get}\n"
    )

    rules, fallbacks = rules_module._load_and_validate_rules(path)

    assert rules == [{"id": "GET", "risk": "safe", "match": {"verb": "get"}}]
    assert fallbacks["parse_error"] == "review_required"


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
def test_파싱된_명령을_규칙에_따라_분류한다(cmd, expected, rule_id):
    risk, matched = engine.classify(cmd)
    assert risk == expected
    assert rule_id in matched


def test_알수없는_파싱명령은_승인이_필요하다():
    assert engine.classify({"verb": "frobnicate"}) == (
        Risk.REVIEW_REQUIRED,
        ["no-match"],
    )


def test_시크릿_원문조회는_일반조회_규칙과_매칭되지_않는다():
    risk, matched = engine.classify({
        "verb": "get",
        "resource": "secret",
        "flags": set(),
        "options": {"--output": "yaml"},
        "sensitive_output": True,
    })
    assert risk == Risk.REVIEW_REQUIRED
    assert matched == ["no-match"]


def test_잘못된_위험도_규칙은_로드에_실패한다(tmp_path: Path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "version: 1\n"
        "fallback:\n"
        "  parse_error: review_required\n"
        "  no_match: review_required\n"
        "  dynamic_argument: review_required\n"
        "rules:\n  - id: BAD\n    risk: unknown\n    match: {}\n"
    )
    with pytest.raises(ValueError, match="unknown risk"):
        RuleEngine(path)


@pytest.mark.parametrize("fallback", [
    "no_match: review_required\n  dynamic_argument: review_required",
    "parse_error: review_required\n  dynamic_argument: review_required",
    "parse_error: review_required\n  no_match: review_required",
])
def test_필수_폴백이_없으면_로드에_실패한다(tmp_path: Path, fallback: str):
    path = tmp_path / "rules.yaml"
    path.write_text(
        f"version: 1\nfallback:\n  {fallback}\nrules: []\n"
    )
    with pytest.raises(ValueError, match="missing fallback"):
        RuleEngine(path)


def test_매핑이_아닌_매칭조건은_로드에_실패한다(tmp_path: Path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "version: 1\n"
        "fallback:\n"
        "  parse_error: review_required\n"
        "  no_match: review_required\n"
        "  dynamic_argument: review_required\n"
        "rules:\n"
        "  - id: BAD\n"
        "    risk: safe\n"
        "    match: verb=get\n"
    )
    with pytest.raises(ValueError, match="match must be a mapping"):
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
def test_단일_kubectl_명령의_위험도를_분류한다(raw, expected, rule_id):
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
def test_미확정_kubectl_명령은_승인이_필요하다(raw):
    result = classify_kubectl_command_risk(raw)
    assert result.level == Risk.REVIEW_REQUIRED


def test_kubectl이_없는_명령은_검증범위_밖이다():
    with pytest.raises(ValueError, match="kubectl command not found"):
        classify_kubectl_command_risk("echo kubectl")


def test_알수없는_kubectl_옵션은_승인이_필요하다():
    assert classify_kubectl_command_risk(
        "kubectl get pods --unrecognized=value"
    ).level == Risk.REVIEW_REQUIRED


@pytest.mark.parametrize("raw", [
    "kubectl get secrets >out -o yaml",
    "kubectl get secrets < <(cat token) -o yaml",
])
def test_리다이렉션으로_kubectl_인수를_숨길수_없다(raw):
    assert classify_kubectl_command_risk(raw).level == Risk.REVIEW_REQUIRED


@pytest.mark.parametrize("raw", [
    'kubectl get secret token -o "$FORMAT"',
    'kubectl get secret token -o "${FORMAT}"',
    "kubectl get secret token -o $(format)",
    "kubectl get secret token -o `format`",
])
def test_동적_kubectl_인수는_승인이_필요하다(raw):
    assert classify_kubectl_command_risk(raw).level == Risk.REVIEW_REQUIRED


def test_쉼표로_구분된_시크릿_리소스는_승인이_필요하다():
    assert classify_kubectl_command_risk(
        "kubectl get secrets,configmaps -o yaml"
    ).level == Risk.REVIEW_REQUIRED


def test_환경변수_할당이_앞에_있는_kubectl은_승인이_필요하다():
    result = classify_kubectl_command_risk(
        "kubectl get pods; KUBECONFIG=x kubectl delete pod api"
    )
    assert result.level == Risk.REVIEW_REQUIRED
    assert len(result.analyzed_commands) == 2


@pytest.mark.parametrize("raw,expected,count", [
    ("kubectl get pods | grep api", Risk.SAFE, 1),
    ("echo start && kubectl apply -f app.yaml", Risk.CAUTION, 1),
    ("kubectl get pods && kubectl delete pod api", Risk.DESTRUCTIVE, 2),
    ("kubectl frobnicate pods ; kubectl get pods", Risk.REVIEW_REQUIRED, 2),
    ("kubectl frobnicate pods ; kubectl delete pod api", Risk.DESTRUCTIVE, 2),
])
def test_최상위_kubectl_명령을_모두_분류한다(raw, expected, count):
    result = classify_kubectl_command_risk(raw)
    assert result.level == expected
    assert len(result.analyzed_commands) == count


@pytest.mark.parametrize("raw", [
    "sudo kubectl get pods",
    "env KUBECONFIG=test kubectl get pods",
    "kubectl delete pod $(cat name)",
])
def test_모호한_kubectl_호출은_승인이_필요하다(raw):
    assert classify_kubectl_command_risk(raw).level == Risk.REVIEW_REQUIRED


def test_exec_페이로드의_파이프는_최상위_구분자가_아니다():
    result = classify_kubectl_command_risk(
        "kubectl exec api -- sh -c 'echo hi | grep h'"
    )
    assert result.level == Risk.REVIEW_REQUIRED
    assert len(result.analyzed_commands) == 1
