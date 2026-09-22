"""Alembic 실행 환경. 두 길로 들어온다.

- 서버 기동: kukie.store.db 가 이미 연 커넥션을 config.attributes["connection"] 으로 넘긴다 (같은 트랜잭션).
- 개발자 CLI(alembic.ini): 커넥션이 없으니 kukie.store.db.database_url() 로 직접 연다.
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from kukie.store.models import Base

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,   # SQLite 는 ALTER 가 빈약하다 — 열 변경은 배치(표 재생성)로
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    from kukie.store.db import database_url  # CLI 에서만 — 순환 import 를 피해 여기서

    engine = create_engine(database_url(), future=True)
    with engine.connect() as connection:
        _run(connection)
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline(--sql) 모드는 쓰지 않는다 — 서버가 켜질 때 직접 적용한다")
run_migrations()
