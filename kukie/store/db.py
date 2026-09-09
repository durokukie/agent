"""엔진·세션 팩토리. 기본은 SQLite(~/.kukie/kukie.db), KUKIE_DATABASE_URL 로 Postgres 등으로 바꾼다.

MVP 는 동기 SQLAlchemy 다. 쿼리가 짧고(행 몇 개) 단일 프로세스라 이벤트 루프를 세우는 시간이 ms 단위다.
run_kubectl 이 동기인 것과 같은 결정 — 다중 사용자가 되면 to_thread 로 감싼다 (server-plan 3-⑤).
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from kukie.store.chat_store import ChatStore
from kukie.store.models import Base

DEFAULT_DB_PATH = Path.home() / ".kukie" / "kukie.db"

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None
_store: ChatStore | None = None


def database_url() -> str:
    url = os.environ.get("KUKIE_DATABASE_URL")
    if url:
        return url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH}"


def _connect(url: str) -> tuple[Engine, sessionmaker[Session]]:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    Base.metadata.create_all(engine)   # Alembic 은 Postgres 로 갈 때. SQLite MVP 는 create_all 로 충분
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def get_store() -> ChatStore:
    """프로세스당 하나. 처음 부를 때 연결한다 (import 시점에 홈 디렉터리를 만들지 않기 위해)."""
    global _engine, _factory, _store
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
