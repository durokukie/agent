"""엔진·세션 팩토리. 기본은 SQLite(~/.kukie/kukie.db), KUKIE_DATABASE_URL 로 Postgres 등으로 바꾼다.

MVP 는 동기 SQLAlchemy 다. 쿼리가 짧고(행 몇 개) 단일 프로세스라 이벤트 루프를 세우는 시간이 ms 단위다.
run_kubectl 이 동기인 것과 같은 결정 — 다중 사용자가 되면 to_thread 로 감싼다 (server-plan 3-⑤).

표 모양은 켜질 때 Alembic 리비전(kukie/store/migrations/versions)을 순서대로 적용해 맞춘다 (issue #67).
create_all 은 없는 표만 만들어서, 이미 있는 DB 의 열·인덱스 변경이 안 나갔다. 모델을 바꾸면 리비전도 같이 —
tests/test_migrations.py 가 둘이 같은지 대조한다.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from kukie.store.chat_store import ChatStore

DEFAULT_DB_PATH = Path.home() / ".kukie" / "kukie.db"
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
BASELINE_REVISION = "0001"   # Alembic 이전 create_all 이 만들던 모양 그대로 얼려 둔 리비전

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None
_store: ChatStore | None = None
_init_lock = threading.Lock()   # 동기 dependency 는 스레드풀에서 돈다 — 첫 요청 여럿이 create_all 에 동시에 들어오면 안 된다


def database_url() -> str:
    url = os.environ.get("KUKIE_DATABASE_URL")
    if url:
        _reject_memory_sqlite(url)   # CLI(alembic.ini → env.py)도 이 함수를 거친다 — 서버 기동과 같은 문에서 막는다
        return url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH}"


def _alembic_config(connection: Connection) -> Config:
    """alembic.ini 없이 코드로 같은 설정을 만든다 — 서버 이미지에는 ini 가 없고, 이미 연 커넥션을 env.py 에 넘긴다."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def _shape(connection: Connection) -> dict[str, frozenset[str]]:
    """표 이름 → 열 이름 집합. alembic_version 은 마이그레이션의 부산물이라 뺀다 (실패한 첫 시도 뒤 빈 채 남을 수 있다)."""
    inspector = inspect(connection)
    return {
        table: frozenset(column["name"] for column in inspector.get_columns(table))
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }


def _baseline_shape() -> dict[str, frozenset[str]]:
    """0001 이 만드는 표·열 — 빈 메모리 DB 에 0001 만 적용해 읽는다 (모델이 아니라 리비전이 기준)."""
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            command.upgrade(_alembic_config(connection), BASELINE_REVISION)
            return _shape(connection)
    finally:
        engine.dispose()


def _migrate(engine: Engine) -> None:
    """표 모양을 리비전 순서대로 최신(head)으로.

    Alembic 이전에 create_all 로 만든 DB(표는 있는데 리비전 기록이 없음)는, 표·열이 0001 과 같을 때만
    0001 로 도장을 찍고 그 뒤 리비전만 적용한다 — 0001 을 실제로 돌리면 이미 있는 표를 또 만들려다 죽는다.
    표가 덜 만들어졌거나 열이 다른 옛 DB 는 도장을 찍으면 영영 못 낫는 채 남으므로(PR #83 리뷰) 멈추고
    사람이 읽을 메시지를 낸다. 리비전 기록의 유무는 표 존재가 아니라 현재 리비전으로 본다 — 첫 시도가 중간에
    멈추면 SQLite 는 alembic_version 표만 빈 채 남긴다(아래).

    한 번의 기동에서 적용하는 리비전 전부(도장 포함)가 **트랜잭션 하나**다 — env.py 가 넘겨받은 커넥션의
    트랜잭션을 그대로 쓰고 transaction_per_migration 을 켜지 않으므로. 중간에 멈추면 그 기동에서 한 것이 전부
    되돌아가고(0002·0003 이 되고 0004 가 멈추면 셋 다), 다음 기동이 처음부터 다시 한다. SQLite 도 그렇다:
    _migration_engine 이 BEGIN 을 직접 쳐서 DDL 까지 트랜잭션 안에 둔다 (PR #83 리뷰 2차). 그래도 리비전은
    검사 → 변경 순서로 쓴다 — 멈출 거면 아무것도 안 건드리고 멈추는 편이 읽기 쉽다.

    여러 프로세스가 동시에 뜨면(Aurora + 복제본, uvicorn --workers) 같이 upgrade 에 들어와 한쪽이 죽는다.
    Postgres 는 트랜잭션 잠금으로 줄을 세운다. SQLite 는 단일 프로세스 전제(_init_lock 은 프로세스 안쪽만).
    """
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(7378616)"))   # 'kukie' 를 숫자로 — 임의 상수
        config = _alembic_config(connection)
        if MigrationContext.configure(connection).get_current_revision() is None:
            expected = _baseline_shape()
            # kukie 표만 본다 — 남의 표(공유 DB·예전 도구)는 create_all 때처럼 무시한다 (PR #83 리뷰 2차)
            actual = {table: columns for table, columns in _shape(connection).items() if table in expected}
            if actual:   # 리비전 기록 없이 kukie 표가 있다 — Alembic 이전 DB, 또는 첫 기동이 중간에 끊긴 DB
                if actual != expected:
                    missing = sorted(set(expected) - set(actual))
                    columns = sorted(
                        f"{table}({', '.join(sorted(actual[table] ^ expected[table]))})"
                        for table in actual if actual[table] != expected[table]
                    )
                    raise RuntimeError(
                        "리비전 기록이 없는데 kukie 표의 모양이 기준(0001)과 다르다 — Alembic 이전에 만들다 만 DB 거나 "
                        f"첫 기동이 중간에 끊긴 DB 다. 없는 표 {missing or '없음'} · 열이 다른 표 {columns or '없음'}. "
                        "개발용 DB 면 지우고 다시 켜라 (기본 ~/.kukie/kukie.db). 남겨야 할 데이터가 있으면 "
                        "0001 모양에 맞춰 손으로 고친 뒤 다시 켜라"
                    )
                command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")


