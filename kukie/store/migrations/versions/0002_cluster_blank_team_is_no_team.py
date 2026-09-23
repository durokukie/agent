"""tbl_cluster — team_id '' 를 NULL 로 접고, 이름 유일성 인덱스가 '' 도 "팀 없음" 으로 보게 (issue #67, #64 리뷰 13차).

읽는 쪽(list_clusters · _visible_sessions)은 이미 '' 를 팀 없음으로 보는데, Alembic 이전 DB 의 인덱스는
IS NULL 만 봐서 '' 행이 개인 이름 유일성 밖에 남았다(한 사람 목록에 같은 이름이 둘 뜰 수 있다).

'' → NULL 로 바꾸면 새 인덱스에 걸릴 수 있는 행(같은 사람, 같은 이름의 '' 행과 NULL 행)이 있으면
**멈추고 어느 행인지 말한다.** 이름을 몰래 바꾸면 사용자 화면이 달라지므로 사람이 정리한 뒤 다시 켠다
(DURO-110 결정). '' 를 만드는 입구는 이미 닫혀 있어(create_cluster 가 접는다) 실제로 겹칠 DB 는 없다시피 하다.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NO_TEAM = "team_id IS NULL OR team_id = ''"
HAS_TEAM = "team_id IS NOT NULL AND team_id <> ''"


def upgrade() -> None:
    bind = op.get_bind()
    # 1) '' 를 NULL 로 접었을 때 (registered_by, name) 이 겹치는 행 — 멈추고 알린다
    clashes = bind.execute(sa.text(
        "SELECT registered_by, name, COUNT(*) AS n FROM tbl_cluster "
        f"WHERE {NO_TEAM} GROUP BY registered_by, name HAVING COUNT(*) > 1"
    )).fetchall()
    if clashes:
        listed = ", ".join(f"{r.registered_by}/{r.name}({r.n}개)" for r in clashes)
        raise RuntimeError(
            "tbl_cluster 에 팀 없는 클러스터 이름이 겹치는 행이 있어 마이그레이션 0002 를 멈춘다 — "
            f"{listed}. 같은 사람의 같은 이름 중 하나를 바꾸거나 지운 뒤 다시 켜라"
        )
    # 2) '' → NULL (읽는 쪽과 같은 규칙으로 저장 모양을 하나로)
    op.execute(sa.text("UPDATE tbl_cluster SET team_id = NULL WHERE team_id = ''"))
    # 3) 인덱스 조건을 새 규칙으로 — 옛 DB 도 새 DB(0001)도 여기서 같은 모양이 된다
    op.drop_index("uq_cluster_personal_name", table_name="tbl_cluster")
    op.drop_index("uq_cluster_team_name", table_name="tbl_cluster")
    op.create_index("uq_cluster_personal_name", "tbl_cluster", ["registered_by", "name"], unique=True,
                    sqlite_where=sa.text(NO_TEAM), postgresql_where=sa.text(NO_TEAM))
    op.create_index("uq_cluster_team_name", "tbl_cluster", ["team_id", "name"], unique=True,
                    sqlite_where=sa.text(HAS_TEAM), postgresql_where=sa.text(HAS_TEAM))


def downgrade() -> None:
    # 데이터('' → NULL)는 되돌리지 않는다 — 옛 조건 아래서도 NULL 은 올바른 "팀 없음" 이다
    op.drop_index("uq_cluster_personal_name", table_name="tbl_cluster")
    op.drop_index("uq_cluster_team_name", table_name="tbl_cluster")
    op.create_index("uq_cluster_personal_name", "tbl_cluster", ["registered_by", "name"], unique=True,
                    sqlite_where=sa.text("team_id IS NULL"), postgresql_where=sa.text("team_id IS NULL"))
    op.create_index("uq_cluster_team_name", "tbl_cluster", ["team_id", "name"], unique=True,
                    sqlite_where=sa.text("team_id IS NOT NULL"), postgresql_where=sa.text("team_id IS NOT NULL"))
