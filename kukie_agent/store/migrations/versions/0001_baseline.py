"""baseline — Alembic 을 붙이기 전 create_all 이 만들던 표 모양 그대로 (2026-09-22 기준).

이 리비전은 **얼려 둔다.** 모델을 바꾸면 여기를 고치지 말고 새 리비전을 만든다.
Alembic 이전에 만든 DB(표는 있는데 alembic_version 이 없음)는 kukie.store.db 가 이 리비전으로 도장을
찍고 그 뒤 것만 적용한다 — 그래서 여기 모양은 그 DB 들과 같아야 한다. 노트북의 옛 ~/.kukie/kukie.db 와
sqlite_master 를 대조해 확인했다: 차이는 tbl_cluster 의 조건부 유일 인덱스 둘뿐이고, 그 둘은 옛 조건
(IS NULL / IS NOT NULL) 그대로 여기 두고 0002 에서 바꾼다.

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('tbl_chat_session',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('current_mode', sa.String(length=20), nullable=False),
    sa.Column('installation_id', sa.String(length=64), nullable=True),
    sa.Column('context_name', sa.String(length=200), nullable=False),
    sa.Column('namespace', sa.String(length=63), nullable=False),
    sa.Column('cluster_fingerprint', sa.String(length=200), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('team_id', sa.String(length=64), nullable=True),
    sa.Column('cluster_id', sa.String(length=64), nullable=True),
    sa.Column('shared', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tbl_chat_session', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tbl_chat_session_cluster_id'), ['cluster_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tbl_chat_session_team_id'), ['team_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tbl_chat_session_user_id'), ['user_id'], unique=False)

    op.create_table('tbl_cluster',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=64), nullable=True),
    sa.Column('registered_by', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('provider', sa.String(length=20), nullable=False),
    sa.Column('api_server', sa.String(length=300), nullable=False),
    sa.Column('ca_data', sa.Text(), nullable=True),
    sa.Column('insecure', sa.Boolean(), nullable=False),
    sa.Column('credential_encrypted', sa.Text(), nullable=False),
    sa.Column('context_name', sa.String(length=200), nullable=False),
    sa.Column('default_namespace', sa.String(length=63), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("api_server LIKE 'https://%'", name='ck_cluster_https'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tbl_cluster', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tbl_cluster_fingerprint'), ['fingerprint'], unique=False)
        batch_op.create_index(batch_op.f('ix_tbl_cluster_registered_by'), ['registered_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_tbl_cluster_team_id'), ['team_id'], unique=False)
        # 옛 조건 그대로 (0002 가 '' 도 "팀 없음" 으로 보게 바꾼다)
        batch_op.create_index('uq_cluster_personal_name', ['registered_by', 'name'], unique=True,
                              sqlite_where=sa.text("team_id IS NULL"), postgresql_where=sa.text("team_id IS NULL"))
        batch_op.create_index('uq_cluster_team_name', ['team_id', 'name'], unique=True,
                              sqlite_where=sa.text("team_id IS NOT NULL"), postgresql_where=sa.text("team_id IS NOT NULL"))

    op.create_table('tbl_chat_run',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('turn_no', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('input_text', sa.Text(), nullable=True),
    sa.Column('response_payload', sa.JSON(), nullable=True),
    sa.Column('agent_messages', sa.JSON(), nullable=True),
    sa.Column('history_format_version', sa.Integer(), nullable=False),
    sa.Column('usage_summary', sa.JSON(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['tbl_chat_session.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'request_id', name='uq_run_request'),
    sa.UniqueConstraint('session_id', 'turn_no', name='uq_run_turn')
    )
    with op.batch_alter_table('tbl_chat_run', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tbl_chat_run_session_id'), ['session_id'], unique=False)

    op.create_table('tbl_action_plan',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('tool_call_id', sa.String(length=64), nullable=False),
    sa.Column('tool_name', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('risk', sa.String(length=20), nullable=False),
    sa.Column('plan_payload', sa.JSON(), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('decision', sa.JSON(), nullable=True),
    sa.Column('execution_result', sa.JSON(), nullable=True),
    sa.Column('failure_reason', sa.String(length=40), nullable=True),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status <> 'FAILED' OR failure_reason IS NOT NULL", name='ck_plan_failed_has_reason'),
    sa.CheckConstraint("status NOT IN ('APPLIED', 'EFFECT_VERIFIED') OR (execution_result IS NOT NULL AND applied_at IS NOT NULL)", name='ck_plan_applied_has_result'),
    sa.CheckConstraint("status NOT IN ('APPROVED', 'EXECUTING', 'APPLIED', 'EFFECT_VERIFIED') OR decision IS NOT NULL", name='ck_plan_decision_present'),
    sa.ForeignKeyConstraint(['run_id'], ['tbl_chat_run.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'tool_call_id', name='uq_plan_tool_call')
    )
    with op.batch_alter_table('tbl_action_plan', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tbl_action_plan_run_id'), ['run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tbl_action_plan_tool_call_id'), ['tool_call_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('tbl_action_plan', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tbl_action_plan_tool_call_id'))
        batch_op.drop_index(batch_op.f('ix_tbl_action_plan_run_id'))

    op.drop_table('tbl_action_plan')
    with op.batch_alter_table('tbl_chat_run', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tbl_chat_run_session_id'))

    op.drop_table('tbl_chat_run')
    with op.batch_alter_table('tbl_cluster', schema=None) as batch_op:
        batch_op.drop_index('uq_cluster_team_name')
        batch_op.drop_index('uq_cluster_personal_name')
        batch_op.drop_index(batch_op.f('ix_tbl_cluster_team_id'))
        batch_op.drop_index(batch_op.f('ix_tbl_cluster_registered_by'))
        batch_op.drop_index(batch_op.f('ix_tbl_cluster_fingerprint'))

    op.drop_table('tbl_cluster')
    with op.batch_alter_table('tbl_chat_session', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tbl_chat_session_user_id'))
        batch_op.drop_index(batch_op.f('ix_tbl_chat_session_team_id'))
        batch_op.drop_index(batch_op.f('ix_tbl_chat_session_cluster_id'))

    op.drop_table('tbl_chat_session')
