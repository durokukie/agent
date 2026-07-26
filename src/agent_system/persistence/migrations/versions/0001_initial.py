"""초기 애플리케이션 persistence schema.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Task 실행과 전달 의도를 보존하는 초기 table을 만든다."""

    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(length=255), primary_key=True),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_tasks_version_positive"),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "task_events",
        sa.Column("event_id", sa.String(length=255), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "task_version",
            name="uq_task_events_task_version",
        ),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.execute(
        """
        CREATE TRIGGER task_events_no_update
        BEFORE UPDATE ON task_events
        BEGIN
            SELECT RAISE(ABORT, 'task_events are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_events_no_delete
        BEFORE DELETE ON task_events
        BEGIN
            SELECT RAISE(ABORT, 'task_events are append-only');
        END
        """
    )

    op.create_table(
        "workflow_runs",
        sa.Column("workflow_run_id", sa.String(length=255), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "agent_runs",
        sa.Column("agent_run_id", sa.String(length=255), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=255),
            sa.ForeignKey("workflow_runs.workflow_run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("budget_sequence", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "workflow_run_id",
            "budget_sequence",
            name="uq_agent_runs_workflow_budget_sequence",
        ),
    )
    op.create_index(
        "ix_agent_runs_workflow_run_id",
        "agent_runs",
        ["workflow_run_id"],
    )

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(length=255), primary_key=True),
        sa.Column("decision_id", sa.String(length=255), nullable=False),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sa.String(length=255), nullable=False),
        sa.Column("approval_snapshot_json", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Text(), nullable=False),
        sa.Column("result_task_snapshot_json", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "decision_id",
            name="uq_approvals_task_decision",
        ),
        sa.UniqueConstraint(
            "task_id",
            "task_version",
            "plan_hash",
            name="uq_approvals_binding",
        ),
    )

    op.create_table(
        "request_idempotency",
        sa.Column("namespace", sa.String(length=100), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=255), primary_key=True),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "outbox_events",
        sa.Column("outbox_id", sa.String(length=255), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_attempt_count_nonnegative",
        ),
    )
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])


def downgrade() -> None:
    """초기 app schema를 dependency 역순으로 제거한다."""

    op.drop_index("ix_outbox_events_status", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("request_idempotency")
    op.drop_table("approvals")
    op.drop_index("ix_agent_runs_workflow_run_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_table("workflow_runs")
    op.execute("DROP TRIGGER task_events_no_delete")
    op.execute("DROP TRIGGER task_events_no_update")
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
