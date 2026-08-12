"""Parsed-command rule engine for kubectl guardrails.

Unmatched kubectl forms require review rather than being mislabeled destructive.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import yaml


_SEPARATORS = {"|", "||", "&&", ";", "&", "\n"}
_WRAPPERS = {"sudo", "env"}
_FLAG_ALIASES = {
    "-n": "--namespace",
    "-A": "--all-namespaces",
    "-w": "--watch",
    "-o": "--output",
    "-l": "--selector",
    "-c": "--container",
    "-k": "--kustomize",
    "-i": "--stdin",
    "-t": "--tty",
    "-it": "--stdin-tty",
}
_VALUE_FLAGS = {
    "--context", "--namespace", "--output", "--filename", "--kustomize",
    "--selector", "--field-selector", "--sort-by", "--tail", "--since",
    "--container", "--grace-period", "--dry-run", "--image", "--target",
    "--replicas", "--types", "--for", "--address",
}
_BOOLEAN_FLAGS = {
    "--all-namespaces", "--watch", "--follow", "--previous",
    "--all-containers", "--force", "--all", "--minify", "--client",
    "--prune", "--current", "--stdin", "--tty", "--stdin-tty",
}
_RESOURCE_ALIASES = {
    "pods": "pod",
    "po": "pod",
    "deploy": "deployment",
    "deploys": "deployment",
    "deployments": "deployment",
    "svc": "service",
    "services": "service",
    "secrets": "secret",
}


class Risk(IntEnum):
    SAFE = 0
    CAUTION = 1
    REVIEW_REQUIRED = 2
    DESTRUCTIVE = 3


@dataclass(frozen=True)
class Classification:
    level: Risk
    matched_rules: list[str]
    analyzed_commands: list[str]
    reason: str


def _canonical_flag(token: str, verb: str) -> str:
    """축약 옵션을 규칙에서 사용하는 표준 옵션 이름으로 변환한다."""
    if token == "-f":
        return "--follow" if verb == "logs" else "--filename"
    return _FLAG_ALIASES.get(token, token)


def _normalize_and_split_resource(value: str) -> tuple[str, str | None]:
    """kubectl 리소스 표기를 규칙 매칭용 `(kind, name)`으로 변환한다.

    단축어(`po`)와 복수형(`pods`)을 표준 리소스 이름(`pod`)으로 정규화하고,
    `kind/name` 형식은 리소스 종류와 개별 리소스 이름으로 분리한다.
    여러 리소스 종류를 쉼표로 지정한 입력은 단일 규칙으로 판단할 수 없어 거부한다.
    """
    if "," in value:
        raise ValueError("unsupported resource list")
    kind, separator, name = value.partition("/")
    return _RESOURCE_ALIASES.get(kind, kind.removesuffix("s")), name if separator else None


def _find_verb(args: list[str]) -> tuple[int, str]:
    """전역 옵션을 건너뛰고 kubectl 동사의 위치와 이름을 찾는다."""
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            return index, token.lower()
        name = token.split("=", 1)[0]
        name = _FLAG_ALIASES.get(name, name)
        if "=" not in token and name in _VALUE_FLAGS:
            if index + 1 >= len(args):
                raise ValueError(f"missing option value: {token}")
            index += 2
        elif name in _BOOLEAN_FLAGS:
            index += 1
        else:
            raise ValueError(f"unknown global option: {token}")
    raise ValueError("kubectl verb not found")


def _parse_kubectl_arguments(
    command_args: list[str], verb: str,
) -> tuple[list[str], set[str], dict[str, str]]:
    """kubectl 인수를 위치 인수, 활성화된 boolean flag, flag 값으로 분류한다.

    kubectl의 선택 인수는 모두 flag다. `--watch`, `--force` 같은 boolean
    flag는 활성화 여부를 집합에 저장하고, `--namespace=prod`처럼 값을
    받는 flag는 이름과 값을 딕셔너리에 저장한다.
    """
    positionals: list[str] = []
    enabled_boolean_flags: set[str] = set()
    flag_values: dict[str, str] = {}
    index = 0
    while index < len(command_args):
        token = command_args[index]
        if not token.startswith("-"):
            positionals.append(token)
            index += 1
            continue

        raw_name, separator, inline_value = token.partition("=")
        name = _canonical_flag(raw_name, verb)
        if separator:
            if name not in _VALUE_FLAGS | _BOOLEAN_FLAGS:
                raise ValueError(f"unknown kubectl option: {token}")
            flag_values[name] = inline_value
            if name in _BOOLEAN_FLAGS and inline_value.lower() == "true":
                enabled_boolean_flags.add(name)
            index += 1
        elif name in _VALUE_FLAGS:
            if index + 1 >= len(command_args):
                raise ValueError(f"missing option value: {token}")
            flag_values[name] = command_args[index + 1]
            index += 2
        elif name in _BOOLEAN_FLAGS:
            enabled_boolean_flags.add(name)
            index += 1
        else:
            raise ValueError(f"unknown kubectl option: {token}")

    return positionals, enabled_boolean_flags, flag_values


def _parse_kubectl_tokens(tokens: list[str]) -> dict:
    """kubectl 토큰을 YAML 규칙이 비교할 수 있는 표준 명령 구조로 변환한다."""
    # kubectl 실행 파일인지 확인하고, 원문만으로 확정할 수 없는 인수를 거부한다.
    if not tokens or Path(tokens[0]).name != "kubectl":
        raise ValueError("kubectl command not found")
    if any("$" in token or "`" in token for token in tokens):
        raise ValueError("dynamic kubectl argument")
    if any("<" in token or ">" in token for token in tokens):
        raise ValueError("unsupported kubectl redirection")

    # 전역 옵션을 건너뛰어 kubectl 동사를 찾고, 동사를 제외한 인수만 남긴다.
    args = tokens[1:]
    verb_index, verb = _find_verb(args)
    command_args = args[:verb_index] + args[verb_index + 1:]

    # `--` 뒤의 exec payload를 kubectl 자체 옵션과 분리한다.
    try:
        payload_index = command_args.index("--")
    except ValueError:
        payload = []
    else:
        payload = command_args[payload_index + 1:]
        command_args = command_args[:payload_index]

    # 나머지 인수를 위치 인수, 불리언 플래그, 값이 있는 옵션으로 정규화한다.
    positionals, enabled_boolean_flags, flag_values = (
        _parse_kubectl_arguments(command_args, verb)
    )

    # 동사별 위치 인수에서 하위 명령, 리소스 종류와 이름을 추출한다.
    resource = name = subcommand = None
    if verb in {"config", "rollout"}:
        if positionals:
            subcommand = positionals[0]
    elif verb in {"get", "describe", "delete"}:
        if positionals:
            resource, name = _normalize_and_split_resource(positionals[0])
            name = name or (positionals[1] if len(positionals) > 1 else None)
    elif verb in {"logs", "exec", "debug"}:
        if positionals:
            resource, name = (
                _normalize_and_split_resource(positionals[0])
                if "/" in positionals[0]
                else ("pod", positionals[0])
            )

    # 규칙 매칭에 필요한 필수 대상이나 옵션이 빠졌는지 검증한다.
    if verb in {"get", "describe", "delete", "logs", "exec"} and resource is None:
        raise ValueError(f"missing resource target: {verb}")
    if verb == "apply" and not ({"--filename", "--kustomize"} & flag_values.keys()):
        raise ValueError("apply requires --filename or --kustomize")
    if verb == "config" and subcommand is None:
        raise ValueError("config requires subcommand")

    # YAML 규칙이 직접 비교할 수 있는 표준 명령 구조를 만든다.
    return {
        "verb": verb,
        "subcommand": subcommand,
        "resource": resource,
        "name": name,
        "namespace": flag_values.get("--namespace"),
        "context": flag_values.get("--context"),
        "enabled_boolean_flags": enabled_boolean_flags,
        "flag_values": flag_values,
        "dry_run": flag_values.get("--dry-run", "none"),
        "sensitive_output": (
            verb == "get" and resource == "secret"
            and flag_values.get("--output") in {"yaml", "json"}
        ),
        "payload": payload,
    }


def _shell_tokens(raw_command: str) -> list[str]:
    """셸 명령을 파이프와 연산자를 보존한 토큰 목록으로 변환한다."""
    lexer = shlex.shlex(
        raw_command,
        posix=True,
        punctuation_chars="|&;<>\n",
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _shell_segments(tokens: list[str]) -> list[list[str]]:
    """셸 연산자를 기준으로 독립적으로 분석할 명령 구간을 분리한다."""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _load_and_validate_rules(path: Path) -> tuple[list[dict], dict]:
    """YAML 규칙을 로드하고 필수 구조와 값을 검증한다."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("rules.yaml must have version: 1")

    rules = data.get("rules")
    fallbacks = data.get("fallback")
    if not isinstance(rules, list) or not isinstance(fallbacks, dict):
        raise ValueError("rules.yaml requires rules and fallback")
    missing = {"parse_error", "no_match", "dynamic_argument"} - fallbacks.keys()
    if missing:
        raise ValueError(f"rules.yaml missing fallback: {', '.join(sorted(missing))}")

    ids: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or not {"id", "risk", "match"} <= rule.keys():
            raise ValueError("each rule requires id, risk, and match")
        if not isinstance(rule["match"], dict):
            raise ValueError("rule match must be a mapping")
        if rule["id"] in ids:
            raise ValueError(f"duplicate rule id: {rule['id']}")
        ids.add(rule["id"])
        if rule["risk"].upper() not in {"SAFE", "CAUTION", "DESTRUCTIVE"}:
            raise ValueError(f"unknown risk: {rule['risk']}")

    for kind, level in fallbacks.items():
        if str(level).upper() != "REVIEW_REQUIRED":
            raise ValueError(f"fallback {kind} must be review_required")

    return rules, fallbacks


