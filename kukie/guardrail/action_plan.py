"""Action Plan — 변경 1건당 1장씩 생기는 .md 계획서/기록.

팀 결정 (가드레일 v2):
- Action Plan은 훅이 내부적으로 생성·기록한다. LLM이 호출하는 Plan 툴(조회/평가)은
  MVP 제외 — 필요해지면 히스토리 기능과 함께 추가.
- frontmatter(기계용) = 코드가 아는 사실 (명령·대상·등급·dry-run·승인·결과)
- 본문(사람용) = LLM이 툴 인자로 제출한 intent/예상 영향/부작용 ("왜"의 기록)

상태: draft → executed / failed / rejected. dry-run 실패도 failed 기록으로 보관한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from re import sub
from tempfile import NamedTemporaryFile

import yaml

PLAN_DIR = Path.home() / ".kukie" / "plans"
FINAL_STATUSES = frozenset({"executed", "failed", "rejected"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


class ActionPlan:
    def __init__(self, plan_id: str, path: Path):
        self.id = plan_id
        self.path = path

    @classmethod
    def create_draft(
        cls,
        *,
        tool: str,
        command: list[str],
        risk: str,
        skill: str,
        target: dict[str, str],
        intent: str,
        expected_effects: list[str],
        side_effects: list[str],
    ) -> "ActionPlan":
        created_at = _utc_now()
        timestamp = datetime.fromisoformat(created_at).strftime("%y%m%d-%H%M")
        tool_name = sub(r"[^\w-]+", "-", tool).strip("-") or "tool"
        base_id = f"ap-{timestamp}-{tool_name}"
        metadata = {
            "id": base_id,
            "created_at": created_at,
            "tool": tool,
            "skill": skill,
            "target": dict(target),
            "command": list(command),
            "risk_level": risk,
            "status": "draft",
            "dry_run_result": None,
            "approval": None,
            "execution_result": None,
        }
        body = (
            f"# Intent\n\n{intent}\n\n"
            f"## Expected Effects\n\n{_bullets(expected_effects)}\n\n"
            f"## Side Effects\n\n{_bullets(side_effects)}\n"
        )
        suffix = 1
        while True:
            plan_id = base_id if suffix == 1 else f"{base_id}-{suffix}"
            plan = cls(plan_id, PLAN_DIR / f"{plan_id}.md")
            metadata["id"] = plan_id
            try:
                plan._write(metadata, body, exclusive=True)
            except FileExistsError:
                suffix += 1
            else:
                return plan

    def _write(
        self,
        metadata: dict,
        body: str,
        *,
        exclusive: bool = False,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f"{yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)}"
            "---\n"
            f"{body}"
        )
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as temp:
                temp_path = Path(temp.name)
                temp.write(content)
            if exclusive:
                self.path.hardlink_to(temp_path)
            else:
                temp_path.replace(self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _read(self) -> tuple[dict, str]:
        text = self.path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"invalid Action Plan frontmatter: {self.path}")
        frontmatter, separator, body = text[4:].partition("\n---\n")
        if not separator:
            raise ValueError(f"invalid Action Plan frontmatter: {self.path}")
        metadata = yaml.safe_load(frontmatter)
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid Action Plan frontmatter: {self.path}")
        return metadata, body

    def _update(self, key: str, value: object) -> None:
        metadata, body = self._read()
        metadata[key] = value
        self._write(metadata, body)

    def record_dry_run(self, output: str, ok: bool) -> None:
        self._update(
            "dry_run_result",
            {"success": ok, "output": output, "at": _utc_now()},
        )

    def record_approval(self, mode: str) -> None:
        self._update("approval", {"mode": mode, "at": _utc_now()})

    def record_result(self, output: str, ok: bool) -> None:
        self._update(
            "execution_result",
            {"success": ok, "output": output, "at": _utc_now()},
        )

    def mark(self, status: str) -> None:
        if status not in FINAL_STATUSES:
            raise ValueError(f"invalid Action Plan status: {status}")
        self._update("status", status)

def list_plans(**filters) -> list[dict]:
    """히스토리 스킬용 — frontmatter만 파싱해 요약 목록 반환 (본문 안 읽음, 토큰 절약)."""
    raise NotImplementedError  # TODO


def get_plan(plan_id: str) -> str:
    """히스토리 스킬용 — 특정 Plan .md 전문."""
    raise NotImplementedError  # TODO
