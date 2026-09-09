"""SQLAlchemy 모델 — Linear "Kukie 채팅 DB 구조" 문서의 tbl_chat_session / tbl_chat_run 을 그대로 옮긴다.

문서와 다른 점은 셋뿐이고 issue #53 에 적었다 (product-spec: 팀 → 클러스터 → 대화):
  tbl_chat_session.team_id, cluster_id, shared  — 없으면 "팀 공유 대화" 를 고를 수 없다.

tbl_user 는 Spring 회원 서버 것이라 여기 없다 (user_id 는 문자열로만 들고 소유권만 검사한다).
tbl_action_plan 은 아직 .md 파일(guardrail/action_plan.py)이 원본이다 — 다음 이슈.
실패 정보는 별도 컬럼이 아니라 response_payload 에 {"kind": "error", ...} 로 남긴다 (문서에 error 컬럼이 없다).
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


# run 상태 — DB 문서 4절. 앞의 셋은 활성(finished_at 없음), 뒤의 셋은 종료(finished_at 있음).
# "채팅방 하나에 활성 run 은 최대 하나" — 활성 run 이 있으면 새 요청·모드 변경을 막는다.
RUN_ACTIVE = ("running", "awaiting_approval", "recovery_required")
RUN_FINAL = ("completed", "failed", "interrupted")
RUN_STATUSES = RUN_ACTIVE + RUN_FINAL

# 문서 4절: 일반 요청 chat, 모드 변경 mode_change. 승인·재개는 새 run 이 아니라 기존 run 을 이어서 끝낸다 (7절).
RUN_KINDS = ("chat", "mode_change")

MODES = ("학습", "진단", "실습")


class ChatSession(Base):
    """채팅방 하나. session 은 로그인 세션이 아니라 대화방이다 (문서 3절)."""

    __tablename__ = "tbl_chat_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)                 # tbl_user.id (Spring)
    title: Mapped[str] = mapped_column(String(200), default="새 대화")
    current_mode: Mapped[str] = mapped_column(String(20))                        # MODES — 다음 요청에 적용
    installation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)   # kubectl 을 실행하는 agent 설치 환경
    context_name: Mapped[str] = mapped_column(String(200))                       # kubeconfig context
    namespace: Mapped[str] = mapped_column(String(63))
    cluster_fingerprint: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 실제 대상 클러스터 확인값
    version: Mapped[int] = mapped_column(Integer, default=1)                     # 오래된 화면의 변경 요청 감지
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # ── 문서에 없는 셋 (issue #53) ──
    team_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)

    runs: Mapped[list["ChatRun"]] = relationship(back_populates="session", order_by="ChatRun.turn_no")


class ChatRun(Base):
    """사용자 요청 한 건 (문서 4절). LLM 호출 한 번이 아니라 요청 하나 처리 전체."""

    __tablename__ = "tbl_chat_run"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id", name="uq_run_request"),
        UniqueConstraint("session_id", "turn_no", name="uq_run_turn"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tbl_chat_session.id", ondelete="RESTRICT"), index=True,
    )
    request_id: Mapped[str] = mapped_column(String(64))                          # 재전송 식별
    turn_no: Mapped[int] = mapped_column(Integer)                                # 방 안의 순서, 1부터
    kind: Mapped[str] = mapped_column(String(20))                                # RUN_KINDS
    mode: Mapped[str] = mapped_column(String(20))                                # 이 run 에 적용된 모드 (과거 값 유지)
    status: Mapped[str] = mapped_column(String(20))                              # RUN_STATUSES
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 화면에 그릴 ChatPayload. 실패면 kind=error
    agent_messages: Mapped[list | None] = mapped_column(JSON, nullable=True)     # 이 run 의 ModelMessage 들
    history_format_version: Mapped[int] = mapped_column(Integer, default=1)
    usage_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)      # 토큰 사용량. 모르면 null
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[ChatSession] = relationship(back_populates="runs")