class RuleEngine:
    """정규화된 kubectl 명령을 YAML 규칙에 따라 위험도로 분류한다."""

    def __init__(self, path: Path | None = None):
        path = path or Path(__file__).parent / "rules.yaml"
        self.rules, self.fallbacks = _load_and_validate_rules(path)

    def fallback(self, kind: str) -> Risk:
        """오류 또는 미매칭 유형에 대응하는 fallback 위험도를 반환한다."""
        try:
            return Risk[str(self.fallbacks[kind]).upper()]
        except KeyError as exc:
            raise ValueError(f"unknown fallback: {kind}") from exc

    def classify(self, cmd: dict) -> tuple[Risk, list[str]]:
        """일치한 모든 규칙 중 최고 위험도와 규칙 ID 목록을 반환한다."""
        hit = [rule for rule in self.rules if self._matches(rule["match"], cmd)]
        if not hit:
            return self.fallback("no_match"), ["no-match"]
        return (
            max(Risk[rule["risk"].upper()] for rule in hit),
            [rule["id"] for rule in hit],
        )

    @staticmethod
    def _matches(match: dict, cmd: dict) -> bool:
        """정규화된 명령이 YAML의 모든 매칭 조건을 만족하는지 확인한다."""
        for key, expected in match.items():
            if key == "any_enabled_boolean_flags":
                if not set(expected) & set(cmd.get("enabled_boolean_flags", ())):
                    return False
            elif key == "required_flag_values":
                actual = cmd.get("flag_values", {})
                if any(str(actual.get(name)) != str(value)
                       for name, value in expected.items()):
                    return False
            elif isinstance(expected, list):
                if cmd.get(key) not in expected:
                    return False
            elif cmd.get(key) != expected:
                return False
        return True


