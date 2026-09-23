"""Postgres 에서의 마이그레이션 — 배포(컴포즈 · Aurora)가 쓰는 DB (DURO-110, PR #83).

KUKIE_TEST_POSTGRES_URL 이 있을 때만 돈다 (CI 의 postgres 서비스, 로컬은 docker 로 하나 띄우고 지정).
SQLite 쪽 test_migrations.py 와 같은 시나리오를 Postgres 카탈로그(information_schema · pg_indexes)로 확인한다.
테스트마다 새 데이터베이스를 만든다 — 상태가 섞이지 않게.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from kukie.store import reset_store_for_tests
from kukie.store.models import Base

ADMIN_URL = os.environ.get("KUKIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="KUKIE_TEST_POSTGRES_URL 이 없다 — Postgres 실기는 CI 의 postgres 잡에서")

HEAD = "0002"
OLD_PERSONAL = "CREATE UNIQUE INDEX uq_cluster_personal_name ON tbl_cluster (registered_by, name) WHERE team_id IS NULL"
OLD_TEAM = "CREATE UNIQUE INDEX uq_cluster_team_name ON tbl_cluster (team_id, name) WHERE team_id IS NOT NULL"
INSERT = ("INSERT INTO tbl_cluster (id, team_id, registered_by, name, provider, api_server, insecure, credential_encrypted, "
          "context_name, default_namespace, fingerprint, status, created_at, updated_at) VALUES "
          "(:id, :team_id, :by, :name, 'GENERIC', 'https://x', false, 'enc', 'ctx', 'default', :id, 'disconnected', now(), now())")


@pytest.fixture
def db_url():
    """테스트마다 빈 데이터베이스 하나. 끝나면 지운다."""
    name = f"kukie_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as con:
        con.execute(text(f'CREATE DATABASE "{name}"'))
    yield make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)   # str() 은 비밀번호를 *** 로 가린다
    with admin.connect() as con:
        con.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def query(url: str, sql: str, **params):
    engine = create_engine(url)
    try:
        with engine.connect() as con:
            return con.execute(text(sql), params).fetchall()
    finally:
        engine.dispose()


def version(url: str) -> str | None:
    exists = query(url, "SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version'")
    return query(url, "SELECT version_num FROM alembic_version")[0][0] if exists else None


def tables(url: str) -> list[str]:
    return sorted(r[0] for r in query(url, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))


def where_of(url: str, index: str) -> str:
    """부분 유일 인덱스의 WHERE 절 — Postgres 가 정규화해 돌려주는 모양 그대로."""
    (indexdef,) = query(url, "SELECT indexdef FROM pg_indexes WHERE indexname = :i", i=index)[0]
    return indexdef.split("WHERE", 1)[1].strip()


def make_old_db(url: str, clusters: list[tuple[str, str | None, str, str]]) -> None:
    """Alembic 이전 모양 — 지금 모델 + 옛 인덱스 조건. Postgres 엔 옛 실물이 없어 이렇게 재현한다."""
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as con:
        con.execute(text("DROP INDEX uq_cluster_personal_name"))
        con.execute(text("DROP INDEX uq_cluster_team_name"))
        con.execute(text(OLD_PERSONAL))
        con.execute(text(OLD_TEAM))
        for cid, team_id, by, name in clusters:
            con.execute(text(INSERT), {"id": cid, "team_id": team_id, "by": by, "name": name})
    engine.dispose()
    assert version(url) is None


def test_빈_DB는_head까지_만들어지고_부분_인덱스_조건이_새_규칙이다(db_url):
    reset_store_for_tests(db_url)

    assert version(db_url) == HEAD
    assert tables(db_url) == ["alembic_version", "tbl_action_plan", "tbl_chat_run", "tbl_chat_session", "tbl_cluster"]
    assert where_of(db_url, "uq_cluster_personal_name") == "((team_id IS NULL) OR ((team_id)::text = ''::text))"
    assert where_of(db_url, "uq_cluster_team_name") == "((team_id IS NOT NULL) AND ((team_id)::text <> ''::text))"


def test_다시_켜도_그대로다(db_url):
    reset_store_for_tests(db_url)
    before = (version(db_url), tables(db_url), where_of(db_url, "uq_cluster_personal_name"))

    reset_store_for_tests(db_url)

    assert (version(db_url), tables(db_url), where_of(db_url, "uq_cluster_personal_name")) == before


def test_옛_DB는_도장을_찍고_0002를_적용한다(db_url):
    make_old_db(db_url, [("a", "", "u1", "운영"), ("b", None, "u1", "스테이징"), ("c", "t1", "u2", "운영")])

    reset_store_for_tests(db_url)

    assert version(db_url) == HEAD
    assert dict(query(db_url, "SELECT id, team_id FROM tbl_cluster")) == {"a": None, "b": None, "c": "t1"}
    assert where_of(db_url, "uq_cluster_personal_name") == "((team_id IS NULL) OR ((team_id)::text = ''::text))"


def test_남의_표가_있는_공유_DB는_새_DB로_보고_그_표는_건드리지_않는다(db_url):
    """회원 서버(Spring)와 한 Postgres 를 나눠 쓰는 경우 (PR #83 리뷰 2차)."""
    engine = create_engine(db_url)
    with engine.begin() as con:
        con.execute(text("CREATE TABLE tbl_team (id varchar(64) PRIMARY KEY, name varchar(100))"))
        con.execute(text("INSERT INTO tbl_team VALUES ('t1', 'DURO')"))
    engine.dispose()

    reset_store_for_tests(db_url)

    assert version(db_url) == HEAD
    assert "tbl_cluster" in tables(db_url)
    assert query(db_url, "SELECT count(*) FROM tbl_team")[0][0] == 1


def test_이름이_겹치면_멈추고_Postgres는_카드도_남기지_않는다(db_url):
    """Postgres 는 DDL 도 트랜잭션 안이라 멈추면 alembic_version 표까지 되돌아간다 — SQLite 와 달리 빈 카드가 안 남는다."""
    make_old_db(db_url, [("a", "", "u1", "운영"), ("b", None, "u1", "운영")])

    with pytest.raises(RuntimeError, match=r"u1/운영\(2개\)"):
        reset_store_for_tests(db_url)

    assert version(db_url) is None
    assert dict(query(db_url, "SELECT id, team_id FROM tbl_cluster")) == {"a": "", "b": None}
    assert where_of(db_url, "uq_cluster_personal_name") == "(team_id IS NULL)"
