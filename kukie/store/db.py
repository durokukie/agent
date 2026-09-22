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
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, Engine
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
        return url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH}"


def _alembic_config(connection: Connection) -> Config:
    """alembic.ini 없이 코드로 같은 설정을 만든다 — 서버 이미지에는 ini 가 없고, 이미 연 커넥션을 env.py 에 넘긴다."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def _migrate(engine: Engine) -> None:
    """표 모양을 리비전 순서대로 최신(head)으로.

    Alembic 이전에 create_all 로 만든 DB(표는 있는데 alembic_version 이 없음)는 그 모양 그대로 얼려 둔
    0001 로 도장만 찍고 그 뒤 리비전만 적용한다 — 0001 을 실제로 돌리면 이미 있는 표를 또 만들려다 죽는다.
    한 트랜잭션으로 묶어, 중간 리비전이 멈추면(예: 0002 의 이름 겹침) 그 앞까지도 남기지 않는다 (SQLite 는
    DDL 이 일부 커밋될 수 있어 완전하진 않지만, 리비전 자체가 검사 → 변경 순서라 데이터는 안 건드린다).
    """
    with engine.begin() as connection:
        config = _alembic_config(connection)
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" not in tables and "tbl_chat_session" in tables:
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")


def _connect(url: str) -> tuple[Engine, sessionmaker[Session]]:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    _migrate(engine)
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
