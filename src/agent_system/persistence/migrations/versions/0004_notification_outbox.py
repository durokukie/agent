"""Notification retry와 lease 정보를 outbox에 추가한다.

Revision ID: 0004_notification_outbox
Revises: 0003_runtime_commands
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_notification_outbox"
down_revision: str | None = "0003_runtime_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """기존 outbox row를 보존하며 eligibility와 claim lease를 추가한다."""

    op.add_column(
        "outbox_events",
        sa.Column("task_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("next_attempt_at", sa.Text(), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("lease_token", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("lease_expires_at", sa.Text(), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("delivered_at", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE outbox_events SET next_attempt_at = created_at "
        "WHERE next_attempt_at IS NULL"
    )
    with op.batch_alter_table("outbox_events") as batch:
        batch.alter_column(
            "next_attempt_at",
            existing_type=sa.Text(),
            nullable=False,
        )
    op.create_index(
        "uq_outbox_task_version_topic",
        "outbox_events",
        ["task_id", "task_version", "topic"],
        unique=True,
        sqlite_where=sa.text("task_version IS NOT NULL"),
    )
    op.create_index(
        "ix_outbox_delivery_eligibility",
        "outbox_events",
        ["status", "next_attempt_at", "lease_expires_at"],
    )


def downgrade() -> None:
    """Notification 전용 retry와 lease field를 제거한다."""

    op.drop_index("ix_outbox_delivery_eligibility", table_name="outbox_events")
    op.drop_index("uq_outbox_task_version_topic", table_name="outbox_events")
    with op.batch_alter_table("outbox_events") as batch:
        batch.drop_column("delivered_at")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_token")
        batch.drop_column("next_attempt_at")
        batch.drop_column("task_version")