def _classify_kubectl_segment(
    tokens: list[str], engine: RuleEngine,
) -> Classification:
    """kubectl 명령 구간 하나를 파싱하고 위험도로 분류한다."""
    try:
        parsed = _parse_kubectl_tokens(tokens)
    except ValueError as exc:
        kind = "dynamic_argument" if "dynamic" in str(exc) else "parse_error"
        return Classification(
            engine.fallback(kind), [], [shlex.join(tokens)], f"{kind}: {exc}"
        )

    level, matched = engine.classify(parsed)
    return Classification(
        level, matched, [shlex.join(tokens)],
        "matched rules: " + ", ".join(matched),
    )


def classify_kubectl_command_risk(raw_command: str) -> Classification:
    """셸 문자열의 모든 kubectl 명령을 분석해 가장 높은 위험도를 반환한다."""
    raw_command = raw_command.strip()
    if not raw_command:
        raise ValueError("kubectl command not found")

    engine = RuleEngine()
    try:
        segments = _shell_segments(_shell_tokens(raw_command))
    except ValueError:
        if not re.search(r"(?:^|[\s/])kubectl(?:\s|$)", raw_command):
            raise ValueError("kubectl command not found")
        return Classification(
            engine.fallback("parse_error"), [], [raw_command], "parse_error"
        )

    results: list[Classification] = []
    for segment in segments:
        executable = Path(segment[0]).name
        if executable == "kubectl":
            results.append(_classify_kubectl_segment(segment, engine))
        elif executable in _WRAPPERS and any(
            Path(token).name == "kubectl" for token in segment[1:]
        ):
            results.append(Classification(
                engine.fallback("parse_error"), [], [shlex.join(segment)],
                "parse_error: wrapped kubectl command",
            ))
        else:
            index = 0
            while index < len(segment) and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[index]
            ):
                index += 1
            if index and index < len(segment) and Path(segment[index]).name == "kubectl":
                results.append(Classification(
                    engine.fallback("parse_error"), [], [shlex.join(segment)],
                    "parse_error: assignment-prefixed kubectl command",
                ))

    if not results:
        raise ValueError("kubectl command not found")

    level = max(result.level for result in results)
    matched_rules = list(dict.fromkeys(
        rule for result in results for rule in result.matched_rules
    ))
    analyzed_commands = [
        command for result in results for command in result.analyzed_commands
    ]
    reasons = list(dict.fromkeys(
        result.reason for result in results if result.level == level
    ))
    return Classification(level, matched_rules, analyzed_commands, "; ".join(reasons))
