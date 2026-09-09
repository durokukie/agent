"""SQLAlchemy 모델 — DB 문서의 tbl_chat_session / tbl_chat_run 을 그대로 옮기고 셋을 더한다.

더한 컬럼 (product-spec: 팀 → 클러스터 → 대화):
  chat_session.team_id, cluster_id, shared  — 없으면 "팀 공유 대화" 를 고를 수 없다.

Action Plan 은 아직 .md 파일(guardrail/action_plan.py)이 원본이다. 인덱스 표는 /action-plans 이슈에서.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# run 상태 — DB 문서 4절. 앞의 셋은 활성(finished_at 없음), 뒤의 셋은 종료.
RUN_ACTIVE = ("running", "awaiting_approval", "recovery_required")
RUN_FINAL = ("completed", "failed", "interrupted")
RUN_STATUSES = RUN_ACTIVE + RUN_FINAL

RUN_KINDS = ("chat", "mode_change", "approve", "resume")


class ChatSession(Base):
    """채팅방 하나. session 은 로그인 세션이 아니라 대화방이다."""

    __tablename__ = "chat_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    team_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="새 대화")
    current_mode: Mapped[str] = mapped_column(String(20))          # 학습 / 진단 / 실습
    context_name: Mapped[str] = mapped_column(String(200))
    namespace: Mapped[str] = mapped_column(String(63))
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    runs: Mapped[list["ChatRun"]] = relationship(back_populates="session", order_by="ChatRun.turn_no")


class ChatRun(Base):
    """사용자 요청 한 건. LLM 호출 한 번이 아니라 요청 하나 처리 전체."""

    __tablename__ = "chat_run"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id", name="uq_run_request"),
        UniqueConstraint("session_id", "turn_no", name="uq_run_turn"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_session.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(64))
    turn_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))                  # RUN_KINDS
    mode: Mapped[str] = mapped_column(String(20))                  # 이 run 에 적용된 모드
    status: Mapped[str] = mapped_column(String(20))                # RUN_STATUSES
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 앱이 그릴 ChatPayload
    agent_messages: Mapped[list | None] = mapped_column(JSON, nullable=True)     # 이 run 의 ModelMessage 들
    history_format_version: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)              # 실패 시 {code, message}
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[ChatSession] = relationship(back_populates="runs")
