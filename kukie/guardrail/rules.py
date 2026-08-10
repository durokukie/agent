"""룰 엔진 — LLM 밖 순수 함수. 같은 입력이면 항상 같은 출력.

- 룰은 rules.yaml에 데이터로 선언 (4축 매칭: verb/kind/namespace/flags)
- fail-closed: 매칭되는 룰이 없으면 최고 등급(destructive)
- evaluate_action(사전 검토)과 훅이 이 코드를 공유한다 — 별도 판정 로직 금지
"""
from __future__ import annotations

from enum import IntEnum
from pathlib import Path

import yaml


class Risk(IntEnum):     # IntEnum이라 max()로 최고 등급 집계 가능
    SAFE = 0
    CAUTION = 1
    DESTRUCTIVE = 2


class RuleEngine:
    def __init__(self, path: Path | None = None):
        path = path or Path(__file__).parent / "rules.yaml"
        self.rules: list[dict] = yaml.safe_load(path.read_text())["rules"]

    def classify(self, cmd: dict) -> tuple[Risk, list[str]]:
        """조립된 명령(dict: verb/kind/namespace/flags) → (등급, 걸린 룰 id들)."""
        hit = [r for r in self.rules if self._matches(r["match"], cmd)]
        if not hit:
            return Risk.DESTRUCTIVE, ["no-match(fail-closed)"]
        risk = max(Risk[r["risk"].upper()] for r in hit)
        return risk, [r["id"] for r in hit]

    @staticmethod
    def _matches(match: dict, cmd: dict) -> bool:
        """조건에 쓴 축은 전부 맞아야 함(AND). flags는 하나라도 포함되면 매칭."""
        raise NotImplementedError  # TODO
