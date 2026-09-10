"""채팅방·run 읽기/쓰기. 라우터와 세션 레지스트리는 이 함수들만 쓴다.

멱등성 (DB 문서 4절): 같은 (session_id, request_id) 로 다시 오면 기존 run 을 돌려주고,
같은 id 에 다른 입력이면 거절한다. 채팅방 하나에 활성 run(running / awaiting_approval /
recovery_required)은 최대 하나 — 활성 run 이 있으면 새 run 을 만들지 않는다.

전제: 단일 프로세스. 검사 → INSERT 사이의 경합은 유니크 제약이 잡고 IntegrityError 를 재조회로 바꾼다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kukie.store.models import PLAN_OPEN, RUN_ACTIVE, ActionPlan, ChatRun, ChatSession, Cluster

_UNSET: Any = object()


class RequestMismatch(ValueError):
    """같은 request_id 로 다른 내용을 보냈다."""


class ActiveRunExists(RuntimeError):
    """이 채팅방에 아직 끝나지 않은 run 이 있다."""


class PlanMissing(LookupError):
    """바꾸려는 계획 행이 표에 없다. 계획은 표가 원본이라 조용히 넘어가면 안 된다."""


class ClusterInUse(RuntimeError):
    """이 클러스터를 쓰는 채팅방이 남아 있어 지울 수 없다."""


def _visible_sessions(user_id: str, team_ids: list[str] | None) -> Any:
    """내가 볼 수 있는 방을 고르는 조건 한 벌 — 방 목록·계획 목록이 같은 규칙을 써야 한다.

    _load(conversations_api) 와 같은 규칙이어야 "목록에 있는데 열 수 없는 것" 이 안 생긴다.
      private 방 → 내 것이면 보인다 (자기 기록이라 읽기는 열어 뒀다)
      shared 방  → 팀이 없거나 내가 그 팀 구성원일 때만. **내가 만든 방도 마찬가지**다
    team_ids 를 안 주면(개발 모드 = 회원 서버 없음) 예전처럼 shared 전부.
    """
    if team_ids is None:
        return or_(ChatSession.user_id == user_id, ChatSession.shared.is_(True))
    my_private = (ChatSession.user_id == user_id) & ChatSession.shared.is_(False)
    # `""` 도 "팀 없음" 으로 본다 — _load 는 falsy 라 팀 검사를 건너뛰므로, 여기서 IN 에만 맡기면
    # 목록에선 사라지는데 상세는 200 인 방이 남는다 (자동 리뷰 지적). 새로 들어오는 값은
    # ConversationIn 이 None 으로 접지만 이미 저장된 행이 있을 수 있다.
    no_team = or_(ChatSession.team_id.is_(None), ChatSession.team_id == "")
    open_shared = ChatSession.shared.is_(True) & or_(no_team, ChatSession.team_id.in_(team_ids))
    return or_(my_private, open_shared)


@dataclass(frozen=True)
class PlanScope:
    """계획이 속한 방의 권한 정보 — plans_api 가 _load 와 같은 규칙을 걸 때 쓴다."""

    owner_id: str
    shared: bool
    team_id: str | None


@dataclass(frozen=True)
class SessionRow:
    id: str
    user_id: str
    team_id: str | None
    cluster_id: str | None
    title: str
    current_mode: str
    installation_id: str | None
    context_name: str
    namespace: str
    cluster_fingerprint: str | None
    shared: bool
    version: int
    created_at: datetime
    updated_at: datetime
    running: bool                      # 지금 실행 중(running)인 run 이 있나 — 승인 대기는 아니다

    @classmethod
    def of(cls, row: ChatSession, *, running: bool) -> "SessionRow":
        return cls(
            id=row.id, user_id=row.user_id, team_id=row.team_id, cluster_id=row.cluster_id,
            title=row.title, current_mode=row.current_mode, installation_id=row.installation_id,
            context_name=row.context_name, namespace=row.namespace,
            cluster_fingerprint=row.cluster_fingerprint, shared=row.shared, version=row.version,
            created_at=row.created_at, updated_at=row.updated_at, running=running,
        )


@dataclass(frozen=True)
class RunRow:
    id: str
    session_id: str
    request_id: str
    turn_no: int
    kind: str
    mode: str
    status: str
    input_text: str | None
    response_payload: dict[str, Any] | None    # 화면에 그릴 payload. 실패면 None 이고 error 에 있다
    error: dict[str, Any] | None               # response_payload 가 {"kind": "error"} 일 때 그 내용
    agent_messages: list[Any] | None
    usage_summary: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, row: ChatRun) -> "RunRow":
        payload, error = row.response_payload, None
        if payload is not None and payload.get("kind") == "error":
            error = {k: v for k, v in payload.items() if k != "kind"}
            payload = None
        return cls(
            id=row.id, session_id=row.session_id, request_id=row.request_id, turn_no=row.turn_no,
            kind=row.kind, mode=row.mode, status=row.status, input_text=row.input_text,
            response_payload=payload, error=error, agent_messages=row.agent_messages,
            usage_summary=row.usage_summary, started_at=row.started_at, finished_at=row.finished_at,
        )

    @property
    def active(self) -> bool:
        return self.status in RUN_ACTIVE


@dataclass(frozen=True)
class Interrupted:
    """중단으로 닫은 run 과, **같은 트랜잭션에서** 함께 닫힌 계획.

    계획까지 돌려주는 것은 부르는 쪽이 `.md` 사본을 따라오게 해야 하기 때문이다. 방금 닫힌 행만
    주므로 이미 끝난 계획(APPLIED 등)의 사본을 덧쓰지 않는다 (자동 리뷰 지적).
    """

    runs: list["RunRow"]
    plans: list["PlanRow"]


@dataclass(frozen=True)
class PlanRow:
    """tbl_action_plan 한 행 (DB 문서 5절). 상태 이름은 DURO-83 결정(대문자)."""

    id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    status: str
    risk: str
    plan_payload: dict[str, Any]
    request_hash: str
    decision: dict[str, Any] | None
    execution_result: dict[str, Any] | None
    failure_reason: str | None
    applied_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: ActionPlan) -> "PlanRow":
        return cls(
            id=row.id, run_id=row.run_id, tool_call_id=row.tool_call_id, tool_name=row.tool_name,
            status=row.status, risk=row.risk, plan_payload=row.plan_payload, request_hash=row.request_hash,
            decision=row.decision, execution_result=row.execution_result, failure_reason=row.failure_reason,
            applied_at=row.applied_at, created_at=row.created_at, updated_at=row.updated_at,
        )

    @property
    def open(self) -> bool:
        return self.status in PLAN_OPEN

    @property
    def approved(self) -> bool:
        return bool(self.decision and self.decision.get("approved"))


@dataclass(frozen=True)
class PlanSummaryRow:
    """GET /action-plans 한 줄 — 앱 api/types.ts 의 ActionPlanSummary (대시보드용)."""

    id: str
    cluster_id: str | None
    title: str
    status: str
    running: bool
    risk: str
    requested_by: str
    updated_at: datetime
    approvals: int          # 같은 run 에서 함께 나온 카드 수 (batch)



@dataclass(frozen=True)
class ClusterRow:
    """등록된 클러스터 한 행 (기획 04 §8). **자격증명은 여기 담지 않는다** — 실행 계층만 따로 읽는다."""

    id: str
    team_id: str | None
    registered_by: str
    name: str
    provider: str
    api_server: str
    ca_data: str | None
    insecure: bool
    context_name: str
    default_namespace: str
    fingerprint: str
    status: str
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: Cluster) -> "ClusterRow":
        return cls(
            id=row.id, team_id=row.team_id, registered_by=row.registered_by, name=row.name,
            provider=row.provider, api_server=row.api_server, ca_data=row.ca_data,
            insecure=row.insecure, context_name=row.context_name,
            default_namespace=row.default_namespace, fingerprint=row.fingerprint,
            status=row.status, last_checked_at=row.last_checked_at,
            created_at=row.created_at, updated_at=row.updated_at,
        )



def error_payload(code: str, message: str, http_status: int, **extra: Any) -> dict[str, Any]:
    """response_payload 에 남기는 실패 형태. http_status 는 같은 request_id 재전송에 같은 응답을 주기 위해."""
    return {"kind": "error", "code": code, "message": message, "http_status": http_status, **extra}


_APPLIED = ("APPLIED", "EFFECT_VERIFIED")


def _check_applied(status: str | None, result: dict[str, Any] | None) -> None:
    """DB 문서 5절: "executed(=APPLIED)라면 결과의 success=true, exit_code=0 을 요구한다".

    문서는 DB 가 검사한다고 적었지만 JSON 안을 들여다보는 CHECK 는 SQLite 와 Postgres 문법이 달라
    여기서 검사한다. 나머지 셋(결정 유무·결과 유무·실패 사유)은 표의 CHECK 가 본다.
    저장된 값의 앞뒤가 맞는지 보는 검사이고, 클러스터의 현재 상태를 증명하지는 않는다.
    """
    if status not in _APPLIED:
        return
    if not isinstance(result, dict) or result.get("success") is not True or result.get("exit_code") != 0:
        raise ValueError(f"{status} 는 성공한 실행 결과(success=true, exit_code=0)를 요구한다")


def _close_open_plans(db: Session, run_id: str) -> list[ActionPlan]:
    """열린 계획을 닫는 **유일한** 전이 규칙 (DURO-83). 커밋은 부르는 쪽이 한다.

    승인까지 갔는데 실행 결과가 없으면 UNKNOWN — kubectl 이 돌았는지 모른다 (DB 문서 5절, run 의
    recovery_required 와 같은 뜻). 아직 승인 전이면 STALE — 그 카드는 더 이상 쓸 수 없다.

    규칙을 두 벌로 두면 한쪽만 고쳐진다 (자동 리뷰 지적).
    """
    rows = db.scalars(
        select(ActionPlan).where(ActionPlan.run_id == run_id, ActionPlan.status.in_(PLAN_OPEN))
    ).all()
    for row in rows:
        approved = bool(row.decision and row.decision.get("approved"))
        row.status = "UNKNOWN" if approved and row.execution_result is None else "STALE"
    return list(rows)


def _plan_title(plan: ActionPlan) -> str:
    """목록에 보일 한 줄. 변경 툴의 intent(왜 하는지)가 사람이 읽기 가장 좋다."""
    payload = plan.plan_payload or {}
    intent = payload.get("intent")
    if isinstance(intent, str) and intent.strip():
        return intent.strip()
    return plan.tool_name


class ChatStore:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    # ── 채팅방 ───────────────────────────────────────────────

    def create_session(
        self,
        *,
        user_id: str,
        context_name: str,
        namespace: str,
        mode: str,
        title: str = "새 대화",
        installation_id: str | None = None,
        cluster_fingerprint: str | None = None,
        team_id: str | None = None,
        cluster_id: str | None = None,
        shared: bool = False,
    ) -> SessionRow:
        with self._factory() as db:
            row = ChatSession(
                user_id=user_id, title=title, current_mode=mode, installation_id=installation_id,
                context_name=context_name, namespace=namespace, cluster_fingerprint=cluster_fingerprint,
                team_id=team_id, cluster_id=cluster_id, shared=shared,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return SessionRow.of(row, running=False)

    def get_session(self, session_id: str) -> SessionRow | None:
        with self._factory() as db:
            row = db.get(ChatSession, session_id)
            if row is None:
                return None
            return SessionRow.of(row, running=self._has_run_with(db, session_id, ("running",)))

    def list_sessions(
        self, user_id: str, *, team_ids: list[str] | None, cluster_id: str | None = None
    ) -> list[SessionRow]:
        """내 방 + 내가 볼 수 있는 shared 방 (기획 05 §4: 팀원이 같이 본다).

        team_ids 는 부르는 쪽이 회원 서버에서 받아 넘긴다. 주면 팀이 붙은 shared 방은 그 목록에
        든 팀만 보인다 — 목록과 상세(_load)의 답이 달라 "목록에 있는데 열 수 없는 방" 이 생기던 것을
        맞춘다 (자동 리뷰 지적). 주지 않으면(개발 모드) 예전처럼 shared 전부.
        """
        with self._factory() as db:
            stmt = select(ChatSession).where(_visible_sessions(user_id, team_ids))
            if cluster_id is not None:
                stmt = stmt.where(ChatSession.cluster_id == cluster_id)
            stmt = stmt.order_by(ChatSession.updated_at.desc())
            rows = db.scalars(stmt).all()
            return [SessionRow.of(r, running=self._has_run_with(db, r.id, ("running",))) for r in rows]

    def update_session(self, session_id: str, **fields: Any) -> None:
        with self._factory() as db:
            row = db.get(ChatSession, session_id)
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)
            row.version += 1
            db.commit()

    # ── run ──────────────────────────────────────────────────

    def find_run(self, session_id: str, request_id: str) -> RunRow | None:
        with self._factory() as db:
            row = self._find_request(db, session_id, request_id)
            return RunRow.of(row) if row is not None else None

    def start_run(
        self,
        session_id: str,
        *,
        request_id: str,
        kind: str,
        mode: str,
        input_text: str | None,
    ) -> tuple[RunRow, bool]:
        """run 을 만든다. 반환 (run, created). 같은 request_id 면 기존 run 과 created=False."""
        with self._factory() as db:
            existing = self._find_request(db, session_id, request_id)
            if existing is not None:
                return self._replay(existing, kind, input_text), False
            if self._has_run_with(db, session_id, RUN_ACTIVE):
                raise ActiveRunExists(session_id)
            last = db.scalar(
                select(ChatRun.turn_no).where(ChatRun.session_id == session_id).order_by(ChatRun.turn_no.desc())
            )
            row = ChatRun(
                session_id=session_id, request_id=request_id, turn_no=(last or 0) + 1,
                kind=kind, mode=mode, status="running", input_text=input_text,
            )
            db.add(row)
            session = db.get(ChatSession, session_id)
            if session is not None:
                session.updated_at = datetime.now(timezone.utc)
            try:
                db.commit()
            except IntegrityError:
                # 검사와 INSERT 사이에 다른 요청이 먼저 넣었다 — 유니크 제약이 잡았다. 재조회로 판정한다.
                db.rollback()
                existing = self._find_request(db, session_id, request_id)
                if existing is not None:
                    return self._replay(existing, kind, input_text), False
                raise ActiveRunExists(session_id) from None
            db.refresh(row)
            return RunRow.of(row), True

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        response_payload: dict[str, Any] | None = _UNSET,
        agent_messages: list[Any] | None = _UNSET,
        usage_summary: dict[str, Any] | None = _UNSET,
    ) -> None:
        """넘긴 것만 바꾼다. 종료 상태로 바꾸면 finished_at 을 찍고, 활성 상태로 돌아가면 지운다 (문서 4절)."""
        with self._factory() as db:
            row = db.get(ChatRun, run_id)
            if row is None:
                return
            if response_payload is not _UNSET:
                row.response_payload = response_payload
            if agent_messages is not _UNSET:
                row.agent_messages = agent_messages
            if usage_summary is not _UNSET:
                row.usage_summary = usage_summary
            if status is not None:
                row.status = status
                row.finished_at = None if status in RUN_ACTIVE else datetime.now(timezone.utc)
            db.commit()

    def interrupt_active_runs(self, session_id: str, message: str) -> Interrupted:
        """재시작 복원 때: 활성 run 을 interrupted 로 닫는다. 승인 카드는 만료됐으니 error 로 바꾸고,
        이미 error(재개 실패, recovery_required)면 그 코드는 남긴다 — 앱이 "확인 필요" 를 계속 보여줘야 한다.

        닫은 계획도 함께 돌려준다 — 부르는 쪽이 `.md` 사본을 따라오게 한다 (자동 리뷰 지적).
        """
        with self._factory() as db:
            rows = db.scalars(
                select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.status.in_(RUN_ACTIVE))
            ).all()
            now = datetime.now(timezone.utc)
            closed: list[ActionPlan] = []
            for row in rows:
                payload = row.response_payload or {}
                if payload.get("kind") != "error":
                    row.response_payload = error_payload("INTERRUPTED", message, 409)
                row.status = "interrupted"
                row.finished_at = now
                # 계획도 **같은 트랜잭션에서** 닫는다. 따로 커밋하면 그 사이에 죽었을 때 run 은
                # interrupted 인데 계획은 열린 채 남고, 다시 지나가는 경로가 없다 (자동 리뷰 지적).
                closed += _close_open_plans(db, row.id)
            db.commit()
            return Interrupted([RunRow.of(r) for r in rows], [PlanRow.of(r) for r in closed])

    def list_runs(self, session_id: str) -> list[RunRow]:
        with self._factory() as db:
            rows = db.scalars(
                select(ChatRun).where(ChatRun.session_id == session_id).order_by(ChatRun.turn_no)
            ).all()
            return [RunRow.of(r) for r in rows]

    def active_run(self, session_id: str) -> RunRow | None:
        with self._factory() as db:
            row = db.scalar(
                select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.status.in_(RUN_ACTIVE))
            )
            return RunRow.of(row) if row is not None else None

    # ── Action Plan (문서 5절) ───────────────────────────────

    def create_plan(
        self,
        *,
        plan_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        risk: str,
        plan_payload: dict[str, Any],
        request_hash: str,
        status: str = "DRAFT",
    ) -> PlanRow:
        with self._factory() as db:
            _check_applied(status, None)
            row = ActionPlan(
                id=plan_id, run_id=run_id, tool_call_id=tool_call_id, tool_name=tool_name,
                status=status, risk=risk, plan_payload=plan_payload, request_hash=request_hash,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return PlanRow.of(row)

    def get_plan(self, plan_id: str) -> PlanRow | None:
        with self._factory() as db:
            row = db.get(ActionPlan, plan_id)
            return PlanRow.of(row) if row is not None else None

    def find_plan_by_call_id(self, tool_call_id: str, *, run_id: str | None = None) -> PlanRow | None:
        """카드 하나를 tool_call_id 로 찾는다. run 을 알면 같이 좁힌다 (run 안에서 유일 — 문서 5절)."""
        with self._factory() as db:
            stmt = select(ActionPlan).where(ActionPlan.tool_call_id == tool_call_id)
            if run_id is not None:
                stmt = stmt.where(ActionPlan.run_id == run_id)
            stmt = stmt.order_by(ActionPlan.created_at.desc())
            row = db.scalars(stmt).first()
            return PlanRow.of(row) if row is not None else None

    def update_plan(self, plan_id: str, **fields: Any) -> None:
        """행이 없으면 던진다. 계획은 표가 원본이라 "없으면 그만" 이 아니다 (자동 리뷰 지적) —
        조용히 넘어가면 상태 변화가 .md 에만 남고 표와 어긋난다. 방·run 의 no-op 과 뜻이 다르다."""
        with self._factory() as db:
            row = db.get(ActionPlan, plan_id)
            if row is None:
                raise PlanMissing(plan_id)
            for key, value in fields.items():
                setattr(row, key, value)
            _check_applied(row.status, row.execution_result)
            db.commit()

    def list_plans_for_run(self, run_id: str) -> list[PlanRow]:
        with self._factory() as db:
            rows = db.scalars(
                select(ActionPlan).where(ActionPlan.run_id == run_id).order_by(ActionPlan.created_at)
            ).all()
            return [PlanRow.of(r) for r in rows]

    def expire_open_plans(self, run_id: str) -> list[PlanRow]:
        """run 이 종료 상태로 닫힐 때 남은 계획을 닫는다 (DURO-83). 전이 규칙은 _close_open_plans 한 벌."""
        with self._factory() as db:
            rows = _close_open_plans(db, run_id)
            db.commit()
            return [PlanRow.of(r) for r in rows]

    def list_plan_summaries(
        self, user_id: str, *, team_ids: list[str] | None, cluster_id: str | None = None,
        status: str | None = None,
    ) -> list[PlanSummaryRow]:
        """대시보드 목록. 내 방 + 내가 볼 수 있는 shared 방의 계획만 (list_sessions 와 **같은 범위**).

        team_ids 는 부르는 쪽이 회원 서버에서 받아 넘긴다 — 안 넘기면 남의 팀 방 계획까지 섞인다
        (자동 리뷰 지적). 대화 목록과 규칙이 다르면 "목록엔 있는데 열 수 없는 계획" 이 생긴다.

        requested_by 는 방 주인이다 — run 에 요청자 칸이 없다 (문서 6절 "회원 id 를 중복 저장하지 않는다").
        shared 방에서 팀원이 보낸 요청도 방 주인으로 표시된다.
        """
        with self._factory() as db:
            stmt = (
                select(ActionPlan, ChatSession.cluster_id, ChatSession.user_id)
                .join(ChatRun, ActionPlan.run_id == ChatRun.id)
                .join(ChatSession, ChatRun.session_id == ChatSession.id)
                .where(_visible_sessions(user_id, team_ids))
            )
            if cluster_id is not None:
                stmt = stmt.where(ChatSession.cluster_id == cluster_id)
            if status is not None:
                stmt = stmt.where(ActionPlan.status == status)
            stmt = stmt.order_by(ActionPlan.updated_at.desc())
            found = db.execute(stmt).all()
            # 카드 수(batch)는 필터에 걸린 것만 세면 안 된다 — status 필터로 잘린 형제 카드도 같은 run 의 한 묶음이다
            run_ids = {plan.run_id for plan, _, _ in found}
            batch = dict(
                db.execute(
                    select(ActionPlan.run_id, func.count(ActionPlan.id))
                    .where(ActionPlan.run_id.in_(run_ids))
                    .group_by(ActionPlan.run_id)
                ).all()
            ) if run_ids else {}
            return [
                PlanSummaryRow(
                    id=plan.id, cluster_id=cluster, title=_plan_title(plan), status=plan.status,
                    running=plan.status == "EXECUTING", risk=plan.risk, requested_by=owner,
                    updated_at=plan.updated_at, approvals=batch.get(plan.run_id, 1),
                )
                for plan, cluster, owner in found
            ]

    def plan_scope(self, plan_id: str) -> "PlanScope | None":
        """이 계획이 속한 방의 (주인, shared, 팀) — 접근 권한 검사용.

        plan 에 회원 id 를 두지 않으므로 run → session 으로 거슬러 올라간다 (문서 6절). 팀까지 주는
        것은 부르는 쪽이 _load 와 같은 규칙(팀 방은 구성원만)을 걸 수 있어야 하기 때문이다.
        """
        with self._factory() as db:
            row = db.execute(
                select(ChatSession.user_id, ChatSession.shared, ChatSession.team_id)
                .join(ChatRun, ChatRun.session_id == ChatSession.id)
                .join(ActionPlan, ActionPlan.run_id == ChatRun.id)
                .where(ActionPlan.id == plan_id)
            ).first()
            return PlanScope(row[0], bool(row[1]), row[2]) if row is not None else None

    # ── 클러스터 (기획 04 §8) ─────────────────────────────

    def create_cluster(
        self, *, registered_by: str, name: str, api_server: str, ca_data: str | None,
        insecure: bool, credential_encrypted: str, context_name: str, default_namespace: str,
        fingerprint: str, team_id: str | None = None, provider: str = "GENERIC",
    ) -> ClusterRow:
        with self._factory() as db:
            row = Cluster(
                team_id=team_id, registered_by=registered_by, name=name, provider=provider,
                api_server=api_server, ca_data=ca_data, insecure=insecure,
                credential_encrypted=credential_encrypted, context_name=context_name,
                default_namespace=default_namespace, fingerprint=fingerprint,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return ClusterRow.of(row)

    def get_cluster(self, cluster_id: str) -> ClusterRow | None:
        with self._factory() as db:
            row = db.get(Cluster, cluster_id)
            return ClusterRow.of(row) if row is not None else None

    def cluster_credential(self, cluster_id: str) -> str | None:
        """암호문 그대로. 실행 계층만 부르고, 어떤 응답에도 실리지 않는다 (기획 04 §4)."""
        with self._factory() as db:
            row = db.get(Cluster, cluster_id)
            return row.credential_encrypted if row is not None else None

    def list_clusters(
        self, user_id: str, *, team_ids: list[str] | None = None, team_id: str | None = None
    ) -> list[ClusterRow]:
        """내가 등록한 것 + **내가 속한 팀** 것. team_ids 는 부르는 쪽이 회원 서버에서 받아 넘긴다.

        team_ids 를 주지 않으면 팀 클러스터는 보이지 않는다 — 예전에는 팀이 붙은 클러스터를 전부
        보여 줘서 남의 팀 것까지 새어 나갔다.
        """
        with self._factory() as db:
            # 개인 클러스터(team_id 가 NULL)만 "내가 등록했으니 내 것" 이다. 팀이 붙은 행은
            # 등록자여도 지금 소속으로 판단한다 — 팀에서 나간 뒤에도 보이면 안 된다 (자동 리뷰 P1).
            # `""` 도 "팀 없음" 으로 본다 — _readable 은 falsy 라 팀 검사를 건너뛰므로, 여기서
            # IS NULL 에만 맡기면 목록엔 없는데 상세·/test·DELETE 는 되는 행이 남는다 (자동 리뷰).
            no_team = or_(Cluster.team_id.is_(None), Cluster.team_id == "")
            mine = (Cluster.registered_by == user_id) & no_team
            if team_id is not None:
                stmt = select(Cluster).where(Cluster.team_id == team_id)
            elif team_ids:
                stmt = select(Cluster).where(or_(mine, Cluster.team_id.in_(team_ids)))
            else:
                stmt = select(Cluster).where(mine)
            rows = db.scalars(stmt.order_by(Cluster.created_at)).all()
            return [ClusterRow.of(r) for r in rows]

    def update_cluster(self, cluster_id: str, **fields: Any) -> None:
        with self._factory() as db:
            row = db.get(Cluster, cluster_id)
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)
            db.commit()

    def delete_cluster(self, cluster_id: str) -> bool:
        """자격증명까지 함께 사라진다 (기획 04 §4). 이 클러스터를 쓰는 방이 있으면 거절한다."""
        with self._factory() as db:
            row = db.get(Cluster, cluster_id)
            if row is None:
                return False
            using = db.scalar(select(ChatSession.id).where(ChatSession.cluster_id == cluster_id))
            if using is not None:
                raise ClusterInUse(cluster_id)
            db.delete(row)
            db.commit()
            return True

    # ── 내부 ─────────────────────────────────────────────────

    @staticmethod
    def _find_request(db: Session, session_id: str, request_id: str) -> ChatRun | None:
        return db.scalar(
            select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.request_id == request_id)
        )

    @staticmethod
    def _replay(existing: ChatRun, kind: str, input_text: str | None) -> RunRow:
        if existing.input_text != input_text or existing.kind != kind:
            raise RequestMismatch(existing.request_id)
        return RunRow.of(existing)

    @staticmethod
    def _has_run_with(db: Session, session_id: str, statuses: tuple[str, ...]) -> bool:
        return (
            db.scalar(
                select(ChatRun.id).where(ChatRun.session_id == session_id, ChatRun.status.in_(statuses))
            )
            is not None
        )
