"""Action Plan — 변경 1건당 1장씩 생기는 .md 계획서/기록 (기능 2 §1.2, 저장 위치 논의 중).

파일 = YAML frontmatter(기계용) + 본문(사람용).
작성 주체 경계: LLM=intent/영향/부작용, 코드=사실(명령·결과·승인), 룰엔진=위험도.
상태: draft → executed / failed / rejected. (dry-run 실패한 draft는 삭제 — 팀 합의 필요)
"""
from __future__ import annotations

from pathlib import Path

PLAN_DIR = Path.home() / ".kukie" / "plans"


class ActionPlan:
    def __init__(self, plan_id: str, path: Path):
        self.id = plan_id
        self.path = path

    @classmethod
    def create_draft(cls, *, tool: str, command: list[str], intent: str,
                     effects: list[str], side_effects: list[str],
                     risk: str, rules: list[str], skill: str) -> "ActionPlan":
        """파이프라인 ④단계 — draft 상태 .md 생성."""
        raise NotImplementedError  # TODO: frontmatter + 본문 렌더링

    def record_dry_run(self, output: str) -> None: ...        # ⑤
    def record_approval(self, mode: str) -> None: ...         # ⑥
    def record_result(self, output: str, ok: bool) -> None: ...  # ⑧
    def mark(self, status: str) -> None:
        """status 갱신: executed / failed / rejected"""
        raise NotImplementedError  # TODO

    def delete_draft(self) -> None:
        """dry-run 실패 시 draft 정리 (기록 정책 — 팀 합의 후 확정)."""
        raise NotImplementedError  # TODO


def list_plans(**filters) -> list[dict]:
    """히스토리 스킬용 — frontmatter만 파싱해 요약 목록 반환 (본문 안 읽음, 토큰 절약)."""
    raise NotImplementedError  # TODO


def get_plan(plan_id: str) -> str:
    """히스토리 스킬용 — 특정 Plan .md 전문."""
    raise NotImplementedError  # TODO