def _service_engine(url: str) -> Engine:
    """평소 요청이 쓰는 엔진 — 드라이버 기본 동작 그대로."""
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def _migration_engine(url: str) -> Engine:
    """마이그레이션에만 쓰는 엔진. 켜질 때 한 번 쓰고 버린다.

    SQLite 에는 SQLAlchemy 의 pysqlite 레시피를 건다: 드라이버의 암묵 BEGIN(DML 앞에서만, DDL 은 트랜잭션 밖)을
    끄고 트랜잭션을 열 때 BEGIN 을 직접 친다 → DDL 도 롤백된다. 서비스 엔진에는 걸지 않는다 — 걸면 세션의 첫
    SELECT 부터 SHARED 잠금을 쥐어, 같은 세션에서 이어 쓰는 곳이 잠금 승격에서 기다리지 못하고 바로
    `database is locked` 를 맞는다 (PR #83 리뷰 3차).
    """
    if not url.startswith("sqlite"):
        return create_engine(url, future=True)
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)

    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _explicit_begin(connection) -> None:
        connection.exec_driver_sql("BEGIN")

    return engine


def _is_memory_sqlite(url: str) -> bool:
    """sqlite:// · sqlite+pysqlite:// (경로 없음) · …/:memory: · file::memory:?… 전부 — 글자가 아니라 주소를 파싱해 본다."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return False
    database = parsed.database or ""
    return database == "" or ":memory:" in database


def _reject_memory_sqlite(url: str) -> None:
    """엔진이 둘(마이그레이션용·서비스용)이라 메모리 DB 는 서로 다른 DB 를 연다 — 표는 버려지는 쪽에 생기고
    첫 쿼리가 no such table 로 죽는다. 조용히 깨지느니 문에서 막는다 (PR #83 리뷰 4·5차)."""
    if _is_memory_sqlite(url):
        raise ValueError("KUKIE_DATABASE_URL 에 메모리 SQLite(sqlite:// · :memory:)는 쓸 수 없다 — 파일 주소를 써라")


def _connect(url: str) -> tuple[Engine, sessionmaker[Session]]:
    _reject_memory_sqlite(url)
    migration_engine = _migration_engine(url)
    try:
        _migrate(migration_engine)
    finally:
        migration_engine.dispose()
    engine = _service_engine(url)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def get_store() -> ChatStore:
    """프로세스당 하나. 처음 부를 때 연결한다 (import 시점에 홈 디렉터리를 만들지 않기 위해)."""
    global _engine, _factory, _store
    if _store is None:
        with _init_lock:
            if _store is None:
                _engine, _factory = _connect(database_url())
                _store = ChatStore(_factory)
    return _store


def reset_store_for_tests(url: str) -> ChatStore:
    """테스트가 임시 DB 로 갈아끼운다. 운영 코드는 부르지 않는다."""
    global _engine, _factory, _store
    if _engine is not None:
        _engine.dispose()
    _engine, _factory = _connect(url)
    _store = ChatStore(_factory)
    return _store
