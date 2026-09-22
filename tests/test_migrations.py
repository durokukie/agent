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
BASELINE = "0001"
# Alembic 이전 create_all 이 만든 실제 DB 의 모양 — 노트북의 옛 ~/.kukie/kukie.db 에서 그대로 뜬 sqlite_master
LEGACY_SCHEMA = Path(__file__).with_name("data") / "legacy_schema_2026-09-22.sql"
OLD_PERSONAL = "CREATE UNIQUE INDEX uq_cluster_personal_name ON tbl_cluster (registered_by, name) WHERE team_id IS NULL"


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


def make_old_db(path: Path, clusters: list[tuple[str, str | None, str, str]], *, drop_table: str | None = None) -> None:
    """Alembic 이전 DB 를 재현한다 — 실제 옛 DB 의 sqlite_master 스냅샷 그대로. clusters = (id, team_id, registered_by, name).
    drop_table 을 주면 그 표가 아직 없던 더 옛 DB (예: tbl_action_plan 이 #58 로 생기기 전)."""
    con0 = sqlite3.connect(path)
    con0.executescript("\n".join(line for line in LEGACY_SCHEMA.read_text().splitlines() if not line.startswith("--")))
    if drop_table:
        con0.execute(f"DROP TABLE {drop_table}")
    con0.commit(); con0.close()
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as con:
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


def test_0001은_실제_옛_DB의_모양과_같다(tmp_path):
    """도장(stamp)이 거짓말이 아니려면 0001 이 만드는 표·인덱스가 옛 create_all 의 결과와 같아야 한다.
    노트북 옛 DB 에서 뜬 스냅샷과, 빈 DB 에 0001 만 적용한 결과를 대조한다."""
    from alembic import command
    from kukie.store.db import _alembic_config

    legacy = tmp_path / "legacy.db"
    make_old_db(legacy, [])
    baseline = tmp_path / "baseline.db"
    engine = create_engine(f"sqlite:///{baseline}")
    with engine.begin() as con:
        command.upgrade(_alembic_config(con), BASELINE)
    engine.dispose()

    assert schema(baseline) == schema(legacy)


def test_표가_덜_만들어진_옛_DB는_도장을_찍지_않고_멈춘다(tmp_path):
    """tbl_action_plan(#58)·tbl_cluster(#61) 이전에 만든 DB. 예전엔 create_all 이 빠진 표를 채웠지만,
    0001 도장을 찍어 버리면 그 표는 영영 안 생긴다 (PR #83 리뷰) — 멈추고 무엇이 없는지 말한다."""
    db = tmp_path / "older.db"
    make_old_db(db, [], drop_table="tbl_action_plan")

    with pytest.raises(RuntimeError, match=r"없는 표 \['tbl_action_plan'\]"):
        reset_store_for_tests(f"sqlite:///{db}")

    assert version(db) is None                        # 도장을 안 찍었다 — 사람이 정리한 뒤 이어서 갈 수 있다
    assert ("table", "tbl_action_plan") not in schema(db)


def test_겹침으로_멈춘_뒤_정리하고_다시_켜면_이어서_간다(tmp_path):
    """SQLite 는 DDL 이 롤백되지 않아 첫 시도가 alembic_version 표만 빈 채 남긴다. 그 표의 존재만 보고
    도장을 건너뛰면 0001 을 처음부터 돌리다 'already exists' 로 죽는다 (PR #83 리뷰) — 현재 리비전으로 본다."""
    db = tmp_path / "old.db"
    make_old_db(db, [("a", "", "u1", "운영"), ("b", None, "u1", "운영")])
    with pytest.raises(RuntimeError):
        reset_store_for_tests(f"sqlite:///{db}")

    con = sqlite3.connect(db)
    con.execute("UPDATE tbl_cluster SET name = '운영-2' WHERE id = 'b'")   # 사람이 겹침을 정리
    con.commit(); con.close()
    reset_store_for_tests(f"sqlite:///{db}")

    assert version(db) == HEAD
    assert team_ids(db) == {"a": None, "b": None}
    assert schema(db) == model_schema(tmp_path)
