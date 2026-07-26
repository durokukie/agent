"""Alembic app table에 대응하는 private SQLAlchemy mapping."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """create_all에 사용하지 않는 ORM mapping registry."""


class TaskRow(Base):
    """현재 Task snapshot row."""

    __tablename__ = "tasks"
    __table_args__ = (CheckConstraint("version > 0", name="ck_tasks_version_positive"),)

    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    input: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    plan_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class TaskEventRow(Base):
    """append-only Task event row."""

    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_version",
            name="uq_task_events_task_version",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
        index=True,
    )
    task_version: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[str] = mapped_column(Text)


class OutboxRow(Base):
    """알림 전달 의도 row."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_attempt_count_nonnegative",
        ),
    )

    outbox_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
        nullable=True,
    )
    topic: Mapped[str] = mapped_column(String(255))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RequestIdempotencyRow(Base):
    """외부 요청 key를 최초 생성 Task에 결합하는 row."""

    __tablename__ = "request_idempotency"

    namespace: Mapped[str] = mapped_column(String(100), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(255))
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
    )
    created_at: Mapped[str] = mapped_column(Text)


class WorkflowRunRow(Base):
    """WorkflowRun의 현재 issuance ledger 포함 snapshot row."""

    __tablename__ = "workflow_runs"

    workflow_run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
        unique=True,
    )
    snapshot_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class AgentRunRow(Base):
    """WorkflowRun이 발급한 모든 AgentRun history row."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id",
            "budget_sequence",
            name="uq_agent_runs_workflow_budget_sequence",
        ),
    )

    agent_run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("workflow_runs.workflow_run_id", ondelete="RESTRICT"),
        index=True,
    )
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
    )
    budget_sequence: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(Text)


class ApprovalRow(Base):
    """Approval binding의 소비와 결과 Task snapshot row."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "decision_id",
            name="uq_approvals_task_decision",
        ),
        UniqueConstraint(
            "task_id",
            "task_version",
            "plan_hash",
            name="uq_approvals_binding",
        ),
    )

    approval_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(255))
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
    )
    task_version: Mapped[int] = mapped_column(Integer)
    plan_hash: Mapped[str] = mapped_column(String(255))
    approval_snapshot_json: Mapped[str] = mapped_column(Text)
    consumed_at: Mapped[str] = mapped_column(Text)
    result_task_snapshot_json: Mapped[str] = mapped_column(Text)


class ApprovalDecisionRow(Base):
    """승인·거절 응답과 exact successor를 함께 보존하는 row."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_version",
            "plan_hash",
            name="uq_approval_decisions_binding",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
    )
    task_version: Mapped[int] = mapped_column(Integer)
    plan_hash: Mapped[str] = mapped_column(String(255))
    response_snapshot_json: Mapped[str] = mapped_column(Text)
    consumed_at: Mapped[str] = mapped_column(Text)
    result_task_snapshot_json: Mapped[str] = mapped_column(Text)
    failure: Mapped[str | None] = mapped_column(String(100), nullable=True)


class RuntimeCommandRow(Base):
    """HTTP 202 전에 저장하는 background command row."""

    __tablename__ = "runtime_commands"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "fingerprint",
            name="uq_runtime_commands_task_fingerprint",
        ),
        Index(
            "uq_runtime_commands_pending_task",
            "task_id",
            unique=True,
            sqlite_where=text("status = 'PENDING'"),
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_runtime_commands_attempt_nonnegative",
        ),
    )

    command_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("tasks.task_id", ondelete="RESTRICT"),
        index=True,
    )
    command_type: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(255))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "AgentRunRow",
    "ApprovalDecisionRow",
    "ApprovalRow",
    "OutboxRow",
    "RequestIdempotencyRow",
    "RuntimeCommandRow",
    "TaskEventRow",
    "TaskRow",
    "WorkflowRunRow",
]
