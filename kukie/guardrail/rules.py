"""Parsed-command rule engine for kubectl guardrails.

Unmatched kubectl forms require review rather than being mislabeled destructive.
"""
from __future__ import annotations

from enum import IntEnum
from pathlib import Path

import yaml


class Risk(IntEnum):
    SAFE = 0
    CAUTION = 1
    REVIEW_REQUIRED = 2
    DESTRUCTIVE = 3


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
