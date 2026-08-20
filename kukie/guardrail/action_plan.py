"""Action Plan — 변경 1건당 1장씩 생기는 구조화된 계획 객체와 .md 기록.

팀 결정 (가드레일 v2):
- Action Plan은 훅이 내부적으로 생성·기록한다. LLM이 호출하는 Plan 툴(조회/평가)은
  MVP 제외 — 필요해지면 히스토리 기능과 함께 추가.
- 실행 중에는 ActionPlan 객체가 기준이고, 변경할 때마다 .md 스냅샷을 갱신한다.
- frontmatter(기계용) = 명령·대상·등급·dry-run·승인·결과
- 본문(사람용) = 객체의 intent/예상 영향/부작용을 렌더링한 기록
- decision_guidance = 승인 전 별도 LLM 검토가 작성하는 판단 보조 필드

상태: draft → executed / failed / rejected. dry-run 실패도 failed 기록으로 보관한다.
"""
from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class ActionPlan:
    id: str
    path: Path
    created_at: str
    tool: str
    skill: str
    target: dict[str, str]
    command: list[str]
    risk_level: str
    status: str
    intent: str
    expected_effects: list[str]
    side_effects: list[str]
    dry_run_result: dict[str, object] | None = None
    decision_guidance: str | None = None
    approval: dict[str, object] | None = None
    execution_result: dict[str, object] | None = None

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
        # 파일명 원자적 예약: "있나 확인 → 쓰기"로 나누면 확인과 쓰기 사이에 다른 프로세스가
        # 같은 이름을 잡을 수 있다. 확인 없이 exclusive 쓰기를 시도하고, 이미 있으면(FileExistsError)
        # 다음 번호로 재시도한다 — 판단이 "쓰는 순간" OS에서 이뤄져 틈이 없다.
        suffix = 1
        while True:
            plan_id = base_id if suffix == 1 else f"{base_id}-{suffix}"
            plan = cls(
                id=plan_id,
                path=PLAN_DIR / f"{plan_id}.md",
                created_at=created_at,
                tool=tool,
                skill=skill,
                target=dict(target),
                command=list(command),
                risk_level=risk,
                status="draft",
                intent=intent,
                expected_effects=list(expected_effects),
                side_effects=list(side_effects),
            )
            try:
                plan._write(exclusive=True)
            except FileExistsError:   # 누군가 먼저 이 이름을 썼음 → -2, -3 …
                suffix += 1
            else:
                return plan

    def _metadata(self) -> dict[str, object]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "tool": self.tool,
            "skill": self.skill,
            "target": self.target,
            "command": self.command,
            "risk_level": self.risk_level,
            "status": self.status,
            "dry_run_result": self.dry_run_result,
            "decision_guidance": self.decision_guidance,
            "approval": self.approval,
            "execution_result": self.execution_result,
        }

    def _body(self) -> str:
        return (
            f"# Intent\n\n{self.intent}\n\n"
            f"## Expected Effects\n\n{_bullets(self.expected_effects)}\n\n"
            f"## Side Effects\n\n{_bullets(self.side_effects)}\n"
        )

    def render(self, *, include_decision_guidance: bool = True) -> str:
        metadata = self._metadata()
        if not include_decision_guidance:
            metadata.pop("decision_guidance")
        return (
            "---\n"
            f"{yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)}"
            "---\n"
            f"{self._body()}"
        )

    def _write(self, *, exclusive: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as temp:
                temp_path = Path(temp.name)
                temp.write(self.render())
            if exclusive:
                # hardlink_to는 대상 이름이 이미 존재하면 FileExistsError를 내고 절대 덮어쓰지 않는다.
                # 이 성질을 "없을 때만 생성"의 원자적 잠금으로 쓴다 (create_draft 전용).
                # 임시파일에 먼저 다 쓰고 이름을 붙이므로 반쪽 파일도 남지 않는다.
                self.path.hardlink_to(temp_path)
            else:
                # replace는 대상이 있으면 덮어쓴다 — 기존 Plan 갱신용 (record_*, mark).
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
        try:
            metadata = yaml.safe_load(frontmatter)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid Action Plan frontmatter: {self.path}") from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid Action Plan frontmatter: {self.path}")
        return metadata, body

    def _update(self, key: str, value: object) -> None:
        previous = getattr(self, key)
        setattr(self, key, value)
        try:
            self._write()
        except Exception:
            setattr(self, key, previous)
            raise

    def validate_for_decision_guidance(self) -> None:
        required = (
            "id",
            "created_at",
            "tool",
            "skill",
            "target",
            "command",
            "risk_level",
            "intent",
            "expected_effects",
            "side_effects",
        )
        for field in required:
            if not getattr(self, field):
                raise ValueError(
                    f"ActionPlan is not ready for guidance: missing={field}"
                )
        if self.status != "draft":
            raise ValueError("ActionPlan is not ready for guidance: status must be draft")

        dry_run = self.dry_run_result
        if not isinstance(dry_run, dict) or dry_run.get("success") is not True:
            raise ValueError(
                "ActionPlan is not ready for guidance: "
                "dry_run_result.success must be true"
            )
        if self.approval is not None:
            raise ValueError("ActionPlan is not ready for guidance: approval must be empty")
        if self.execution_result is not None:
            raise ValueError(
                "ActionPlan is not ready for guidance: execution_result must be empty"
            )
        if self.decision_guidance is not None:
            raise ValueError(
                "ActionPlan is not ready for guidance: decision guidance already exists"
            )

    def record_decision_guidance(self, text: str) -> None:
        guidance = text.strip()
        if not guidance:
            raise ValueError("decision guidance must not be empty")

        if self.decision_guidance is not None:
            raise ValueError("decision guidance already exists")
        self._update("decision_guidance", guidance)

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
