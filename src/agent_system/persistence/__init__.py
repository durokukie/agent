"""SQLite 영속성과 migration의 공개 interface."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL

from ._store import SQLiteStore
from ._values import (
    ApprovalApplyResult,
    ApprovalApplyStatus,
    ApprovalConflictError,
    ApprovalDecisionRecord,
    ApprovalRecord,
    IdempotencyConflictError,
    IdempotencyKey,
    InvalidOutboxTransitionError,
    InvalidPersistenceValueError,
    OptimisticConcurrencyError,
    OutboxDraft,
    OutboxMessage,
    OutboxStatus,
    PersistenceConflictError,
    PersistenceError,
    PersistenceNotFoundError,
    RecoveryCandidate,
    RecoveryDisposition,
    RuntimeCommandDraft,
    RuntimeCommandRecord,
    RuntimeCommandStatus,
    RuntimeCommandType,
    TaskEvent,
    TaskEventDraft,
    TaskWriteResult,
)


def _alembic_config(database_path: Path) -> Config:
    """주입된 SQLite 파일을 대상으로 하는 Alembic 설정을 만든다."""

    migration_directory = Path(__file__).resolve().parent / "migrations"
    config = Config()
    config.set_main_option(
        "script_location", str(migration_directory).replace("%", "%%")
    )
    database_url = URL.create("sqlite", database=str(database_path.resolve()))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


def upgrade_database(database_path: str | Path) -> None:
    """애플리케이션 schema를 현재 Alembic head로 올린다."""

    path = Path(database_path)
    if not path.name:
        raise ValueError("database_path에는 SQLite 파일 경로가 필요합니다.")
    command.upgrade(_alembic_config(path), "head")


__all__ = [
    "ApprovalApplyResult",
    "ApprovalApplyStatus",
    "ApprovalConflictError",
    "ApprovalDecisionRecord",
    "ApprovalRecord",
    "IdempotencyConflictError",
    "IdempotencyKey",
    "InvalidOutboxTransitionError",
    "InvalidPersistenceValueError",
    "OptimisticConcurrencyError",
    "OutboxDraft",
    "OutboxMessage",
    "OutboxStatus",
    "PersistenceConflictError",
    "PersistenceError",
    "PersistenceNotFoundError",
    "RecoveryCandidate",
    "RecoveryDisposition",
    "RuntimeCommandDraft",
    "RuntimeCommandRecord",
    "RuntimeCommandStatus",
    "RuntimeCommandType",
    "SQLiteStore",
    "TaskEvent",
    "TaskEventDraft",
    "TaskWriteResult",
    "upgrade_database",
]
