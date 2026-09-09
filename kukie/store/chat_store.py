"""채팅방·run 읽기/쓰기. 라우터와 세션 레지스트리는 이 함수들만 쓴다.

멱등성 (DB 문서 4절): 같은 (session_id, request_id) 로 다시 오면 기존 run 을 돌려주고,
같은 id 에 다른 입력이면 거절한다. 채팅방 하나에 활성 run 은 최대 하나.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kukie.store.models import RUN_ACTIVE, ChatRun, ChatSession


class RequestMismatch(ValueError):
    """같은 request_id 로 다른 내용을 보냈다."""


class ActiveRunExists(RuntimeError):
    """이 채팅방에 아직 끝나지 않은 run 이 있다."""


@dataclass(frozen=True)
class SessionRow:
    id: str
    user_id: str
    team_id: str | None
    cluster_id: str | None
    title: str
    current_mode: str
    context_name: str
    namespace: str
    shared: bool
    created_at: datetime
    updated_at: datetime
    running: bool

    @classmethod
    def of(cls, row: ChatSession, *, running: bool) -> "SessionRow":
        return cls(
            id=row.id, user_id=row.user_id, team_id=row.team_id, cluster_id=row.cluster_id,
            title=row.title, current_mode=row.current_mode, context_name=row.context_name,
            namespace=row.namespace, shared=row.shared, created_at=row.created_at,
            updated_at=row.updated_at, running=running,
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
    response_payload: dict[str, Any] | None
    agent_messages: list[Any] | None
    error: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, row: ChatRun) -> "RunRow":
        return cls(
            id=row.id, session_id=row.session_id, request_id=row.request_id, turn_no=row.turn_no,
            kind=row.kind, mode=row.mode, status=row.status, input_text=row.input_text,
            response_payload=row.response_payload, agent_messages=row.agent_messages,
            error=row.error, started_at=row.started_at, finished_at=row.finished_at,
        )


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
        team_id: str | None = None,
        cluster_id: str | None = None,
        shared: bool = False,
    ) -> SessionRow:
        with self._factory() as db:
            row = ChatSession(
                user_id=user_id, team_id=team_id, cluster_id=cluster_id, title=title,
                current_mode=mode, context_name=context_name, namespace=namespace, shared=shared,
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
            return SessionRow.of(row, running=self._has_active_run(db, session_id))

    def list_sessions(self, user_id: str, *, cluster_id: str | None = None) -> list[SessionRow]:
        with self._factory() as db:
            stmt = select(ChatSession).where(ChatSession.user_id == user_id)
            if cluster_id is not None:
                stmt = stmt.where(ChatSession.cluster_id == cluster_id)
            stmt = stmt.order_by(ChatSession.updated_at.desc())
            rows = db.scalars(stmt).all()
            return [SessionRow.of(r, running=self._has_active_run(db, r.id)) for r in rows]

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
            existing = db.scalar(
                select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.request_id == request_id)
            )
            if existing is not None:
                if existing.input_text != input_text or existing.kind != kind:
                    raise RequestMismatch(request_id)
                return RunRow.of(existing), False
            if self._has_active_run(db, session_id):
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
            db.commit()
            db.refresh(row)
            return RunRow.of(row), True

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        response_payload: dict[str, Any] | None = None,
        agent_messages: list[Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self._factory() as db:
            row = db.get(ChatRun, run_id)
            if row is None:
                return
            row.status = status
            row.response_payload = response_payload
            row.agent_messages = agent_messages
            row.error = error
            if status not in RUN_ACTIVE:
                row.finished_at = datetime.now(timezone.utc)
            db.commit()

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

    @staticmethod
    def _has_active_run(db: Session, session_id: str) -> bool:
        return (
            db.scalar(
                select(ChatRun.id).where(ChatRun.session_id == session_id, ChatRun.status.in_(RUN_ACTIVE))
            )
            is not None
        )
