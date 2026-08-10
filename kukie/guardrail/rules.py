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


_SEPARATORS = {"|", "||", "&&", ";", "&", "\n", ">", ">>", "<", "<<"}
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
    if token == "-f":
        return "--follow" if verb == "logs" else "--filename"
    return _FLAG_ALIASES.get(token, token)


def _resource(value: str) -> tuple[str, str | None]:
    kind, separator, name = value.partition("/")
    return _RESOURCE_ALIASES.get(kind, kind.removesuffix("s")), name if separator else None


def _find_verb(args: list[str]) -> tuple[int, str]:
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


def _parse_kubectl_tokens(tokens: list[str]) -> dict:
    if not tokens or Path(tokens[0]).name != "kubectl":
        raise ValueError("kubectl command not found")
    if any("$(" in token or "`" in token for token in tokens):
        raise ValueError("dynamic kubectl argument")

    args = tokens[1:]
    verb_index, verb = _find_verb(args)
    command_args = args[:verb_index] + args[verb_index + 1:]
    try:
        payload_index = command_args.index("--")
    except ValueError:
        payload = []
    else:
        payload = command_args[payload_index + 1:]
        command_args = command_args[:payload_index]

    positionals: list[str] = []
    flags: set[str] = set()
    options: dict[str, str] = {}
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
            options[name] = inline_value
            if name in _BOOLEAN_FLAGS and inline_value.lower() == "true":
                flags.add(name)
            index += 1
        elif name in _VALUE_FLAGS:
            if index + 1 >= len(command_args):
                raise ValueError(f"missing option value: {token}")
            options[name] = command_args[index + 1]
            index += 2
        elif name in _BOOLEAN_FLAGS:
            flags.add(name)
            index += 1
        else:
            raise ValueError(f"unknown kubectl option: {token}")

    resource = name = subcommand = None
    if verb in {"config", "rollout"}:
        if positionals:
            subcommand = positionals[0]
    elif verb in {"get", "describe", "delete"}:
        if positionals:
            resource, name = _resource(positionals[0])
            name = name or (positionals[1] if len(positionals) > 1 else None)
    elif verb in {"logs", "exec", "debug"}:
        if positionals:
            resource, name = _resource(positionals[0]) if "/" in positionals[0] else ("pod", positionals[0])

    if verb in {"get", "describe", "delete", "logs", "exec"} and resource is None:
        raise ValueError(f"missing resource target: {verb}")
    if verb == "apply" and not ({"--filename", "--kustomize"} & options.keys()):
        raise ValueError("apply requires --filename or --kustomize")
    if verb == "config" and subcommand is None:
        raise ValueError("config requires subcommand")

    return {
        "verb": verb,
        "subcommand": subcommand,
        "resource": resource,
        "name": name,
        "namespace": options.get("--namespace"),
        "context": options.get("--context"),
        "flags": flags,
        "options": options,
        "dry_run": options.get("--dry-run", "none"),
        "sensitive_output": (
            verb == "get" and resource == "secret"
            and options.get("--output") in {"yaml", "json"}
        ),
        "payload": payload,
    }


def _shell_tokens(raw_command: str) -> list[str]:
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


class RuleEngine:
    def __init__(self, path: Path | None = None):
        path = path or Path(__file__).parent / "rules.yaml"
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("rules.yaml must have version: 1")

        self.rules = data.get("rules")
        self.fallbacks = data.get("fallback")
        if not isinstance(self.rules, list) or not isinstance(self.fallbacks, dict):
            raise ValueError("rules.yaml requires rules and fallback")

        ids: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, dict) or not {"id", "risk", "match"} <= rule.keys():
                raise ValueError("each rule requires id, risk, and match")
            if rule["id"] in ids:
                raise ValueError(f"duplicate rule id: {rule['id']}")
            ids.add(rule["id"])
            if rule["risk"].upper() not in {"SAFE", "CAUTION", "DESTRUCTIVE"}:
                raise ValueError(f"unknown risk: {rule['risk']}")

        for kind, level in self.fallbacks.items():
            if str(level).upper() != "REVIEW_REQUIRED":
                raise ValueError(f"fallback {kind} must be review_required")

    def fallback(self, kind: str) -> Risk:
        try:
            return Risk[str(self.fallbacks[kind]).upper()]
        except KeyError as exc:
            raise ValueError(f"unknown fallback: {kind}") from exc

    def classify(self, cmd: dict) -> tuple[Risk, list[str]]:
        hit = [rule for rule in self.rules if self._matches(rule["match"], cmd)]
        if not hit:
            return self.fallback("no_match"), ["no-match"]
        return (
            max(Risk[rule["risk"].upper()] for rule in hit),
            [rule["id"] for rule in hit],
        )

    @staticmethod
    def _matches(match: dict, cmd: dict) -> bool:
        for key, expected in match.items():
            if key == "flags_any":
                if not set(expected) & set(cmd.get("flags", ())):
                    return False
            elif key == "options":
                actual = cmd.get("options", {})
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
