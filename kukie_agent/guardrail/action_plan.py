"""Action Plan — 변경 1건당 1장씩 생기는 구조화된 계획 객체.

팀 결정 (가드레일 v2):
- Action Plan은 훅이 내부적으로 생성·기록한다. LLM이 호출하는 Plan 툴(조회/평가)은
  MVP 제외 — 필요해지면 히스토리 기능과 함께 추가.
- 실행 중에는 ActionPlan 객체가 기준이고, 상태가 바뀔 때마다 원본에 먼저 기록한다.
- Markdown(사람용) = 객체 필드를 표시할 때만 동적으로 조합
- decision_guidance = 승인 전 별도 LLM 검토가 작성하는 판단 보조 필드

원본은 계획이 어디서 생겼는지에 따라 둘 중 하나다.
- **대화 경로 (run_id 있음)**: Kukie API 서버(kukie-server)의 tbl_action_plan (DURO-113). agent 는 DB 가 없으므로
  상태가 바뀔 때마다 내부 API 의 plan 동작(create·dry-run·offer·decision·start·result·fail·unknown)을 부르고,
  **다음 상태는 server 가 정한다** — 허용되지 않는 전이·승인한 호출과 다른 실행은 server 가 409 로 거절한다.
  메모리의 객체는 그 응답으로 맞춘다. id 는 server 가 만든 UUID(승인 카드의 plan_id)이고,
  화면에 보이는 id 는 code(`ap-YYMMDD-HHMM-<tool>[-N]`)다. .md 사본은 만들지 않는다 — 상세 Markdown 은 server 가 그린다.
- **flat 엔드포인트 (run_id 없음, 개발용)**: ~/.kukie/plans 의 .md 파일. frontmatter 가 원본이고,
  전이 규칙도 여기서 server 와 같은 규칙으로 본다 (issue #58 댓글 4번).

상태 (DURO-83 결정, 기획 10 이름):
  DRAFT → WAITING_APPROVAL → APPROVED → EXECUTING → APPLIED → (EFFECT_VERIFIED)
  옆길: REJECTED, EXPIRED(카드 만료), STALE(전제 깨짐 — 기획 08, 아직 없다), FAILED(+failure_reason), UNKNOWN
EFFECT_VERIFIED 로 가는 경로는 아직 없다 (검증 = 기획 09, 다음 이슈).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from re import sub
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from kukie_agent import server_api

logger = logging.getLogger(__name__)

PLAN_DIR = Path.home() / ".kukie" / "plans"
ID_ATTEMPTS = 20        # 이름 충돌을 물러나며 재시도할 횟수

# plan 상태 — server 의 wire 값 그대로 (DURO-83, 기획 10 이름 + REJECTED).
PLAN_OPEN = ("DRAFT", "WAITING_APPROVAL", "APPROVED", "EXECUTING")   # 아직 끝나지 않은 계획
PLAN_CLOSED = ("APPLIED", "EFFECT_VERIFIED", "REJECTED", "EXPIRED", "STALE", "FAILED", "UNKNOWN")
PLAN_STATUSES = PLAN_OPEN + PLAN_CLOSED
# EXPIRED = 중단·재시작으로 승인 카드가 못 쓰게 됐다 (클러스터는 그대로). STALE = 클러스터 상태가 바뀌어 전제가 깨졌다 (기획 08).
PLAN_FAILURE_REASONS = (
    "PERMISSION_DENIED",           # RBAC 거부
    "CONCURRENT_MODIFICATION",     # 다른 사람이 먼저 바꿨다 (기획 08, 아직 판정하지 않는다)
    "DRY_RUN_FAILED",              # 승인 전 예행에서 떨어졌다
    "EXECUTION_FAILED",            # kubectl 이 실패했다
    "VERIFICATION_FAILED",         # 적용은 됐지만 효과 확인이 실패했다 (기획 09)
    "VERIFICATION_TIMEOUT",
)
FINAL_STATUSES = frozenset(PLAN_CLOSED)
DRY_RUN_STATUSES = frozenset({"succeeded", "failed", "unsupported"})
PlanTarget = dict[str, str | list[dict[str, str]]]

#: server 가 "지금 상태에서는 그 동작을 받을 수 없다" 고 답하는 코드. 훅은 이걸 실행 거절(ToolFailed)로 읽는다.
PLAN_CONFLICTS = frozenset({"PLAN_STATE_CONFLICT", "PLAN_CALL_MISMATCH"})


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
    id: str                 # server 계획은 UUID (승인 카드의 plan_id), 파일 계획은 파일 이름과 같은 표시용 id
    path: Path | None       # 파일 계획의 .md. server 계획은 None — 사본을 두지 않는다
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
    run_id: str | None = None                          # 어느 run 의 계획인가. None 이면 server 에 넣지 않는다 (flat 엔드포인트)
    code: str | None = None                            # server 계획의 표시용 id. 파일 계획은 id 가 곧 표시용이라 None

    @property
    def remote(self) -> bool:
        """원본이 server 인가 (대화 경로). False 면 .md 파일이 원본이다."""
        return self.run_id is not None

    @classmethod
    async def create_draft(
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
        if run_id is not None:
            # id·code 는 server 가 정한다 — 이름 예약을 agent 가 할 필요가 없다
            created = await server_api.create_plan(
                run_id,
                tool_call_id=call_id,
                tool_name=tool,
                risk=risk,
                request_hash=compute_request_hash(
                    tool=tool, args=args, command=command, risk=risk, target=target,
                ),
                payload={
                    "created_at": created_at,
                    "args": dict(args),
                    "skill": skill,
                    "target": dict(target),
                    "command": list(command),
                    "intent": intent,
                    "expected_effects": list(expected_effects),
                    "side_effects": list(side_effects),
                },
            )
            return cls.from_response(created)

        timestamp = datetime.fromisoformat(created_at).strftime("%y%m%d-%H%M")
        tool_name = sub(r"[^\w-]+", "-", tool).strip("-") or "tool"
        base_id = f"ap-{timestamp}-{tool_name}"
        # 파일명 원자적 예약: "있나 확인 → 쓰기"로 나누면 확인과 쓰기 사이에 다른 프로세스가
        # 같은 이름을 잡을 수 있다. 확인 없이 exclusive 쓰기를 시도하고, 이미 있으면(FileExistsError)
        # 다음 번호로 재시도한다 — 판단이 "쓰는 순간" OS에서 이뤄져 틈이 없다.
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
            )
            try:
                plan._write(exclusive=True)
            except FileExistsError:   # 누군가 먼저 이 이름을 썼음 → 다음 번호로
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
    def from_response(cls, plan: server_api.Plan) -> "ActionPlan":
        """server 의 PlanResponse 에서 만든다. DB 컬럼이 아닌 필드는 payload 에 통째로 들어 있다."""
        payload = dict(plan.payload or {})
        return cls(
            id=plan.id,
            path=None,
            created_at=payload.get("created_at") or plan.created_at or "",
            call_id=plan.tool_call_id,
            tool=plan.tool_name,
            args=dict(payload.get("args") or {}),
            skill=payload.get("skill", ""),
            target=dict(payload.get("target") or {}),
            command=list(payload.get("command") or []),
            risk_level=plan.risk,
            status=plan.status,
            intent=payload.get("intent", ""),
            expected_effects=list(payload.get("expected_effects") or []),
            side_effects=list(payload.get("side_effects") or []),
            dry_run_result=payload.get("dry_run_result"),
            decision_guidance=payload.get("decision_guidance"),
            decision=plan.decision,
            execution_result=plan.execution_result,
            failure_reason=plan.failure_reason,
            applied_at=plan.applied_at,
            run_id=plan.run_id,
            code=plan.code,
        )

    @classmethod
    async def find_by_call_id(cls, call_id: str, *, run_id: str | None = None) -> "ActionPlan":
        """카드 하나를 찾는다. run 을 알면 server 에서(원본), 모르면 .md 를 훑는다 (flat 엔드포인트·테스트)."""
        if run_id is not None:
            found = await server_api.run_plans(run_id, tool_call_id=call_id)
            if not found:
                raise FileNotFoundError(f"Action Plan not found for call_id={call_id}")
            if len(found) > 1:
                raise ValueError(f"multiple Action Plans found for call_id={call_id}")
            return cls.from_response(found[0])
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
            "id": self.code or self.id,      # 사람이 읽는 id — server 계획은 UUID 대신 code
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

    # ── 원본에 기록 ──────────────────────────────────────

    @property
    def request_hash(self) -> str:
        """승인한 내용과 실행 요청이 같은지 확인하는 해시 (DB 문서 5절 request_hash). 만들 때 server 에 함께 남긴다.
        승인 내용 대조는 server 의 decision 동작이 필드별로 한다 (어느 칸이 달라졌는지 알려주려고)."""
        return compute_request_hash(
            tool=self.tool, args=self.args, command=self.command,
            risk=self.risk_level, target=self.target,
        )

    async def _act(self, action: str, body: dict[str, Any] | None = None) -> None:
        """server 계획: 동작을 알리고 돌아온 plan 으로 이 객체를 맞춘다.

        상태 충돌(409)은 ValueError 로 바꾼다 — 파일 계획의 전이 검사와 같은 예외라 훅이 한 갈래로 받는다.
        server 에 못 닿은 것(503 등)은 그대로 던진다. 어느 쪽이든 이 객체는 바뀌지 않는다.
        """
        try:
            updated = await server_api.plan_action(self.id, action, body)
        except server_api.ServerError as exc:
            if exc.code in PLAN_CONFLICTS:
                raise ValueError(f"ActionPlan {action} rejected ({exc.code}): {exc.message}") from None
            raise
        fresh = type(self).from_response(updated)
        for item in dataclasses.fields(self):
            setattr(self, item.name, getattr(fresh, item.name))

    def _update_fields(self, **changes: object) -> None:
        """파일 계획의 상태를 바꾼다. .md 가 원본이라 쓰지 못하면 객체를 원래대로 되돌리고 던진다."""
        previous = {key: getattr(self, key) for key in changes}
        for key, value in changes.items():
            setattr(self, key, value)
        try:
            self._write()
        except Exception:
            for key, value in previous.items():
                setattr(self, key, value)
            raise

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

    async def record_dry_run(self, status: str, stdout: str, stderr: str) -> None:
        """승인 전 예행 결과. 떨어졌으면(failed) 그 자리에서 FAILED + DRY_RUN_FAILED 로 닫힌다."""
        if status not in DRY_RUN_STATUSES:
            raise ValueError(f"invalid dry-run status: {status}")
        if self.remote:
            await self._act("dry-run", {"status": status, "stdout": stdout, "stderr": stderr})
            return
        if self.status != "DRAFT" or self.dry_run_result is not None:
            raise ValueError("ActionPlan is not ready for dry-run")
        changes: dict[str, object] = {
            "dry_run_result": {
                "status": status,
                "stdout": stdout,
                "stderr": stderr,
                "at": _utc_now(),
            }
        }
        if status == "failed":
            changes.update(status="FAILED", failure_reason="DRY_RUN_FAILED")
        self._update_fields(**changes)

    async def offer(self, guidance: str) -> None:
        """판단 가이드를 남기고 승인 카드로 내보낸다 — DRAFT → WAITING_APPROVAL (DURO-83)."""
        text = guidance.strip()
        if not text:
            raise ValueError("decision guidance must not be empty")
        if self.remote:
            await self._act("offer", {"decisionGuidance": text})
            return
        dry_run = self.dry_run_result
        if self.decision_guidance is not None:
            raise ValueError("decision guidance already exists")
        if (
            self.status != "DRAFT"
            or not isinstance(dry_run, dict)
            or dry_run.get("status") not in {"succeeded", "unsupported"}
        ):
            raise ValueError("ActionPlan is not ready for approval")
        self._update_fields(decision_guidance=text, status="WAITING_APPROVAL")

    def _record_decision(self, *, approved: bool, user_id: str | None) -> None:
        self._update_fields(
            decision={"approved": approved, "user_id": user_id, "at": _utc_now()},
            status="APPROVED" if approved else "REJECTED",
        )

    async def approve_for_execution(
        self,
        *,
        tool: str,
        args: dict[str, object],
        command: list[str],
        risk: str,
        target: PlanTarget,
        user_id: str | None = None,
    ) -> None:
        """승인을 남긴다. 승인한 카드와 지금 실행하려는 호출이 같아야 한다 — 다르면 ValueError (실행하지 않는다)."""
        current = {
            "tool": tool,
            "args": args,
            "command": command,
            "risk": risk,
            "target": target,
        }
        if self.remote:
            # 비교는 server 가 한다 (다르면 409 PLAN_CALL_MISMATCH — 다른 칸 이름이 message 에 온다)
            await self._act("decision", {"approved": True, "userId": user_id, "call": current})
            return
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
        self._record_decision(approved=True, user_id=user_id)

    async def mark_executing(self) -> None:
        """kubectl 을 부르기 직전 — APPROVED → EXECUTING. 여기서 죽으면 실행 여부를 모른다 (UNKNOWN)."""
        if self.remote:
            await self._act("start")
            return
        if self.status != "APPROVED":
            raise ValueError("ActionPlan is not ready to execute")
        self._update_fields(status="EXECUTING")

    async def record_execution(
        self,
        *,
        success: bool,
        stdout: str,
        stderr: str,
        exit_code: int | None,
    ) -> None:
        """실행 결과. 성공이고 exit 0 이면 APPLIED, 아니면 FAILED + EXECUTION_FAILED."""
        if self.remote:
            await self._act("result", {
                "success": success,
                "stdout": stdout,
                "stderr": stderr,
                "exitCode": exit_code,
            })
            return
        if (
            self.status not in {"APPROVED", "EXECUTING"}
            or self.decision is None
            or self.execution_result is not None
        ):
            raise ValueError("ActionPlan is not ready to record execution")
        at = _utc_now()
        applied = success and exit_code == 0
        self._update_fields(
            execution_result={
                "success": success,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "at": at,
            },
            # 적용 시각은 성공했을 때만 남긴다. 검증(기획 09)이 나중에 실패해 FAILED 로 가도 이 값은 지우지 않는다 (DURO-83)
            applied_at=at if applied else self.applied_at,
            failure_reason=None if applied else "EXECUTION_FAILED",
            status="APPLIED" if applied else "FAILED",
        )

    async def reject(self, user_id: str | None = None) -> None:
        """거절을 기록한다. **다시 불러도 성공한다** — 안내가 "같은 카드를 다시 결정하라" 이기 때문이다.

        기록은 성공했는데 응답이 끊겨 실패로 올라오는 경우가 있다. 그때 사용자가 시킨 대로 다시 거절하면
        상태 검사에 걸려 또 실패하고 그 카드는 영영 결정할 수 없다. 이미 거절로 끝났으면 조용히 성공한다
        (server 의 decision 동작도 같은 규칙이다 — 자동 리뷰 지적).
        """
        if self.remote:
            await self._act("decision", {"approved": False, "userId": user_id})
            return
        if self.status == "REJECTED" and self.decision is not None and not self.decision.get("approved"):
            return
        if (
            self.status not in {"DRAFT", "WAITING_APPROVAL"}
            or self.decision is not None
            or self.execution_result is not None
        ):
            raise ValueError("ActionPlan is not ready for rejection")
        self._record_decision(approved=False, user_id=user_id)

    async def mark_failed(self, reason: str) -> None:
        """FAILED 는 이유를 함께 남긴다 (DURO-83: "세부 실패 사유를 분리"). 열린 계획만."""
        if reason not in PLAN_FAILURE_REASONS:
            raise ValueError(f"invalid Action Plan failure reason: {reason}")
        if self.remote:
            await self._act("fail", {"reason": reason})
            return
        if self.status not in PLAN_OPEN:
            raise ValueError("ActionPlan is already closed")
        self._update_fields(status="FAILED", failure_reason=reason)

    async def mark_unknown(self) -> None:
        """실행 여부를 모른다 — 승인은 남았는데 결과가 없다 (run 의 recovery_required 와 같은 뜻)."""
        if self.remote:
            await self._act("unknown")
            return
        if self.status not in {"APPROVED", "EXECUTING"} or self.execution_result is not None:
            raise ValueError("ActionPlan is not waiting for an execution result")
        self._update_fields(status="UNKNOWN")
