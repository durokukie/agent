"""짧은 SQL transaction 뒤에 SQLite 세부사항을 숨기는 store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import URL, Engine, create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_system.notifications import Notification, NotificationChannel
from agent_system.orchestration import (
    AgentRun,
    Approval,
    ApprovalConsumeResult,
    ApprovalConsumeStatus,
    ApprovalResponse,
    FailureCode,
    Phase,
    Status,
    Task,
    WorkflowRun,
)

from ._schema import (
    AgentRunRow,
    ApprovalDecisionRow,
    ApprovalRow,
    OutboxRow,
    RequestIdempotencyRow,
    RuntimeCommandRow,
    TaskEventRow,
    TaskRow,
    WorkflowRunRow,
)
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

_NOTIFICATION_TOPIC = "task.status_changed"


def _json_dump(value: object) -> str:
    """JSON 값을 canonical text로 직렬화한다."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise InvalidPersistenceValueError(
            "payload는 JSON으로 직렬화할 수 있어야 합니다."
        ) from error


def _json_load(value: str) -> dict[str, object]:
    """저장된 JSON object를 복원한다."""

    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise InvalidPersistenceValueError("저장된 payload는 JSON object여야 합니다.")
    return loaded


def _create_sqlite_engine(database_path: Path, *, busy_timeout_ms: int) -> Engine:
    """동일한 SQLite 연결 정책을 가진 SQLAlchemy engine을 만든다."""

    engine = create_engine(
        URL.create("sqlite", database=str(database_path.resolve())),
        connect_args={
            "check_same_thread": False,
            "timeout": busy_timeout_ms / 1000,
        },
    )

    @event.listens_for(engine, "connect")
    def _configure_connection(
        dbapi_connection: sqlite3.Connection,
        _connection_record: Any,
    ) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms:d}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def _task_from_row(row: TaskRow) -> Task:
    return Task.from_snapshot(
        {
            "task_id": row.task_id,
            "input": row.input,
            "status": row.status,
            "version": row.version,
            "plan_hash": row.plan_hash,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _event_from_row(row: TaskEventRow) -> TaskEvent:
    return TaskEvent(
        event_id=row.event_id,
        task_id=row.task_id,
        task_version=row.task_version,
        event_type=row.event_type,
        payload=_json_load(row.payload_json),
        occurred_at=_parse_datetime(row.occurred_at),
    )


def _runtime_command_from_row(
    row: RuntimeCommandRow,
    *,
    replayed: bool = False,
) -> RuntimeCommandRecord:
    return RuntimeCommandRecord(
        command_id=row.command_id,
        task_id=row.task_id,
        command_type=RuntimeCommandType(row.command_type),
        fingerprint=row.fingerprint,
        payload=_json_load(row.payload_json),
        status=RuntimeCommandStatus(row.status),
        attempt_count=row.attempt_count,
        created_at=datetime.fromisoformat(row.created_at),
        updated_at=datetime.fromisoformat(row.updated_at),
        last_error=row.last_error,
        replayed=replayed,
    )


def _outbox_from_row(row: OutboxRow) -> OutboxMessage:
    return OutboxMessage(
        outbox_id=row.outbox_id,
        task_id=row.task_id,
        task_version=row.task_version,
        topic=row.topic,
        payload=_json_load(row.payload_json),
        status=OutboxStatus(row.status),
        attempt_count=row.attempt_count,
        created_at=_parse_datetime(row.created_at),
        updated_at=_parse_datetime(row.updated_at),
        next_attempt_at=_parse_datetime(row.next_attempt_at),
        lease_token=row.lease_token,
        lease_expires_at=(
            None
            if row.lease_expires_at is None
            else _parse_datetime(row.lease_expires_at)
        ),
        delivered_at=(
            None if row.delivered_at is None else _parse_datetime(row.delivered_at)
        ),
        last_error=row.last_error,
    )


def _workflow_from_row(row: WorkflowRunRow) -> WorkflowRun:
    return WorkflowRun.from_snapshot(_json_load(row.snapshot_json))


def _agent_run_from_row(row: AgentRunRow, workflow: WorkflowRun) -> AgentRun:
    return AgentRun.from_snapshot(
        _json_load(row.snapshot_json),
        workflow=workflow,
    )


def _approval_from_row(row: ApprovalRow) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.approval_id,
        decision_id=row.decision_id,
        approval=Approval.from_snapshot(_json_load(row.approval_snapshot_json)),
        consumed_at=_parse_datetime(row.consumed_at),
        result_task=Task.from_snapshot(_json_load(row.result_task_snapshot_json)),
    )


def _approval_decision_from_row(row: ApprovalDecisionRow) -> ApprovalDecisionRecord:
    return ApprovalDecisionRecord(
        decision_id=row.decision_id,
        task_id=row.task_id,
        task_version=row.task_version,
        plan_hash=row.plan_hash,
        response=ApprovalResponse.from_snapshot(_json_load(row.response_snapshot_json)),
        consumed_at=_parse_datetime(row.consumed_at),
        result_task=Task.from_snapshot(_json_load(row.result_task_snapshot_json)),
        failure=None if row.failure is None else FailureCode(row.failure),
    )


def _parse_datetime(value: str) -> Any:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidPersistenceValueError("저장된 시각에 timezone offset이 없습니다.")
    return parsed


class SQLiteStore:
    """Domain snapshot과 원자적 persistence 명령을 제공하는 SQLite adapter."""

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = 5_000):
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise InvalidPersistenceValueError(
                "busy_timeout_ms는 양의 정수여야 합니다."
            )
        self._database_path = Path(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._engine = _create_sqlite_engine(
            self._database_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False)
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """store가 소유한 SQLAlchemy connection pool을 닫는다."""

        if not self._closed:
            self._engine.dispose()
            self._closed = True

    def _session(self) -> Session:
        if self._closed:
            raise RuntimeError("닫힌 SQLiteStore는 사용할 수 없습니다.")
        return self._session_factory()

    @contextmanager
    def open_checkpointer(self) -> Iterator[SqliteSaver]:
        """같은 DB 파일의 별도 raw connection을 소유하는 saver를 연다."""

        if self._closed:
            raise RuntimeError("닫힌 SQLiteStore는 사용할 수 없습니다.")
        connection = sqlite3.connect(
            str(self._database_path.resolve()),
            check_same_thread=False,
            timeout=self._busy_timeout_ms / 1000,
        )
        try:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms:d}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
            checkpointer = SqliteSaver(
                connection,
                serde=JsonPlusSerializer(allowed_msgpack_modules=None),
            )
            checkpointer.setup()
            yield checkpointer
        finally:
            connection.close()

    def create_task(
        self,
        task: Task,
        *,
        event: TaskEventDraft,
        outbox: OutboxDraft | None = None,
        idempotency: IdempotencyKey | None = None,
        command: RuntimeCommandDraft | None = None,
    ) -> TaskWriteResult:
        """최초 Task, event와 선택적 outbox를 한 transaction에 기록한다."""

        if task.status is not Status.RECEIVED or task.version != 1:
            raise InvalidPersistenceValueError(
                "create_task에는 최초 RECEIVED Task가 필요합니다."
            )
        self._reject_public_notification_outbox(outbox)
        event_value = TaskEvent(
            event_id=event.event_id,
            task_id=task.task_id,
            task_version=task.version,
            event_type=event.event_type,
            payload=dict(event.payload),
            occurred_at=event.occurred_at,
        )
        outbox_value = None if outbox is None else self._new_outbox(task, outbox)
        if command is not None and command.task_id != task.task_id:
            raise InvalidPersistenceValueError(
                "Runtime command와 Task의 task_id가 다릅니다."
            )
        try:
            with self._session() as session, session.begin():
                if idempotency is not None:
                    existing_key = session.get(
                        RequestIdempotencyRow,
                        (idempotency.namespace, idempotency.key),
                    )
                    if existing_key is not None:
                        if existing_key.fingerprint != idempotency.fingerprint:
                            raise IdempotencyConflictError(
                                "같은 멱등성 key가 다른 요청에 사용되었습니다."
                            )
                        existing_task = session.get(TaskRow, existing_key.task_id)
                        if existing_task is None:
                            raise InvalidPersistenceValueError(
                                "멱등성 key가 존재하지 않는 Task를 참조합니다."
                            )
                        return TaskWriteResult(
                            task=_task_from_row(existing_task),
                            event=None,
                            outbox=None,
                            replayed=True,
                        )
                session.add(
                    TaskRow(
                        task_id=task.task_id,
                        input=task.input,
                        status=task.status.value,
                        version=task.version,
                        plan_hash=task.plan_hash,
                        created_at=task.created_at.isoformat(),
                        updated_at=task.updated_at.isoformat(),
                    )
                )
                # ORM relationship을 노출하지 않으므로 FK 부모 insert 순서를 명시한다.
                session.flush()
                session.add(
                    TaskEventRow(
                        event_id=event_value.event_id,
                        task_id=event_value.task_id,
                        task_version=event_value.task_version,
                        event_type=event_value.event_type,
                        payload_json=_json_dump(event_value.payload),
                        occurred_at=event_value.occurred_at.isoformat(),
                    )
                )
                if outbox_value is not None:
                    session.add(
                        OutboxRow(
                            outbox_id=outbox_value.outbox_id,
                            task_id=outbox_value.task_id,
                            task_version=outbox_value.task_version,
                            topic=outbox_value.topic,
                            payload_json=_json_dump(outbox_value.payload),
                            status=outbox_value.status.value,
                            attempt_count=outbox_value.attempt_count,
                            created_at=outbox_value.created_at.isoformat(),
                            updated_at=outbox_value.updated_at.isoformat(),
                            next_attempt_at=outbox_value.next_attempt_at.isoformat(),
                            lease_token=outbox_value.lease_token,
                            lease_expires_at=None,
                            delivered_at=None,
                            last_error=outbox_value.last_error,
                        )
                    )
                if command is not None:
                    session.add(self._runtime_command_row(command))
                if idempotency is not None:
                    session.add(
                        RequestIdempotencyRow(
                            namespace=idempotency.namespace,
                            idempotency_key=idempotency.key,
                            fingerprint=idempotency.fingerprint,
                            task_id=task.task_id,
                            created_at=idempotency.created_at.isoformat(),
                        )
                    )
        except IntegrityError as error:
            raise PersistenceConflictError("Task 생성 값이 이미 존재합니다.") from error
        return TaskWriteResult(
            task=task,
            event=event_value,
            outbox=outbox_value,
        )

    def put_runtime_command(
        self,
        command: RuntimeCommandDraft,
        *,
        expected_task: Task,
    ) -> RuntimeCommandRecord:
        """Task snapshot에 결합된 명령을 저장하거나 exact replay를 반환한다."""

        try:
            with self._session() as session, session.begin():
                existing = session.scalar(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.task_id == command.task_id,
                        RuntimeCommandRow.fingerprint == command.fingerprint,
                    )
                )
                if existing is not None:
                    restored = _runtime_command_from_row(existing, replayed=True)
                    if restored.command_type is not command.command_type or dict(
                        restored.payload
                    ) != dict(command.payload):
                        raise PersistenceConflictError(
                            "같은 command fingerprint의 내용이 다릅니다."
                        )
                    return restored
                current_row = session.get(TaskRow, command.task_id)
                if current_row is None or _task_from_row(current_row) != expected_task:
                    raise OptimisticConcurrencyError(
                        "Runtime command의 Task snapshot이 현재 값과 다릅니다."
                    )
                active = session.scalar(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.task_id == command.task_id,
                        RuntimeCommandRow.status == RuntimeCommandStatus.PENDING.value,
                    )
                )
                if active is not None:
                    raise PersistenceConflictError(
                        "Task에 다른 background command가 대기 중입니다."
                    )
                row = self._runtime_command_row(command)
                session.add(row)
                session.flush()
                return _runtime_command_from_row(row)
        except IntegrityError as error:
            raise PersistenceConflictError(
                "Runtime command identity 또는 활성 순서가 충돌했습니다."
            ) from error

    def list_pending_runtime_commands(self) -> tuple[RuntimeCommandRecord, ...]:
        """Startup과 queue pump가 처리할 명령을 생성 순서로 반환한다."""

        statement = select(RuntimeCommandRow).where(
            RuntimeCommandRow.status == RuntimeCommandStatus.PENDING.value
        )
        with self._session() as session, session.begin():
            records = tuple(
                _runtime_command_from_row(row) for row in session.scalars(statement)
            )
        return tuple(
            sorted(
                records,
                key=lambda record: (
                    record.created_at.astimezone(UTC),
                    record.command_id,
                ),
            )
        )

    def list_runtime_commands(
        self,
        task_id: str | None = None,
    ) -> tuple[RuntimeCommandRecord, ...]:
        """Task 필터를 선택해 모든 command history를 반환한다."""

        statement = select(RuntimeCommandRow)
        if task_id is not None:
            statement = statement.where(RuntimeCommandRow.task_id == task_id)
        with self._session() as session, session.begin():
            records = tuple(
                _runtime_command_from_row(row) for row in session.scalars(statement)
            )
        return tuple(
            sorted(records, key=lambda record: (record.created_at, record.command_id))
        )

    def record_runtime_command_failure(
        self,
        command_id: str,
        *,
        error_code: str,
        at: datetime,
    ) -> RuntimeCommandRecord:
        """실패한 명령을 pending으로 유지하고 안정적인 오류만 기록한다."""

        with self._session() as session, session.begin():
            row = session.get(RuntimeCommandRow, command_id)
            if row is None:
                raise PersistenceNotFoundError("Runtime command가 없습니다.")
            if row.status != RuntimeCommandStatus.PENDING.value:
                raise PersistenceConflictError(
                    "완료된 Runtime command는 실패할 수 없습니다."
                )
            row.attempt_count += 1
            row.last_error = error_code
            row.updated_at = at.isoformat()
            session.flush()
            return _runtime_command_from_row(row)

    def complete_runtime_command(
        self,
        command_id: str,
        *,
        at: datetime,
    ) -> RuntimeCommandRecord:
        """성공한 command를 terminal COMPLETED로 전이한다."""

        with self._session() as session, session.begin():
            row = session.get(RuntimeCommandRow, command_id)
            if row is None:
                raise PersistenceNotFoundError("Runtime command가 없습니다.")
            if row.status == RuntimeCommandStatus.COMPLETED.value:
                return _runtime_command_from_row(row, replayed=True)
            row.status = RuntimeCommandStatus.COMPLETED.value
            row.updated_at = at.isoformat()
            row.last_error = None
            session.flush()
            return _runtime_command_from_row(row)

    @staticmethod
    def _runtime_command_row(command: RuntimeCommandDraft) -> RuntimeCommandRow:
        return RuntimeCommandRow(
            command_id=command.command_id,
            task_id=command.task_id,
            command_type=command.command_type.value,
            fingerprint=command.fingerprint,
            payload_json=_json_dump(dict(command.payload)),
            status=RuntimeCommandStatus.PENDING.value,
            attempt_count=0,
            created_at=command.created_at.isoformat(),
            updated_at=command.created_at.isoformat(),
            last_error=None,
        )

    def save_task(
        self,
        task: Task,
        *,
        expected_version: int,
        event: TaskEventDraft,
        outbox: OutboxDraft | None = None,
    ) -> TaskWriteResult:
        """Task의 정확한 다음 version과 event를 optimistic transaction으로 저장한다."""

        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version <= 0
        ):
            raise InvalidPersistenceValueError(
                "expected_version은 양의 정수여야 합니다."
            )
        self._reject_public_notification_outbox(outbox)
        event_value = TaskEvent(
            event_id=event.event_id,
            task_id=task.task_id,
            task_version=task.version,
            event_type=event.event_type,
            payload=dict(event.payload),
            occurred_at=event.occurred_at,
        )
        automatic_outbox = self._notification_draft(task)
        if automatic_outbox is not None:
            if outbox is not None and outbox != automatic_outbox:
                raise InvalidPersistenceValueError(
                    "알림 대상 Task 전이의 notification outbox는 교체할 수 없습니다."
                )
            outbox = automatic_outbox
        outbox_value = None if outbox is None else self._new_outbox(task, outbox)
        try:
            with self._session() as session, session.begin():
                current_row = session.get(TaskRow, task.task_id)
                if current_row is None or current_row.version != expected_version:
                    raise OptimisticConcurrencyError(
                        "저장된 Task version이 expected_version과 다릅니다."
                    )
                current = _task_from_row(current_row)
                self._validate_task_successor(current, task)
                result = session.execute(
                    update(TaskRow)
                    .where(
                        TaskRow.task_id == task.task_id,
                        TaskRow.version == expected_version,
                    )
                    .values(
                        input=task.input,
                        status=task.status.value,
                        version=task.version,
                        plan_hash=task.plan_hash,
                        created_at=task.created_at.isoformat(),
                        updated_at=task.updated_at.isoformat(),
                    )
                )
                if result.rowcount != 1:
                    raise OptimisticConcurrencyError(
                        "Task snapshot이 다른 writer에 의해 변경되었습니다."
                    )
                session.add(
                    TaskEventRow(
                        event_id=event_value.event_id,
                        task_id=event_value.task_id,
                        task_version=event_value.task_version,
                        event_type=event_value.event_type,
                        payload_json=_json_dump(event_value.payload),
                        occurred_at=event_value.occurred_at.isoformat(),
                    )
                )
                if outbox_value is not None:
                    session.add(
                        OutboxRow(
                            outbox_id=outbox_value.outbox_id,
                            task_id=outbox_value.task_id,
                            task_version=outbox_value.task_version,
                            topic=outbox_value.topic,
                            payload_json=_json_dump(outbox_value.payload),
                            status=outbox_value.status.value,
                            attempt_count=outbox_value.attempt_count,
                            created_at=outbox_value.created_at.isoformat(),
                            updated_at=outbox_value.updated_at.isoformat(),
                            next_attempt_at=outbox_value.next_attempt_at.isoformat(),
                            lease_token=outbox_value.lease_token,
                            lease_expires_at=None,
                            delivered_at=None,
                            last_error=outbox_value.last_error,
                        )
                    )
        except IntegrityError as error:
            raise PersistenceConflictError(
                "Task event 또는 outbox 값이 이미 존재합니다."
            ) from error
        return TaskWriteResult(task=task, event=event_value, outbox=outbox_value)

    def get_task(self, task_id: str) -> Task | None:
        """현재 Task domain snapshot을 반환한다."""

        with self._session() as session, session.begin():
            row = session.get(TaskRow, task_id)
            return None if row is None else _task_from_row(row)

    def create_workflow_run(self, workflow: WorkflowRun) -> WorkflowRun:
        """Task 현재 version에 결합된 최초 WorkflowRun snapshot을 저장한다."""

        if workflow.budget.consumed != 0 or workflow.agent_run_issuances:
            raise InvalidPersistenceValueError(
                "create_workflow_run에는 AgentRun 발급 전 snapshot이 필요합니다."
            )
        try:
            with self._session() as session, session.begin():
                task_row = session.get(TaskRow, workflow.task_id)
                if task_row is None:
                    raise PersistenceNotFoundError(
                        f"WorkflowRun의 Task가 없습니다: {workflow.task_id}"
                    )
                task = _task_from_row(task_row)
                if task.version != workflow.task_version:
                    raise OptimisticConcurrencyError(
                        "WorkflowRun의 task_version이 현재 Task와 다릅니다."
                    )
                session.add(
                    WorkflowRunRow(
                        workflow_run_id=workflow.workflow_run_id,
                        task_id=workflow.task_id,
                        snapshot_json=_json_dump(workflow.to_snapshot()),
                        updated_at=workflow.updated_at.isoformat(),
                    )
                )
        except IntegrityError as error:
            raise PersistenceConflictError(
                "WorkflowRun 식별자 또는 Task 소유권이 이미 존재합니다."
            ) from error
        return workflow

    def record_agent_run(
        self,
        previous_workflow: WorkflowRun,
        workflow: WorkflowRun,
        agent_run: AgentRun,
    ) -> tuple[WorkflowRun, AgentRun]:
        """새 issuance ledger와 AgentRun을 한 optimistic transaction에 저장한다."""

        self._validate_issuance_successor(previous_workflow, workflow, agent_run)
        previous_json = _json_dump(previous_workflow.to_snapshot())
        workflow_json = _json_dump(workflow.to_snapshot())
        agent_run_json = _json_dump(agent_run.to_snapshot())
        try:
            with self._session() as session, session.begin():
                result = session.execute(
                    update(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.workflow_run_id == workflow.workflow_run_id,
                        WorkflowRunRow.snapshot_json == previous_json,
                    )
                    .values(
                        snapshot_json=workflow_json,
                        updated_at=workflow.updated_at.isoformat(),
                    )
                )
                if result.rowcount != 1:
                    raise OptimisticConcurrencyError(
                        "저장된 WorkflowRun이 previous_workflow와 다릅니다."
                    )
                session.add(
                    AgentRunRow(
                        agent_run_id=agent_run.agent_run_id,
                        workflow_run_id=agent_run.workflow_run_id,
                        task_id=agent_run.task_id,
                        budget_sequence=agent_run.budget_sequence,
                        snapshot_json=agent_run_json,
                        started_at=agent_run.started_at.isoformat(),
                    )
                )
        except IntegrityError as error:
            raise PersistenceConflictError(
                "AgentRun identity 또는 budget sequence가 이미 존재합니다."
            ) from error
        return workflow, agent_run

    def save_workflow_run(
        self,
        previous_workflow: WorkflowRun,
        workflow: WorkflowRun,
    ) -> WorkflowRun:
        """issuance 변경 없는 정확한 다음 phase snapshot을 저장한다."""

        expected = previous_workflow.advance(
            workflow.phase,
            at=workflow.updated_at,
        )
        if expected != workflow:
            raise InvalidPersistenceValueError(
                "workflow가 previous_workflow의 정확한 다음 phase가 아닙니다."
            )
        previous_json = _json_dump(previous_workflow.to_snapshot())
        with self._session() as session, session.begin():
            result = session.execute(
                update(WorkflowRunRow)
                .where(
                    WorkflowRunRow.workflow_run_id == workflow.workflow_run_id,
                    WorkflowRunRow.snapshot_json == previous_json,
                )
                .values(
                    snapshot_json=_json_dump(workflow.to_snapshot()),
                    updated_at=workflow.updated_at.isoformat(),
                )
            )
            if result.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "저장된 WorkflowRun이 previous_workflow와 다릅니다."
                )
        return workflow

    def complete_agent_run(self, agent_run: AgentRun) -> AgentRun:
        """열린 AgentRun row를 완료 snapshot으로 optimistic하게 갱신한다."""

        if not agent_run.is_completed:
            raise InvalidPersistenceValueError(
                "complete_agent_run에는 완료된 AgentRun이 필요합니다."
            )
        with self._session() as session, session.begin():
            row = session.get(AgentRunRow, agent_run.agent_run_id)
            if row is None:
                raise PersistenceNotFoundError(
                    f"AgentRun이 없습니다: {agent_run.agent_run_id}"
                )
            workflow_row = session.get(WorkflowRunRow, row.workflow_run_id)
            if workflow_row is None:
                raise InvalidPersistenceValueError(
                    "AgentRun의 소유 WorkflowRun이 없습니다."
                )
            workflow = _workflow_from_row(workflow_row)
            candidate = AgentRun.from_snapshot(
                agent_run.to_snapshot(),
                workflow=workflow,
            )
            current = _agent_run_from_row(row, workflow)
            if current.is_completed:
                if current == candidate:
                    return current
                raise OptimisticConcurrencyError(
                    "AgentRun이 다른 완료 결과로 이미 저장되었습니다."
                )
            current_identity = current.to_snapshot() | {
                "outcome": candidate.outcome,
                "output": candidate.output,
                "completed_at": (
                    None
                    if candidate.completed_at is None
                    else candidate.completed_at.isoformat()
                ),
            }
            if current_identity != candidate.to_snapshot():
                raise InvalidPersistenceValueError(
                    "완료 결과가 저장된 AgentRun identity를 변경했습니다."
                )
            result = session.execute(
                update(AgentRunRow)
                .where(
                    AgentRunRow.agent_run_id == agent_run.agent_run_id,
                    AgentRunRow.snapshot_json == row.snapshot_json,
                )
                .values(snapshot_json=_json_dump(candidate.to_snapshot()))
            )
            if result.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "AgentRun이 다른 writer에 의해 변경되었습니다."
                )
            return candidate

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun | None:
        """현재 WorkflowRun domain snapshot을 반환한다."""

        with self._session() as session, session.begin():
            row = session.get(WorkflowRunRow, workflow_run_id)
            return None if row is None else _workflow_from_row(row)

    def get_workflow_run_for_task(self, task_id: str) -> WorkflowRun | None:
        """Task가 소유한 현재 WorkflowRun snapshot을 반환한다."""

        statement = select(WorkflowRunRow).where(WorkflowRunRow.task_id == task_id)
        with self._session() as session, session.begin():
            row = session.scalar(statement)
            return None if row is None else _workflow_from_row(row)

    def rebind_workflow_run(
        self,
        previous_workflow: WorkflowRun,
        workflow: WorkflowRun,
    ) -> WorkflowRun:
        """승인 successor에 결합된 exact WorkflowRun rebind를 저장한다."""

        previous_json = _json_dump(previous_workflow.to_snapshot())
        with self._session() as session, session.begin():
            task_row = session.get(TaskRow, workflow.task_id)
            if task_row is None:
                raise PersistenceNotFoundError(
                    f"WorkflowRun의 Task가 없습니다: {workflow.task_id}"
                )
            expected = previous_workflow.rebind_task(
                _task_from_row(task_row),
                at=workflow.updated_at,
            )
            if expected != workflow:
                raise InvalidPersistenceValueError(
                    "workflow가 현재 승인 Task의 exact rebind 결과가 아닙니다."
                )
            result = session.execute(
                update(WorkflowRunRow)
                .where(
                    WorkflowRunRow.workflow_run_id == workflow.workflow_run_id,
                    WorkflowRunRow.snapshot_json == previous_json,
                )
                .values(
                    snapshot_json=_json_dump(workflow.to_snapshot()),
                    updated_at=workflow.updated_at.isoformat(),
                )
            )
            if result.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "저장된 WorkflowRun이 previous_workflow와 다릅니다."
                )
        return workflow

    def get_agent_run(self, agent_run_id: str) -> AgentRun | None:
        """저장된 owner 검증과 함께 AgentRun domain snapshot을 반환한다."""

        with self._session() as session, session.begin():
            row = session.get(AgentRunRow, agent_run_id)
            if row is None:
                return None
            workflow_row = session.get(WorkflowRunRow, row.workflow_run_id)
            if workflow_row is None:
                raise InvalidPersistenceValueError(
                    "AgentRun의 소유 WorkflowRun이 없습니다."
                )
            return _agent_run_from_row(row, _workflow_from_row(workflow_row))

    def list_agent_runs(self, workflow_run_id: str) -> tuple[AgentRun, ...]:
        """한 WorkflowRun의 모든 AgentRun을 issuance 순서로 복원한다."""

        statement = (
            select(AgentRunRow)
            .where(AgentRunRow.workflow_run_id == workflow_run_id)
            .order_by(AgentRunRow.budget_sequence)
        )
        with self._session() as session, session.begin():
            workflow_row = session.get(WorkflowRunRow, workflow_run_id)
            if workflow_row is None:
                return ()
            workflow = _workflow_from_row(workflow_row)
            return tuple(
                _agent_run_from_row(row, workflow) for row in session.scalars(statement)
            )

    def list_task_events(self, task_id: str) -> tuple[TaskEvent, ...]:
        """Task event를 version 순서로 반환한다."""

        statement = (
            select(TaskEventRow)
            .where(TaskEventRow.task_id == task_id)
            .order_by(TaskEventRow.task_version)
        )
        with self._session() as session, session.begin():
            return tuple(_event_from_row(row) for row in session.scalars(statement))

    def apply_approval(
        self,
        approval: Approval,
        *,
        decision_id: str,
        resumed_at: datetime,
        event: TaskEventDraft,
        outbox: OutboxDraft | None = None,
    ) -> ApprovalApplyResult:
        """Approval binding 검증, 소비와 Task 재개를 한 transaction으로 수행한다."""

        if not isinstance(decision_id, str) or not decision_id.strip():
            raise InvalidPersistenceValueError("decision_id는 비어 있을 수 없습니다.")
        approval_snapshot = approval.to_snapshot()
        approval_json = _json_dump(approval_snapshot)
        approval_id = sha256(f"{approval.task_id}\0{decision_id}".encode()).hexdigest()
        try:
            with self._session() as session, session.begin():
                decision_statement = select(ApprovalRow).where(
                    ApprovalRow.task_id == approval.task_id,
                    ApprovalRow.decision_id == decision_id,
                )
                existing_decision = session.scalar(decision_statement)
                if existing_decision is not None:
                    if existing_decision.approval_snapshot_json != approval_json:
                        raise ApprovalConflictError(
                            "같은 decision_id가 다른 Approval에 사용되었습니다."
                        )
                    return self._approval_replay(existing_decision)

                binding_statement = select(ApprovalRow).where(
                    ApprovalRow.task_id == approval.task_id,
                    ApprovalRow.task_version == approval.task_version,
                    ApprovalRow.plan_hash == approval.plan_hash,
                )
                existing_binding = session.scalar(binding_statement)
                if existing_binding is not None:
                    return self._approval_replay(existing_binding)

                task_row = session.get(TaskRow, approval.task_id)
                if task_row is None:
                    raise PersistenceNotFoundError(
                        f"Approval의 Task가 없습니다: {approval.task_id}"
                    )
                current = _task_from_row(task_row)
                resumed = current.transition(
                    Status.RUNNING,
                    at=resumed_at,
                    approval=approval,
                )
                event_value = TaskEvent(
                    event_id=event.event_id,
                    task_id=resumed.task_id,
                    task_version=resumed.version,
                    event_type=event.event_type,
                    payload=dict(event.payload),
                    occurred_at=event.occurred_at,
                )
                outbox_value = (
                    None if outbox is None else self._new_outbox(resumed, outbox)
                )
                result = session.execute(
                    update(TaskRow)
                    .where(
                        TaskRow.task_id == approval.task_id,
                        TaskRow.status == Status.WAITING_APPROVAL.value,
                        TaskRow.version == approval.task_version,
                        TaskRow.plan_hash == approval.plan_hash,
                    )
                    .values(
                        status=resumed.status.value,
                        version=resumed.version,
                        updated_at=resumed.updated_at.isoformat(),
                    )
                )
                if result.rowcount != 1:
                    raise OptimisticConcurrencyError(
                        "Approval binding과 현재 Task snapshot이 다릅니다."
                    )
                record = ApprovalRecord(
                    approval_id=approval_id,
                    decision_id=decision_id,
                    approval=approval,
                    consumed_at=resumed_at,
                    result_task=resumed,
                )
                session.add(
                    ApprovalRow(
                        approval_id=record.approval_id,
                        decision_id=record.decision_id,
                        task_id=approval.task_id,
                        task_version=approval.task_version,
                        plan_hash=approval.plan_hash,
                        approval_snapshot_json=approval_json,
                        consumed_at=resumed_at.isoformat(),
                        result_task_snapshot_json=_json_dump(resumed.to_snapshot()),
                    )
                )
                session.add(
                    TaskEventRow(
                        event_id=event_value.event_id,
                        task_id=event_value.task_id,
                        task_version=event_value.task_version,
                        event_type=event_value.event_type,
                        payload_json=_json_dump(event_value.payload),
                        occurred_at=event_value.occurred_at.isoformat(),
                    )
                )
                if outbox_value is not None:
                    session.add(
                        OutboxRow(
                            outbox_id=outbox_value.outbox_id,
                            task_id=outbox_value.task_id,
                            task_version=outbox_value.task_version,
                            topic=outbox_value.topic,
                            payload_json=_json_dump(outbox_value.payload),
                            status=outbox_value.status.value,
                            attempt_count=outbox_value.attempt_count,
                            created_at=outbox_value.created_at.isoformat(),
                            updated_at=outbox_value.updated_at.isoformat(),
                            next_attempt_at=outbox_value.next_attempt_at.isoformat(),
                            lease_token=outbox_value.lease_token,
                            lease_expires_at=None,
                            delivered_at=None,
                            last_error=outbox_value.last_error,
                        )
                    )
                return ApprovalApplyResult(
                    task=resumed,
                    status=ApprovalApplyStatus.APPLIED,
                    record=record,
                    event=event_value,
                    outbox=outbox_value,
                )
        except IntegrityError as error:
            raise PersistenceConflictError(
                "Approval, Task event 또는 outbox 값이 이미 존재합니다."
            ) from error

    def list_approvals(self, task_id: str) -> tuple[ApprovalRecord, ...]:
        """Task에 소비된 Approval을 소비 시각 순서로 반환한다."""

        statement = select(ApprovalRow).where(ApprovalRow.task_id == task_id)
        with self._session() as session, session.begin():
            records = tuple(
                _approval_from_row(row) for row in session.scalars(statement)
            )
            return tuple(
                sorted(
                    records,
                    key=lambda record: (
                        record.consumed_at.astimezone(UTC),
                        record.approval_id,
                    ),
                )
            )

    def consume_approval(
        self,
        *,
        task: Task,
        response: ApprovalResponse,
        at: datetime,
        event: TaskEventDraft,
    ) -> ApprovalConsumeResult:
        """승인·거절 응답과 exact Task successor를 원자적으로 저장한다."""

        response_json = _json_dump(response.to_snapshot())
        try:
            with self._session() as session, session.begin():
                existing = session.get(ApprovalDecisionRow, response.decision_id)
                if existing is not None:
                    record = _approval_decision_from_row(existing)
                    if existing.response_snapshot_json != response_json or (
                        record.task_id,
                        record.task_version,
                        record.plan_hash,
                    ) != (task.task_id, task.version, task.plan_hash):
                        raise ApprovalConflictError(
                            "같은 decision_id가 다른 응답 또는 binding에 "
                            "사용되었습니다."
                        )
                    return ApprovalConsumeResult(
                        ApprovalConsumeStatus.ALREADY_APPLIED,
                        record.result_task,
                        record.failure,
                    )

                if task.plan_hash is None:
                    raise ApprovalConflictError(
                        "Approval decision에는 plan binding이 필요합니다."
                    )
                binding_statement = select(ApprovalDecisionRow).where(
                    ApprovalDecisionRow.task_id == task.task_id,
                    ApprovalDecisionRow.task_version == task.version,
                    ApprovalDecisionRow.plan_hash == task.plan_hash,
                )
                if session.scalar(binding_statement) is not None:
                    raise ApprovalConflictError(
                        "Approval binding이 다른 decision으로 이미 소비되었습니다."
                    )
                current_row = session.get(TaskRow, task.task_id)
                if current_row is None:
                    raise PersistenceNotFoundError(
                        f"Approval의 Task가 없습니다: {task.task_id}"
                    )
                current = _task_from_row(current_row)
                if current != task or current.status is not Status.WAITING_APPROVAL:
                    raise OptimisticConcurrencyError(
                        "Approval binding과 현재 Task snapshot이 다릅니다."
                    )
                if response.accepted:
                    approval = response.approval
                    if approval is None or approval.binding != (
                        current.task_id,
                        current.version,
                        current.plan_hash,
                    ):
                        raise ApprovalConflictError(
                            "승인 응답의 binding이 Task snapshot과 다릅니다."
                        )
                    successor = current.transition(
                        Status.RUNNING,
                        at=at,
                        approval=approval,
                    )
                    failure = None
                else:
                    successor = current.transition(Status.REJECTED, at=at)
                    failure = FailureCode.HUMAN_REJECTED
                event_value = TaskEvent(
                    event_id=event.event_id,
                    task_id=successor.task_id,
                    task_version=successor.version,
                    event_type=event.event_type,
                    payload=dict(event.payload),
                    occurred_at=event.occurred_at,
                )
                result = session.execute(
                    update(TaskRow)
                    .where(
                        TaskRow.task_id == current.task_id,
                        TaskRow.status == Status.WAITING_APPROVAL.value,
                        TaskRow.version == current.version,
                        TaskRow.plan_hash == current.plan_hash,
                    )
                    .values(
                        status=successor.status.value,
                        version=successor.version,
                        updated_at=successor.updated_at.isoformat(),
                    )
                )
                if result.rowcount != 1:
                    raise OptimisticConcurrencyError(
                        "Approval binding과 현재 Task snapshot이 다릅니다."
                    )
                session.add(
                    ApprovalDecisionRow(
                        decision_id=response.decision_id,
                        task_id=current.task_id,
                        task_version=current.version,
                        plan_hash=current.plan_hash,
                        response_snapshot_json=response_json,
                        consumed_at=at.isoformat(),
                        result_task_snapshot_json=_json_dump(successor.to_snapshot()),
                        failure=None if failure is None else failure.value,
                    )
                )
                session.add(
                    TaskEventRow(
                        event_id=event_value.event_id,
                        task_id=event_value.task_id,
                        task_version=event_value.task_version,
                        event_type=event_value.event_type,
                        payload_json=_json_dump(event_value.payload),
                        occurred_at=event_value.occurred_at.isoformat(),
                    )
                )
                outbox_draft = self._notification_draft(successor)
                if outbox_draft is not None:
                    outbox_value = self._new_outbox(successor, outbox_draft)
                    session.add(
                        OutboxRow(
                            outbox_id=outbox_value.outbox_id,
                            task_id=outbox_value.task_id,
                            task_version=outbox_value.task_version,
                            topic=outbox_value.topic,
                            payload_json=_json_dump(outbox_value.payload),
                            status=outbox_value.status.value,
                            attempt_count=outbox_value.attempt_count,
                            created_at=outbox_value.created_at.isoformat(),
                            updated_at=outbox_value.updated_at.isoformat(),
                            next_attempt_at=outbox_value.next_attempt_at.isoformat(),
                            lease_token=None,
                            lease_expires_at=None,
                            delivered_at=None,
                            last_error=None,
                        )
                    )
                return ApprovalConsumeResult(
                    ApprovalConsumeStatus.APPLIED,
                    successor,
                    failure,
                )
        except IntegrityError as error:
            raise PersistenceConflictError(
                "Approval decision 또는 Task event 값이 이미 존재합니다."
            ) from error

    def list_approval_decisions(
        self, task_id: str
    ) -> tuple[ApprovalDecisionRecord, ...]:
        """Task에 소비된 승인·거절 결정을 소비 시각 순서로 반환한다."""

        statement = select(ApprovalDecisionRow).where(
            ApprovalDecisionRow.task_id == task_id
        )
        with self._session() as session, session.begin():
            records = tuple(
                _approval_decision_from_row(row) for row in session.scalars(statement)
            )
            return tuple(
                sorted(
                    records,
                    key=lambda record: (
                        record.consumed_at.astimezone(UTC),
                        record.decision_id,
                    ),
                )
            )

    def list_recovery_candidates(self) -> tuple[RecoveryCandidate, ...]:
        """terminal Task를 제외하고 자동 재개와 승인 대기를 구분한다."""

        terminal_statuses = {
            Status.COMPLETED.value,
            Status.REJECTED.value,
            Status.FAILED.value,
            Status.CANCELLED.value,
            Status.ESCALATED.value,
        }
        task_statement = select(TaskRow).where(TaskRow.status.not_in(terminal_statuses))
        with self._session() as session, session.begin():
            workflow_by_task = {
                row.task_id: _workflow_from_row(row)
                for row in session.scalars(select(WorkflowRunRow))
            }
            candidates = []
            for row in session.scalars(task_statement):
                task = _task_from_row(row)
                workflow = workflow_by_task.get(task.task_id)
                disposition = (
                    RecoveryDisposition.WAITING_APPROVAL
                    if task.status is Status.WAITING_APPROVAL
                    else RecoveryDisposition.RESUME
                )
                candidates.append(
                    RecoveryCandidate(
                        task=task,
                        workflow_run=workflow,
                        disposition=disposition,
                        # Runtime은 최초 수락부터 checkpoint까지 Task ID를
                        # stable thread identity로 사용한다.
                        thread_id=task.task_id,
                    )
                )
            return tuple(
                sorted(
                    candidates,
                    key=lambda candidate: (
                        candidate.task.created_at.astimezone(UTC),
                        candidate.task.task_id,
                    ),
                )
            )

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
    ) -> tuple[OutboxMessage, ...]:
        """요청한 상태의 outbox snapshot을 생성 순서로 반환한다."""

        statement = select(OutboxRow)
        if status is not None:
            statement = statement.where(OutboxRow.status == status.value)
        with self._session() as session, session.begin():
            messages = tuple(
                _outbox_from_row(row) for row in session.scalars(statement)
            )
            return tuple(
                sorted(
                    messages,
                    key=lambda message: (
                        message.created_at.astimezone(UTC),
                        message.outbox_id,
                    ),
                )
            )

    def claim_outbox(
        self,
        *,
        now: datetime,
        lease_duration: timedelta,
        lease_token: str,
        topic: str | None = None,
    ) -> OutboxMessage | None:
        """가장 오래된 eligible outbox 한 건을 lease CAS로 claim한다."""

        self._validate_outbox_time(now, field_name="now")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise InvalidPersistenceValueError("lease_duration은 양수여야 합니다.")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise InvalidPersistenceValueError("lease_token은 비어 있을 수 없습니다.")
        if topic is not None and (not isinstance(topic, str) or not topic.strip()):
            raise InvalidPersistenceValueError("topic은 비어 있을 수 없습니다.")
        expires_at = now + lease_duration
        active_statuses = {
            OutboxStatus.PENDING.value,
            OutboxStatus.FAILED.value,
            OutboxStatus.PROCESSING.value,
        }
        with self._session() as session, session.begin():
            statement = select(OutboxRow).where(OutboxRow.status.in_(active_statuses))
            if topic is not None:
                statement = statement.where(OutboxRow.topic == topic)
            candidates = tuple(session.scalars(statement))
            eligible = [
                row for row in candidates if self._outbox_is_eligible(row, now=now)
            ]
            if not eligible:
                return None
            row = min(
                eligible,
                key=lambda candidate: (
                    _parse_datetime(candidate.next_attempt_at).astimezone(UTC),
                    _parse_datetime(candidate.created_at).astimezone(UTC),
                    candidate.outbox_id,
                ),
            )
            previous_status = row.status
            previous_token = row.lease_token
            conditions = [
                OutboxRow.outbox_id == row.outbox_id,
                OutboxRow.status == previous_status,
            ]
            if previous_token is None:
                conditions.append(OutboxRow.lease_token.is_(None))
            else:
                conditions.append(OutboxRow.lease_token == previous_token)
            result = session.execute(
                update(OutboxRow)
                .where(*conditions)
                .values(
                    status=OutboxStatus.PROCESSING.value,
                    attempt_count=row.attempt_count + 1,
                    updated_at=now.isoformat(),
                    lease_token=lease_token,
                    lease_expires_at=expires_at.isoformat(),
                    delivered_at=None,
                )
            )
            if result.rowcount != 1:
                return None
            session.flush()
            session.refresh(row)
            return _outbox_from_row(row)

    def mark_outbox_delivered(
        self,
        outbox_id: str,
        *,
        lease_token: str,
        at: datetime,
    ) -> OutboxMessage:
        """현재 lease owner만 outbox 전달 성공을 확정한다."""

        self._validate_outbox_identity(outbox_id, lease_token)
        self._validate_outbox_time(at, field_name="at")
        with self._session() as session, session.begin():
            row = session.get(OutboxRow, outbox_id)
            self._require_outbox_lease(row, lease_token=lease_token, at=at)
            assert row is not None
            result = session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.outbox_id == outbox_id,
                    OutboxRow.status == OutboxStatus.PROCESSING.value,
                    OutboxRow.lease_token == lease_token,
                )
                .values(
                    status=OutboxStatus.DELIVERED.value,
                    updated_at=at.isoformat(),
                    lease_token=None,
                    lease_expires_at=None,
                    delivered_at=at.isoformat(),
                    last_error=None,
                )
            )
            if result.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "outbox lease가 다른 dispatcher에 의해 변경되었습니다."
                )
            session.flush()
            session.refresh(row)
            return _outbox_from_row(row)

    def renew_outbox_lease(
        self,
        outbox_id: str,
        *,
        lease_token: str,
        at: datetime,
        lease_duration: timedelta,
    ) -> OutboxMessage:
        """현재 owner의 active outbox lease 만료 시각을 CAS로 연장한다."""

        self._validate_outbox_identity(outbox_id, lease_token)
        self._validate_outbox_time(at, field_name="at")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise InvalidPersistenceValueError("lease_duration은 양수여야 합니다.")
        expires_at = at + lease_duration
        with self._session() as session, session.begin():
            row = session.get(OutboxRow, outbox_id)
            self._require_outbox_lease(row, lease_token=lease_token, at=at)
            assert row is not None
            result = session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.outbox_id == outbox_id,
                    OutboxRow.status == OutboxStatus.PROCESSING.value,
                    OutboxRow.lease_token == lease_token,
                )
                .values(
                    updated_at=at.isoformat(),
                    lease_expires_at=expires_at.isoformat(),
                )
            )
            if result.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "outbox lease가 다른 dispatcher에 의해 변경되었습니다."
                )
            session.flush()
            session.refresh(row)
            return _outbox_from_row(row)

    def record_outbox_failure(
        self,
        outbox_id: str,
        *,
        lease_token: str,
        error_code: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> OutboxMessage:
        """전달 실패와 다음 eligibility를 기록하고 Task에는 손대지 않는다."""

        self._validate_outbox_identity(outbox_id, lease_token)
        if not isinstance(error_code, str) or not error_code.strip():
            raise InvalidPersistenceValueError("error_code는 비어 있을 수 없습니다.")
        if len(error_code) > 100:
            raise InvalidPersistenceValueError("error_code는 100자 이하여야 합니다.")
        self._validate_outbox_time(at, field_name="at")
        self._validate_outbox_time(next_attempt_at, field_name="next_attempt_at")
        if next_attempt_at.astimezone(UTC) <= at.astimezone(UTC):
            raise InvalidPersistenceValueError(
                "next_attempt_at은 실패 시각보다 뒤여야 합니다."
            )
        with self._session() as session, session.begin():
            row = session.get(OutboxRow, outbox_id)
            self._require_outbox_lease(row, lease_token=lease_token, at=at)
            assert row is not None
            result = session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.outbox_id == outbox_id,
                    OutboxRow.status == OutboxStatus.PROCESSING.value,
                    OutboxRow.lease_token == lease_token,
                )
                .values(
                    status=OutboxStatus.FAILED.value,
                    updated_at=at.isoformat(),
                    next_attempt_at=next_attempt_at.isoformat(),
                    lease_token=None,
                    lease_expires_at=None,
                    delivered_at=None,
                    last_error=error_code,
                )
            )
            if result.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "outbox lease가 다른 dispatcher에 의해 변경되었습니다."
                )
            session.flush()
            session.refresh(row)
            return _outbox_from_row(row)

    def transition_outbox(
        self,
        outbox_id: str,
        *,
        expected_status: OutboxStatus,
        target: OutboxStatus,
        at: datetime,
        last_error: str | None = None,
    ) -> OutboxMessage:
        """outbox snapshot을 허용된 다음 상태로 optimistic하게 전이한다."""

        if OutboxStatus.PROCESSING in {expected_status, target}:
            raise InvalidOutboxTransitionError(
                "PROCESSING 전이와 확정은 lease claim interface를 사용해야 합니다."
            )
        allowed = {
            OutboxStatus.PENDING: frozenset(),
            OutboxStatus.PROCESSING: frozenset(),
            OutboxStatus.FAILED: frozenset({OutboxStatus.PENDING}),
            OutboxStatus.DELIVERED: frozenset(),
        }
        if target not in allowed[expected_status]:
            raise InvalidOutboxTransitionError(
                f"허용되지 않은 outbox 전이입니다: {expected_status} -> {target}"
            )
        if at.tzinfo is None or at.utcoffset() is None:
            raise InvalidPersistenceValueError(
                "at에는 timezone-aware datetime이 필요합니다."
            )
        if target is OutboxStatus.FAILED:
            if not isinstance(last_error, str) or not last_error.strip():
                raise InvalidPersistenceValueError(
                    "FAILED outbox에는 last_error가 필요합니다."
                )
        elif last_error is not None:
            raise InvalidPersistenceValueError(
                "last_error는 FAILED 전이에만 기록할 수 있습니다."
            )
        with self._session() as session, session.begin():
            row = session.get(OutboxRow, outbox_id)
            if row is None:
                raise PersistenceNotFoundError(f"outbox가 없습니다: {outbox_id}")
            if row.topic == _NOTIFICATION_TOPIC:
                raise InvalidPersistenceValueError(
                    "notification topic은 lease 전용 interface로만 다시 시도할 수 있습니다."
                )
            current_status = OutboxStatus(row.status)
            if current_status is not expected_status:
                raise OptimisticConcurrencyError(
                    "저장된 outbox status가 expected_status와 다릅니다."
                )
            previous_updated_at = _parse_datetime(row.updated_at)
            if at.astimezone(UTC) < previous_updated_at.astimezone(UTC):
                raise InvalidPersistenceValueError(
                    "outbox 전이 시각은 이전 snapshot보다 빠를 수 없습니다."
                )
            attempt_count = row.attempt_count + (
                1 if target is OutboxStatus.PROCESSING else 0
            )
            persisted_error = last_error if target is OutboxStatus.FAILED else None
            result = session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.outbox_id == outbox_id,
                    OutboxRow.status == expected_status.value,
                )
                .values(
                    status=target.value,
                    attempt_count=attempt_count,
                    updated_at=at.isoformat(),
                    last_error=persisted_error,
                )
            )
            if result.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "outbox snapshot이 다른 writer에 의해 변경되었습니다."
                )
            return OutboxMessage(
                outbox_id=row.outbox_id,
                task_id=row.task_id,
                task_version=row.task_version,
                topic=row.topic,
                payload=_json_load(row.payload_json),
                status=target,
                attempt_count=attempt_count,
                created_at=_parse_datetime(row.created_at),
                updated_at=at,
                next_attempt_at=_parse_datetime(row.next_attempt_at),
                lease_token=row.lease_token,
                lease_expires_at=(
                    None
                    if row.lease_expires_at is None
                    else _parse_datetime(row.lease_expires_at)
                ),
                delivered_at=(
                    None
                    if row.delivered_at is None
                    else _parse_datetime(row.delivered_at)
                ),
                last_error=persisted_error,
            )

    @staticmethod
    def _new_outbox(task: Task, draft: OutboxDraft) -> OutboxMessage:
        return OutboxMessage(
            outbox_id=draft.outbox_id,
            task_id=task.task_id,
            task_version=(task.version if draft.topic == _NOTIFICATION_TOPIC else None),
            topic=draft.topic,
            payload=dict(draft.payload),
            status=OutboxStatus.PENDING,
            attempt_count=0,
            created_at=draft.created_at,
            updated_at=draft.created_at,
            next_attempt_at=draft.created_at,
            lease_token=None,
            lease_expires_at=None,
            delivered_at=None,
            last_error=None,
        )

    @staticmethod
    def _outbox_is_eligible(row: OutboxRow, *, now: datetime) -> bool:
        current = now.astimezone(UTC)
        status = OutboxStatus(row.status)
        if status in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            return _parse_datetime(row.next_attempt_at).astimezone(UTC) <= current
        if status is OutboxStatus.PROCESSING:
            return row.lease_expires_at is not None and (
                _parse_datetime(row.lease_expires_at).astimezone(UTC) <= current
            )
        return False

    @staticmethod
    def _validate_outbox_time(value: datetime, *, field_name: str) -> None:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise InvalidPersistenceValueError(
                f"{field_name}에는 timezone-aware datetime이 필요합니다."
            )

    @staticmethod
    def _validate_outbox_identity(outbox_id: str, lease_token: str) -> None:
        if not isinstance(outbox_id, str) or not outbox_id.strip():
            raise InvalidPersistenceValueError("outbox_id는 비어 있을 수 없습니다.")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise InvalidPersistenceValueError("lease_token은 비어 있을 수 없습니다.")

    @staticmethod
    def _require_outbox_lease(
        row: OutboxRow | None,
        *,
        lease_token: str,
        at: datetime,
    ) -> None:
        if row is None:
            raise PersistenceNotFoundError("outbox가 없습니다.")
        if (
            row.status != OutboxStatus.PROCESSING.value
            or row.lease_token != lease_token
        ):
            raise OptimisticConcurrencyError(
                "현재 dispatcher가 outbox lease를 소유하지 않습니다."
            )
        if row.lease_expires_at is None or (
            _parse_datetime(row.lease_expires_at).astimezone(UTC) < at.astimezone(UTC)
        ):
            raise OptimisticConcurrencyError("outbox lease가 만료되었습니다.")

    @staticmethod
    def _notification_draft(task: Task) -> OutboxDraft | None:
        notified_statuses = {
            Status.WAITING_APPROVAL,
            Status.COMPLETED,
            Status.REJECTED,
            Status.FAILED,
            Status.ESCALATED,
            Status.CANCELLED,
        }
        if task.status not in notified_statuses:
            return None
        notification_id = f"notification:{task.task_id}:{task.version}"
        metadata = (
            {"plan_hash": task.plan_hash}
            if task.status is Status.WAITING_APPROVAL and task.plan_hash is not None
            else {}
        )
        notification = Notification(
            notification_id=notification_id,
            task_id=task.task_id,
            task_version=task.version,
            status=task.status.value,
            channel=NotificationChannel.OPERATIONS,
            occurred_at=task.updated_at,
            metadata=metadata,
        )
        return OutboxDraft(
            outbox_id=notification_id,
            topic=_NOTIFICATION_TOPIC,
            payload=notification.to_payload(),
            created_at=task.updated_at,
        )

    @staticmethod
    def _reject_public_notification_outbox(outbox: OutboxDraft | None) -> None:
        if outbox is not None and outbox.topic == _NOTIFICATION_TOPIC:
            raise InvalidPersistenceValueError(
                "notification topic은 Task 상태 전이에서만 자동 생성할 수 있습니다."
            )

    @staticmethod
    def _approval_replay(row: ApprovalRow) -> ApprovalApplyResult:
        record = _approval_from_row(row)
        return ApprovalApplyResult(
            task=record.result_task,
            status=ApprovalApplyStatus.ALREADY_APPLIED,
            record=record,
            event=None,
            outbox=None,
        )

    @staticmethod
    def _validate_task_successor(current: Task, candidate: Task) -> None:
        """공개 domain 연산으로 만든 정확한 다음 snapshot인지 검증한다."""

        if (
            candidate.task_id != current.task_id
            or candidate.input != current.input
            or candidate.created_at != current.created_at
            or candidate.version != current.version + 1
        ):
            raise InvalidPersistenceValueError(
                "Task 식별자와 immutable 입력을 보존한 다음 version이 필요합니다."
            )
        if (
            current.status is Status.WAITING_APPROVAL
            and candidate.status is Status.RUNNING
        ):
            raise InvalidPersistenceValueError(
                "승인 재개는 apply_approval transaction으로만 저장할 수 있습니다."
            )
        if candidate.status is current.status:
            if candidate.plan_hash is None:
                raise InvalidPersistenceValueError(
                    "같은 상태의 다음 version에는 plan 갱신이 필요합니다."
                )
            expected = current.update_plan(
                candidate.plan_hash,
                at=candidate.updated_at,
            )
        else:
            expected = current.transition(
                candidate.status,
                at=candidate.updated_at,
            )
        if expected != candidate:
            raise InvalidPersistenceValueError(
                "candidate가 현재 Task의 정확한 다음 snapshot이 아닙니다."
            )

    @staticmethod
    def _validate_issuance_successor(
        previous: WorkflowRun,
        candidate: WorkflowRun,
        agent_run: AgentRun,
    ) -> None:
        """domain 발급 연산의 정확한 두 결과인지 재구성해 검증한다."""

        retry = previous.phase is Phase.VERIFYING and agent_run.phase is Phase.EXECUTING
        expected_workflow, expected_agent_run = previous.begin_agent_run(
            agent_run_id=agent_run.agent_run_id,
            agent_id=agent_run.agent_id,
            at=agent_run.started_at,
            retry=retry,
        )
        if candidate != expected_workflow or agent_run != expected_agent_run:
            raise InvalidPersistenceValueError(
                "WorkflowRun과 AgentRun이 정확한 begin_agent_run 결과가 아닙니다."
            )


__all__ = ["SQLiteStore"]
