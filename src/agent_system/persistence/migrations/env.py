"""애플리케이션 table을 위한 Alembic 실행 환경."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from agent_system.persistence._schema import Base

_TARGET_METADATA = Base.metadata


def _run_migrations_offline() -> None:
    """연결 없이 migration SQL을 생성한다."""

    context.configure(
        url=context.config.get_main_option("sqlalchemy.url"),
        target_metadata=_TARGET_METADATA,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_online() -> None:
    """주입된 SQLite 파일에 migration을 적용한다."""

    connectable = engine_from_config(
        context.config.get_section(context.config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("PRAGMA busy_timeout=5000")
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        # PRAGMA에서 시작된 implicit transaction을 migration transaction과 분리한다.
        connection.commit()
        context.configure(connection=connection, target_metadata=_TARGET_METADATA)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    _run_migrations_offline()
else:
    _run_migrations_online()
