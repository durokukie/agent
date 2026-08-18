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
            "decision_guidance": None,
            "approval": None,
            "execution_result": None,
        }
        body = (
            f"# Intent\n\n{intent}\n\n"
            f"## Expected Effects\n\n{_bullets(expected_effects)}\n\n"
            f"## Side Effects\n\n{_bullets(side_effects)}\n"
        )
        # 파일명 원자적 예약: "있나 확인 → 쓰기"로 나누면 확인과 쓰기 사이에 다른 프로세스가
        # 같은 이름을 잡을 수 있다. 확인 없이 exclusive 쓰기를 시도하고, 이미 있으면(FileExistsError)
        # 다음 번호로 재시도한다 — 판단이 "쓰는 순간" OS에서 이뤄져 틈이 없다.
        suffix = 1
        while True:
            plan_id = base_id if suffix == 1 else f"{base_id}-{suffix}"
            plan = cls(plan_id, PLAN_DIR / f"{plan_id}.md")
            metadata["id"] = plan_id
            try:
                plan._write(metadata, body, exclusive=True)
            except FileExistsError:   # 누군가 먼저 이 이름을 썼음 → -2, -3 …
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
        metadata = yaml.safe_load(frontmatter)
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid Action Plan frontmatter: {self.path}")
        return metadata, body

    def _update(self, key: str, value: object) -> None:
        metadata, body = self._read()
        metadata[key] = value
        self._write(metadata, body)

    def guidance_context(self) -> str:
        metadata, body = self._read()
        required = (
            "id",
            "created_at",
            "tool",
            "skill",
            "target",
            "command",
            "risk_level",
        )
        missing = [key for key in required if not metadata.get(key)]

        prefix = "# Intent\n\n"
        effects_marker = "\n\n## Expected Effects\n\n"
        side_effects_marker = "\n\n## Side Effects\n\n"
        try:
            if not body.startswith(prefix):
                raise ValueError
            intent, rest = body.removeprefix(prefix).split(effects_marker, 1)
            expected_effects, side_effects = rest.split(side_effects_marker, 1)
        except ValueError:
            missing.extend(("intent", "expected_effects", "side_effects"))
        else:
            sections = {
                "intent": intent,
                "expected_effects": expected_effects.strip().strip("-").strip(),
                "side_effects": side_effects.strip().strip("-").strip(),
            }
            missing.extend(name for name, value in sections.items() if not value)

        if missing:
            raise ValueError(
                f"ActionPlan is not ready for guidance: missing={missing}"
            )
        if metadata.get("status") != "draft":
            raise ValueError("ActionPlan is not ready for guidance: status must be draft")

        dry_run = metadata.get("dry_run_result")
        if not isinstance(dry_run, dict) or dry_run.get("success") is not True:
            raise ValueError(
                "ActionPlan is not ready for guidance: "
                "dry_run_result.success must be true"
            )
        if metadata.get("approval") is not None:
            raise ValueError("ActionPlan is not ready for guidance: approval must be empty")
        if metadata.get("execution_result") is not None:
            raise ValueError(
                "ActionPlan is not ready for guidance: execution_result must be empty"
            )
        if metadata.get("decision_guidance") is not None:
            raise ValueError(
                "ActionPlan is not ready for guidance: decision guidance already exists"
            )

        context = dict(metadata)
        context.pop("decision_guidance", None)
        return (
            "---\n"
            f"{yaml.safe_dump(context, sort_keys=False, allow_unicode=True)}"
            "---\n"
            f"{body}"
        )

    def record_decision_guidance(self, text: str) -> None:
        guidance = text.strip()
        if not guidance:
            raise ValueError("decision guidance must not be empty")

        metadata, body = self._read()
        if metadata.get("decision_guidance") is not None:
            raise ValueError("decision guidance already exists")

        metadata["decision_guidance"] = guidance
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
