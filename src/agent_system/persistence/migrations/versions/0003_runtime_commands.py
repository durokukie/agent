"""Durable background runtime command를 추가한다.

Revision ID: 0003_runtime_commands
Revises: 0002_approval_decisions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_runtime_commands"
down_revision: str | None = "0002_approval_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """HTTP 202 전에 저장할 runtime command table을 만든다."""

    op.create_table(
        "runtime_commands",
        sa.Column("command_id", sa.String(length=255), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_runtime_commands_attempt_nonnegative",
        ),
        sa.UniqueConstraint(
            "task_id",
            "fingerprint",
            name="uq_runtime_commands_task_fingerprint",
        ),
    )
    op.create_index(
        "ix_runtime_commands_task_id",
        "runtime_commands",
        ["task_id"],
    )
    op.create_index(
        "ix_runtime_commands_status",
        "runtime_commands",
        ["status"],
    )
    op.create_index(
        "uq_runtime_commands_pending_task",
        "runtime_commands",
        ["task_id"],
        unique=True,
        sqlite_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    """Runtime command table을 제거한다."""

    op.drop_table("runtime_commands")
