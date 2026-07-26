"""승인·거절 decision 소비 기록을 추가한다.

Revision ID: 0002_approval_decisions
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_approval_decisions"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """ApprovalResponse와 exact successor를 저장하는 table을 만든다."""

    op.create_table(
        "approval_decisions",
        sa.Column("decision_id", sa.String(length=255), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=255),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sa.String(length=255), nullable=False),
        sa.Column("response_snapshot_json", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Text(), nullable=False),
        sa.Column("result_task_snapshot_json", sa.Text(), nullable=False),
        sa.Column("failure", sa.String(length=100), nullable=True),
        sa.UniqueConstraint(
            "task_id",
            "task_version",
            "plan_hash",
            name="uq_approval_decisions_binding",
        ),
    )


def downgrade() -> None:
    """Approval decision table을 제거한다."""

    op.drop_table("approval_decisions")
