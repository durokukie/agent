"""${message}

왜: (생성한 뒤 사람이 적는다 — 무엇을 왜 바꾸나)
데이터: (기존 행을 옮기거나 고치는 부분만 손으로 더하고 이유를 적는다. 없으면 이 줄을 지운다)

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
