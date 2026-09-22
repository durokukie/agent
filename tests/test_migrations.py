"""DB 마이그레이션 (issue #67, DURO-110).

create_all 은 없는 표만 만들어서 이미 있는 DB 의 인덱스 변경이 안 나갔다. 이제 켜질 때 Alembic 리비전을
적용한다 — 새 DB 든 옛 DB 든 같은 모양이 되어야 하고, 모델과 마이그레이션 결과가 어긋나면 여기서 잡힌다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from kukie.store import reset_store_for_tests
from kukie.store.models import Base

HEAD = "0002"
# Alembic 이전 create_all 이 만들던 인덱스 — 노트북의 옛 ~/.kukie/kukie.db 에서 그대로 읽은 SQL
OLD_PERSONAL = "CREATE UNIQUE INDEX uq_cluster_personal_name ON tbl_cluster (registered_by, name) WHERE team_id IS NULL"
OLD_TEAM = "CREATE UNIQUE INDEX uq_cluster_team_name ON tbl_cluster (team_id, name) WHERE team_id IS NOT NULL"


def _normalize(kind: str, sql: str) -> str:
    """공백을 접고, CREATE TABLE 은 괄호 안 항목(열·제약)을 정렬한다 — create_all 과 op.create_table 은
    같은 표를 만들면서 FOREIGN KEY 와 UNIQUE 의 순서만 다르게 적는다. 순서는 의미가 없으니 집합으로 본다."""
    flat = " ".join(sql.split())
    if kind != "table":
        return flat
    head, _, rest = flat.partition("(")
    body = rest[: rest.rfind(")")]
    items, depth, current = [], 0, ""
    for ch in body:
        if ch == "," and depth == 0:
            items.append(current.strip()); current = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        current += ch
    items.append(current.strip())
    return f"{head.strip()} ({', '.join(sorted(items))})"


def schema(path: Path) -> dict[tuple[str, str], str]:
    """sqlite_master 를 (종류, 이름) → 정규화한 SQL 로. alembic_version 은 마이그레이션의 부산물이라 뺀다."""
    rows = sqlite3.connect(path).execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
    ).fetchall()
    return {(kind, name): _normalize(kind, sql) for kind, name, sql in rows}


def version(path: Path) -> str | None:
    con = sqlite3.connect(path)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'alembic_version'").fetchone():
        return None
    return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def model_schema(tmp_path: Path) -> dict[tuple[str, str], str]:
    """지금 모델을 create_all 로 만든 모양 — 마이그레이션이 도달해야 하는 목표."""
    path = tmp_path / "model.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return schema(path)


def make_old_db(path: Path, clusters: list[tuple[str, str | None, str, str]]) -> None:
    """Alembic 이전 DB 를 재현한다 — 표는 지금 모델, 인덱스만 옛 조건. clusters = (id, team_id, registered_by, name)."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as con:
        con.execute(text("DROP INDEX uq_cluster_personal_name"))
        con.execute(text("DROP INDEX uq_cluster_team_name"))
        con.execute(text(OLD_PERSONAL))
        con.execute(text(OLD_TEAM))
        for cid, team_id, by, name in clusters:
            con.execute(text(
                "INSERT INTO tbl_cluster (id, team_id, registered_by, name, provider, api_server, insecure, "
                "credential_encrypted, context_name, default_namespace, fingerprint, status, created_at, updated_at) "
                "VALUES (:id, :team_id, :by, :name, 'GENERIC', 'https://x', 0, 'enc', 'ctx', 'default', :id, "
                "'disconnected', '2026-09-01 00:00:00', '2026-09-01 00:00:00')"
            ), {"id": cid, "team_id": team_id, "by": by, "name": name})
    engine.dispose()
    assert schema(path)[("index", "uq_cluster_personal_name")] == OLD_PERSONAL  # 정말 옛 DB 인지
    assert version(path) is None


def team_ids(path: Path) -> dict[str, str | None]:
    return dict(sqlite3.connect(path).execute("SELECT id, team_id FROM tbl_cluster").fetchall())


def test_새_DB는_마이그레이션으로_만들어지고_모델과_모양이_같다(tmp_path):
    db = tmp_path / "fresh.db"
    reset_store_for_tests(f"sqlite:///{db}")

    assert version(db) == HEAD
    # 드리프트 0 — 모델만 고치고 리비전을 안 만들면 여기서 빨개진다
    assert schema(db) == model_schema(tmp_path)


def test_다시_켜도_그대로다(tmp_path):
    db = tmp_path / "fresh.db"
    reset_store_for_tests(f"sqlite:///{db}")
    before = schema(db)

    reset_store_for_tests(f"sqlite:///{db}")

    assert version(db) == HEAD
    assert schema(db) == before


def test_옛_DB는_도장을_찍고_이어서_올린다(tmp_path):
    db = tmp_path / "old.db"
    make_old_db(db, [("a", "", "u1", "운영"), ("b", None, "u1", "스테이징"), ("c", "t1", "u2", "운영")])

    reset_store_for_tests(f"sqlite:///{db}")

    assert version(db) == HEAD
    assert team_ids(db) == {"a": None, "b": None, "c": "t1"}          # '' → NULL, 나머지는 그대로
    assert schema(db) == model_schema(tmp_path)                       # 인덱스 조건이 새 규칙으로


def test_팀_없는_이름이_겹치는_옛_DB는_멈추고_어느_행인지_말한다(tmp_path):
    db = tmp_path / "old.db"
    make_old_db(db, [("a", "", "u1", "운영"), ("b", None, "u1", "운영")])   # 옛 인덱스 아래선 공존했다

    with pytest.raises(RuntimeError, match=r"u1/운영\(2개\)"):
        reset_store_for_tests(f"sqlite:///{db}")

    # 데이터와 인덱스는 손대지 않았다 — 사람이 정리한 뒤 다시 켜면 이어서 간다
    assert team_ids(db) == {"a": "", "b": None}
    assert schema(db)[("index", "uq_cluster_personal_name")] == OLD_PERSONAL
