"""Action Plan — 변경 1건당 1장씩 생기는 구조화된 계획 객체와 .md 기록.

팀 결정 (가드레일 v2):
- Action Plan은 훅이 내부적으로 생성·기록한다. LLM이 호출하는 Plan 툴(조회/평가)은
  MVP 제외 — 필요해지면 히스토리 기능과 함께 추가.
- 실행 중에는 ActionPlan 객체가 기준이고, 변경할 때마다 .md 스냅샷을 갱신한다.
- frontmatter(기계용) = 모든 필드의 단일 저장 원본
- Markdown(사람용) = 객체 필드를 표시할 때만 동적으로 조합
- decision_guidance = 승인 전 별도 LLM 검토가 작성하는 판단 보조 필드

원본은 DB 표 tbl_action_plan 이다 (#58, DB 문서 5절). .md 는 사람이 읽는 사본으로 계속 쓴다 —
DB 를 먼저 쓰고 파일은 뒤이어 갱신하며, 파일 쓰기 실패는 로그만 남기고 넘어간다.

run 에 속하지 않는 계획(flat /chat, /approve)은 DB 에 넣을 자리가 없으므로 파일로만 남는다.
run_id 를 받은 계획만 표에 들어간다 — issue #58 댓글 4번.

상태 (DURO-83 결정, 기획 10 이름):
  DRAFT → WAITING_APPROVAL → APPROVED → EXECUTING → APPLIED → (EFFECT_VERIFIED)
  옆길: REJECTED, STALE, FAILED(+failure_reason), UNKNOWN
EFFECT_VERIFIED 로 가는 경로는 아직 없다 (검증 = 기획 09, 다음 이슈).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from re import compile as _compile, sub
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from sqlalchemy.exc import IntegrityError

from kukie.store.models import PLAN_CLOSED, PLAN_FAILURE_REASONS, PLAN_STATUSES

logger = logging.getLogger(__name__)

PLAN_DIR = Path.home() / ".kukie" / "plans"
ID_ATTEMPTS = 20        # 이름 충돌을 물러나며 재시도할 횟수
FINAL_STATUSES = frozenset(PLAN_CLOSED)
DRY_RUN_STATUSES = frozenset({"succeeded", "failed", "unsupported"})
PlanTarget = dict[str, str | list[dict[str, str]]]

# .md frontmatter 중 DB 컬럼이 아닌 것들 — 통째로 plan_payload JSON 에 들어간다 (DB 문서 5절)
_PAYLOAD_FIELDS = (
    "created_at", "args", "skill", "target", "command", "intent",
    "expected_effects", "side_effects", "dry_run_result", "decision_guidance",
)


def _store():
    """DB 저장소. import 시점에 홈 디렉터리를 만들지 않도록 부를 때 가져온다."""
    from kukie.store import get_store

    return get_store()


def compute_request_hash(*, tool: str, args: dict[str, object], command: list[str], risk: str, target: PlanTarget) -> str:
    """승인한 내용과 실행 요청이 같은지 확인하는 해시 (DB 문서 5절 request_hash)."""
    canonical = json.dumps(
        {"tool": tool, "args": args, "command": command, "risk": risk, "target": target},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


@dataclass
class ActionPlan:
    id: str
    path: Path
    created_at: str
    call_id: str
    tool: str
    args: dict[str, object]
    skill: str
    target: PlanTarget
    command: list[str]
    risk_level: str
    status: str
    intent: str
    expected_effects: list[str]
    side_effects: list[str]
    dry_run_result: dict[str, object] | None = None
    decision_guidance: str | None = None
    decision: dict[str, object] | None = None          # {approved, user_id, at}. 결정 전 None (DB 문서 5절)
    execution_result: dict[str, object] | None = None
    failure_reason: str | None = None                  # PLAN_FAILURE_REASONS. FAILED 일 때만
    applied_at: str | None = None                      # kubectl 이 실제로 적용된 시각 — 검증이 실패해도 남는다 (DURO-83)
    run_id: str | None = None                          # 어느 run 의 계획인가. None 이면 DB 에 넣지 않는다 (flat 엔드포인트)

    @classmethod
    def create_draft(
        cls,
        *,
        call_id: str,
        tool: str,
        args: dict[str, object],
        command: list[str],
        risk: str,
        skill: str,
        target: PlanTarget,
        intent: str,
        expected_effects: list[str],
        side_effects: list[str],
        run_id: str | None = None,
    ) -> "ActionPlan":
        if not isinstance(args, dict):
            raise TypeError("args must be a dict")
        created_at = _utc_now()
        timestamp = datetime.fromisoformat(created_at).strftime("%y%m%d-%H%M")
        tool_name = sub(r"[^\w-]+", "-", tool).strip("-") or "tool"
        base_id = f"ap-{timestamp}-{tool_name}"
        # 파일명 원자적 예약: "있나 확인 → 쓰기"로 나누면 확인과 쓰기 사이에 다른 프로세스가
        # 같은 이름을 잡을 수 있다. 확인 없이 exclusive 쓰기를 시도하고, 이미 있으면(FileExistsError)
        # 다음 번호로 재시도한다 — 판단이 "쓰는 순간" OS에서 이뤄져 틈이 없다.
        # 같은 이름이 있으면 -2, -3 … 으로 물러난다. 이름과 무관한 충돌(같은 run 에 같은 tool_call_id)이면
        # 번호만 늘리며 영원히 돌게 되므로 몇 번만 시도하고 던진다.
        for suffix in range(1, ID_ATTEMPTS + 1):
            plan_id = base_id if suffix == 1 else f"{base_id}-{suffix}"
            plan = cls(
                id=plan_id,
                path=PLAN_DIR / f"{plan_id}.md",
                created_at=created_at,
                call_id=call_id,
                tool=tool,
                args=dict(args),
                skill=skill,
                target=dict(target),
                command=list(command),
                risk_level=risk,
                status="DRAFT",
                intent=intent,
                expected_effects=list(expected_effects),
                side_effects=list(side_effects),
                run_id=run_id,
            )
            try:
                plan._write(exclusive=True)
            except FileExistsError:   # 누군가 먼저 이 이름을 썼음 → 다음 번호로
                continue
            try:
                plan._insert()
            except IntegrityError:
                # 이름 예약은 파일이 하지만 원본은 표다 — 표에서 부딪히면 파일도 물리고 다음 번호로
                # (자동 리뷰 지적: _insert 가 루프 밖이라 재시도되지 않았다)
                plan.path.unlink(missing_ok=True)
                continue
            return plan
        raise RuntimeError(f"Action Plan id 를 확보하지 못했다: {base_id}")

    @classmethod
    def load(cls, path: Path) -> "ActionPlan":
        path = Path(path)
        metadata, _ = cls._read_path(path)
        try:
            intent = metadata["intent"]
            expected_effects = metadata["expected_effects"]
            side_effects = metadata["side_effects"]
            args = metadata["args"]
            if (
                not isinstance(args, dict)
                or not isinstance(intent, str)
                or not isinstance(expected_effects, list)
                or not all(isinstance(item, str) for item in expected_effects)
                or not isinstance(side_effects, list)
                or not all(isinstance(item, str) for item in side_effects)
            ):
                raise TypeError
            return cls(
                id=metadata["id"],
                path=path,
                created_at=metadata["created_at"],
                call_id=metadata["call_id"],
                tool=metadata["tool"],
                args=dict(args),
                skill=metadata["skill"],
                target=dict(metadata["target"]),
                command=list(metadata["command"]),
                risk_level=metadata["risk_level"],
                status=metadata["status"],
                intent=intent,
                expected_effects=expected_effects,
                side_effects=side_effects,
                dry_run_result=metadata.get("dry_run_result"),
                decision_guidance=metadata.get("decision_guidance"),
                decision=metadata.get("decision"),
                execution_result=metadata.get("execution_result"),
                failure_reason=metadata.get("failure_reason"),
                applied_at=metadata.get("applied_at"),
                run_id=metadata.get("run_id"),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid Action Plan frontmatter: {path}") from exc

    @classmethod
    def from_row(cls, row: Any) -> "ActionPlan":
        """DB 행(PlanRow)에서 되살린다 — 서버가 다시 떠서 .md 가 없거나 낡아도 이쪽이 원본이다."""
        payload = dict(row.plan_payload or {})
        return cls(
            id=row.id,
            path=PLAN_DIR / f"{row.id}.md",
            created_at=payload.get("created_at", row.created_at.isoformat()),
            call_id=row.tool_call_id,
            tool=row.tool_name,
            args=dict(payload.get("args") or {}),
            skill=payload.get("skill", ""),
            target=dict(payload.get("target") or {}),
            command=list(payload.get("command") or []),
            risk_level=row.risk,
            status=row.status,
            intent=payload.get("intent", ""),
            expected_effects=list(payload.get("expected_effects") or []),
            side_effects=list(payload.get("side_effects") or []),
            dry_run_result=payload.get("dry_run_result"),
            decision_guidance=payload.get("decision_guidance"),
            decision=row.decision,
            execution_result=row.execution_result,
            failure_reason=row.failure_reason,
            applied_at=row.applied_at.isoformat() if row.applied_at else None,
            run_id=row.run_id,
        )

    @classmethod
    def find_by_call_id(cls, call_id: str, *, run_id: str | None = None) -> "ActionPlan":
        """카드 하나를 찾는다. run 을 알면 DB 에서(원본), 모르면 .md 를 훑는다 (flat 엔드포인트·테스트)."""
        if run_id is not None:
            row = _store().find_plan_by_call_id(call_id, run_id=run_id)
            if row is not None:
                return cls.from_row(row)
        # ponytail: 로컬 MVP에서는 선형 탐색이면 충분하다. 실제 병목일 때만 인덱스를 추가한다.
        matches = [
            path
            for path in PLAN_DIR.glob("*.md")
            if cls._read_path(path)[0].get("call_id") == call_id
        ]
        if not matches:
            raise FileNotFoundError(f"Action Plan not found for call_id={call_id}")
        if len(matches) > 1:
            raise ValueError(f"multiple Action Plans found for call_id={call_id}")
        return cls.load(matches[0])

    def _metadata(self) -> dict[str, object]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "call_id": self.call_id,
            "tool": self.tool,
            "args": self.args,
            "skill": self.skill,
            "target": self.target,
            "command": self.command,
            "risk_level": self.risk_level,
            "status": self.status,
            "intent": self.intent,
            "expected_effects": self.expected_effects,
            "side_effects": self.side_effects,
            "dry_run_result": self.dry_run_result,
            "decision_guidance": self.decision_guidance,
            "decision": self.decision,
            "execution_result": self.execution_result,
            "failure_reason": self.failure_reason,
            "applied_at": self.applied_at,
            "run_id": self.run_id,
        }

    def _serialize(self) -> str:
        return (
            "---\n"
            f"{yaml.safe_dump(self._metadata(), sort_keys=False, allow_unicode=True)}"
            "---\n"
        )

    def _body(self) -> str:
        return (
            f"# Intent\n\n{self.intent}\n\n"
            f"## Expected Effects\n\n{_bullets(self.expected_effects)}\n\n"
            f"## Side Effects\n\n{_bullets(self.side_effects)}\n"
        )

    def render_markdown(self, *, include_decision_guidance: bool = True) -> str:
        metadata = self._metadata()
        for field in ("intent", "expected_effects", "side_effects"):
            metadata.pop(field)
        if not include_decision_guidance:
            metadata.pop("decision_guidance")
        details = yaml.safe_dump(
            metadata, sort_keys=False, allow_unicode=True
        ).rstrip()
        return f"# Action Plan\n\n```yaml\n{details}\n```\n\n{self._body()}"

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
                temp.write(self._serialize())
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

    @staticmethod
    def _read_path(path: Path) -> tuple[dict, str]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"invalid Action Plan frontmatter: {path}")
        frontmatter, separator, body = text[4:].partition("\n---\n")
        if not separator:
            raise ValueError(f"invalid Action Plan frontmatter: {path}")
        try:
            metadata = yaml.safe_load(frontmatter)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid Action Plan frontmatter: {path}") from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid Action Plan frontmatter: {path}")
        return metadata, body

    def _read(self) -> tuple[dict, str]:
        return self._read_path(self.path)

    # ── DB (원본) ─────────────────────────────────────────

    def _payload(self) -> dict[str, Any]:
        """DB 컬럼이 아닌 필드는 통째로 plan_payload JSON 에 (DB 문서 5절)."""
        return {field: getattr(self, field) for field in _PAYLOAD_FIELDS}

    @property
    def request_hash(self) -> str:
        """DB 문서 5절의 request_hash. 지금은 기록만 하고 비교에는 쓰지 않는다 —
        승인 내용 대조는 approve_for_execution 의 필드별 비교가 한다 (어느 칸이 달라졌는지 알려주려고)."""
        return compute_request_hash(
            tool=self.tool, args=self.args, command=self.command,
            risk=self.risk_level, target=self.target,
        )

    def _insert(self) -> None:
        if self.run_id is None:      # run 밖의 계획(flat 엔드포인트)은 파일이 원본이다
            return
        _store().create_plan(
            plan_id=self.id, run_id=self.run_id, tool_call_id=self.call_id, tool_name=self.tool,
            risk=self.risk_level, plan_payload=self._payload(), request_hash=self.request_hash,
            status=self.status,
        )

    def _sync(self) -> None:
        if self.run_id is None:
            return
        _store().update_plan(
            self.id,
            status=self.status,
            plan_payload=self._payload(),
            decision=self.decision,
            execution_result=self.execution_result,
            failure_reason=self.failure_reason,
            applied_at=datetime.fromisoformat(self.applied_at) if self.applied_at else None,
        )

    def _update_fields(self, **changes: object) -> None:
        """상태를 바꾼다. run 에 속한 계획은 DB 가 원본이라 그걸 먼저 쓰고, .md 는 뒤이어 갱신한다.

        DB 를 쓰지 못하면 객체를 원래대로 되돌리고 던진다. .md 만 실패하면 로그만 남긴다 —
        사람이 읽는 사본이 낡을 뿐이고 판단은 DB 로 한다. run 밖의 계획은 파일이 원본이라 그대로 던진다.
        """
        previous = {key: getattr(self, key) for key in changes}
        for key, value in changes.items():
            setattr(self, key, value)
        try:
            self._sync()
        except Exception:
            for key, value in previous.items():
                setattr(self, key, value)
            raise
        try:
            self._write()
        except Exception:
            if self.run_id is None:
                for key, value in previous.items():
                    setattr(self, key, value)
                raise
            logger.exception("Action Plan .md 갱신 실패 — DB 기록은 남았다 (plan_id=%s)", self.id)

    def validate_for_decision_guidance(self) -> None:
        if not isinstance(self.args, dict):
            raise ValueError("ActionPlan is not ready for guidance: missing=args")
        required = (
            "id",
            "created_at",
            "call_id",
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
        if self.status != "DRAFT":
            raise ValueError("ActionPlan is not ready for guidance: status must be DRAFT")

        dry_run = self.dry_run_result
        if not isinstance(dry_run, dict) or dry_run.get("status") != "succeeded":
            raise ValueError(
                "ActionPlan is not ready for guidance: "
                "dry_run_result.status must be succeeded"
            )
        if self.decision is not None:
            raise ValueError("ActionPlan is not ready for guidance: decision must be empty")
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
        self._update_fields(decision_guidance=guidance)

    def record_dry_run(self, status: str, stdout: str, stderr: str) -> None:
        if status not in DRY_RUN_STATUSES:
            raise ValueError(f"invalid dry-run status: {status}")
        self._update_fields(
            dry_run_result={
                "status": status,
                "stdout": stdout,
                "stderr": stderr,
                "at": _utc_now(),
            }
        )

    def offer_for_approval(self) -> None:
        """승인 카드로 사용자에게 나간다 — DRAFT → WAITING_APPROVAL (DURO-83)."""
        if self.status != "DRAFT":
            raise ValueError("ActionPlan is not ready for approval")
        self._update_fields(status="WAITING_APPROVAL")

    def record_decision(self, *, approved: bool, user_id: str | None = None) -> None:
        """사용자의 결정을 남긴다 — 승인이면 APPROVED, 거절이면 REJECTED (DB 문서 5절 decision 칸).

        검사는 부르는 쪽(approve_for_execution / reject)이 한다.
        """
        self._update_fields(
            decision={"approved": approved, "user_id": user_id, "at": _utc_now()},
            status="APPROVED" if approved else "REJECTED",
        )

    def approve_for_execution(
        self,
        *,
        tool: str,
        args: dict[str, object],
        command: list[str],
        risk: str,
        target: PlanTarget,
        user_id: str | None = None,
    ) -> None:
        dry_run = self.dry_run_result
        if (
            self.status not in {"DRAFT", "WAITING_APPROVAL"}
            or not isinstance(dry_run, dict)
            or dry_run.get("status") not in {"succeeded", "unsupported"}
            or not self.decision_guidance
            or self.decision is not None
            or self.execution_result is not None
        ):
            raise ValueError("ActionPlan is not ready for execution")

        current = {
            "tool": tool,
            "args": args,
            "command": command,
            "risk": risk,
            "target": target,
        }
        approved = {
            "tool": self.tool,
            "args": self.args,
            "command": self.command,
            "risk": self.risk_level,
            "target": self.target,
        }
        for field, value in current.items():
            if value != approved[field]:
                raise ValueError(f"approved request mismatch: {field}")

        self.record_decision(approved=True, user_id=user_id)

    def mark_executing(self) -> None:
        """kubectl 을 부르기 직전 — APPROVED → EXECUTING. 여기서 죽으면 실행 여부를 모른다 (UNKNOWN)."""
        if self.status != "APPROVED":
            raise ValueError("ActionPlan is not ready to execute")
        self._update_fields(status="EXECUTING")

    def record_execution(
        self,
        *,
        success: bool,
        stdout: str,
        stderr: str,
        exit_code: int | None,
    ) -> None:
        if (
            self.status not in {"APPROVED", "EXECUTING"}
            or self.decision is None
            or self.execution_result is not None
        ):
            raise ValueError("ActionPlan is not ready to record execution")
        at = _utc_now()
        self._update_fields(
            execution_result={
                "success": success,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "at": at,
            },
            # 적용 시각은 성공했을 때만 남긴다. 검증(기획 09)이 나중에 실패해 FAILED 로 가도 이 값은 지우지 않는다 (DURO-83)
            applied_at=at if success else self.applied_at,
            failure_reason=None if success else "EXECUTION_FAILED",
            status="APPLIED" if success else "FAILED",
        )

    def reject(self, user_id: str | None = None) -> None:
        """거절을 기록한다. **다시 불러도 성공한다** — 안내가 "같은 카드를 다시 결정하라" 이기 때문이다.

        DB commit 은 성공했는데 응답이 끊겨 실패로 올라오는 경우가 있다. 그때 사용자가 시킨 대로
        다시 거절하면 예전에는 상태 검사에 걸려 또 503 이 나고 그 카드는 영영 결정할 수 없었다.
        이미 거절로 끝난 계획이면 조용히 성공으로 돌려준다 (자동 리뷰 지적).
        """
        if self.status == "REJECTED" and self.decision is not None and not self.decision.get("approved"):
            # 이 경로가 생기는 상황은 "DB 는 됐는데 응답이 끊겼다" 이고, 그때 _write 는 한 번도 안 돌았다 —
            # .md 가 결정 전 카드로 남아 있다. 사본을 따라오게 한다 (자동 리뷰 지적). 실패는 로그만.
            try:
                self._write()
            except Exception:
                logger.exception("거절 재시도에서 .md 갱신 실패 (plan_id=%s)", self.id)
            return
        if (
            self.status not in {"DRAFT", "WAITING_APPROVAL"}
            or self.decision is not None
            or self.execution_result is not None
        ):
            raise ValueError("ActionPlan is not ready for rejection")
        self.record_decision(approved=False, user_id=user_id)

    def mark_failed(self, reason: str) -> None:
        """FAILED 는 이유를 함께 남긴다 (DURO-83: "세부 실패 사유를 분리")."""
        if reason not in PLAN_FAILURE_REASONS:
            raise ValueError(f"invalid Action Plan failure reason: {reason}")
        self._update_fields(status="FAILED", failure_reason=reason)

    def mark_unknown(self) -> None:
        """실행 여부를 모른다 — 승인은 남았는데 결과가 없다 (run 의 recovery_required 와 같은 뜻)."""
        self._update_fields(status="UNKNOWN")

    def mark(self, status: str, *, failure_reason: str | None = None) -> None:
        if status not in PLAN_STATUSES:
            raise ValueError(f"invalid Action Plan status: {status}")
        if status == "FAILED" and failure_reason is None:
            raise ValueError("FAILED requires a failure_reason")
        self._update_fields(status=status, failure_reason=failure_reason)

_PLAN_ID = _compile(r"^[A-Za-z0-9._-]{1,64}$")


def list_plans(
    user_id: str, *, cluster_id: str | None = None, status: str | None = None,
) -> list[Any]:
    """GET /action-plans 와 히스토리 스킬이 같이 쓰는 목록 — 내 방 + shared 방의 계획 요약.

    본문(.md)은 읽지 않는다. 한 줄에 필요한 값은 전부 DB 표에 있다.
    """
    if status is not None and status not in PLAN_STATUSES:
        raise ValueError(f"invalid Action Plan status: {status}")
    return _store().list_plan_summaries(user_id, cluster_id=cluster_id, status=status)


def get_plan(plan_id: str) -> str:
    """Plan 전문(사람이 읽는 Markdown). DB 행이 있으면 거기서 다시 그리고, 없으면 .md 파일을 읽는다."""
    if not _PLAN_ID.match(plan_id):
        raise ValueError(f"invalid Action Plan id: {plan_id}")
    row = _store().get_plan(plan_id)
    if row is not None:
        return ActionPlan.from_row(row).render_markdown()
    path = PLAN_DIR / f"{plan_id}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Action Plan not found: {plan_id}")
    return ActionPlan.load(path).render_markdown()
