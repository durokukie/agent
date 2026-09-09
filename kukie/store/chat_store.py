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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kukie.store.models import RUN_ACTIVE, ChatRun, ChatSession

_UNSET: Any = object()


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


def error_payload(code: str, message: str, http_status: int, **extra: Any) -> dict[str, Any]:
    """response_payload 에 남기는 실패 형태. http_status 는 같은 request_id 재전송에 같은 응답을 주기 위해."""
    return {"kind": "error", "code": code, "message": message, "http_status": http_status, **extra}


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

    def list_sessions(self, user_id: str, *, cluster_id: str | None = None) -> list[SessionRow]:
        with self._factory() as db:
            stmt = select(ChatSession).where(ChatSession.user_id == user_id)
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

    def interrupt_active_runs(self, session_id: str, message: str) -> list[RunRow]:
        """재시작 복원 때: 활성 run 을 interrupted 로 닫는다. 승인 카드는 만료됐으니 error 로 바꾸고,
        이미 error(재개 실패, recovery_required)면 그 코드는 남긴다 — 앱이 "확인 필요" 를 계속 보여줘야 한다."""
        with self._factory() as db:
            rows = db.scalars(
                select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.status.in_(RUN_ACTIVE))
            ).all()
            now = datetime.now(timezone.utc)
            for row in rows:
                payload = row.response_payload or {}
                if payload.get("kind") != "error":
                    row.response_payload = error_payload("INTERRUPTED", message, 409)
                row.status = "interrupted"
                row.finished_at = now
            db.commit()
            return [RunRow.of(r) for r in rows]

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
