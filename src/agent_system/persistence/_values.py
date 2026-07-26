"""ORM row를 노출하지 않는 persistence 결과 값."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from agent_system.orchestration import (
    Approval,
    ApprovalResponse,
    FailureCode,
    Task,
    WorkflowRun,
)


class PersistenceError(Exception):
    """영속화 interface 오류의 기본 타입."""


class PersistenceConflictError(PersistenceError):
    """고유성 또는 저장된 값과의 충돌을 나타낸다."""


class OptimisticConcurrencyError(PersistenceConflictError):
    """호출자가 기대한 snapshot이 더 이상 현재 값이 아닐 때 발생한다."""


class IdempotencyConflictError(PersistenceConflictError):
    """같은 멱등성 key가 다른 요청에 재사용되었을 때 발생한다."""


class ApprovalConflictError(PersistenceConflictError):
    """승인 decision key가 다른 Approval에 재사용되었을 때 발생한다."""


class PersistenceNotFoundError(PersistenceError):
    """요청한 영속 값이 없을 때 발생한다."""


class InvalidPersistenceValueError(PersistenceError, ValueError):
    """persistence 명령 값이 interface 불변 조건을 위반할 때 발생한다."""


class InvalidOutboxTransitionError(PersistenceError):
    """outbox 상태 machine에 없는 전이를 요청했을 때 발생한다."""


class OutboxStatus(StrEnum):
    """전달 dispatcher가 갱신할 수 있는 outbox 상태."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class ApprovalApplyStatus(StrEnum):
    """Approval compare-and-transition의 결정 가능한 결과."""

    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"


class RecoveryDisposition(StrEnum):
    """startup runner가 non-terminal Task를 다루는 방식."""

    RESUME = "RESUME"
    WAITING_APPROVAL = "WAITING_APPROVAL"


class RuntimeCommandType(StrEnum):
    """Background runner가 durable하게 실행할 명령 종류."""

    START = "START"
    APPROVAL = "APPROVAL"
    CANCEL = "CANCEL"


class RuntimeCommandStatus(StrEnum):
    """Runtime command의 durable 처리 상태."""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"


def _require_text(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPersistenceValueError(f"{field_name}는 비어 있을 수 없습니다.")


def _require_aware(value: datetime, *, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InvalidPersistenceValueError(
            f"{field_name}에는 timezone-aware datetime이 필요합니다."
        )


def _require_payload(payload: Mapping[str, object]) -> None:
    if not isinstance(payload, Mapping):
        raise InvalidPersistenceValueError("payload는 mapping이어야 합니다.")


@dataclass(frozen=True, slots=True)
class TaskEventDraft:
    """Task snapshot과 함께 추가할 append-only event 입력."""

    event_id: str
    event_type: str
    payload: Mapping[str, object]
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.event_id, field_name="event_id")
        _require_text(self.event_type, field_name="event_type")
        _require_payload(self.payload)
        _require_aware(self.occurred_at, field_name="occurred_at")


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """저장된 Task version에 결합된 감사 event."""

    event_id: str
    task_id: str
    task_version: int
    event_type: str
    payload: Mapping[str, object]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxDraft:
    """업무 transaction과 함께 기록할 알림 전달 의도."""

    outbox_id: str
    topic: str
    payload: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.outbox_id, field_name="outbox_id")
        _require_text(self.topic, field_name="topic")
        _require_payload(self.payload)
        _require_aware(self.created_at, field_name="created_at")


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """dispatcher가 읽고 optimistic하게 전이하는 outbox snapshot."""

    outbox_id: str
    task_id: str | None
    topic: str
    payload: Mapping[str, object]
    status: OutboxStatus
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    last_error: str | None


@dataclass(frozen=True, slots=True)
class TaskWriteResult:
    """Task write 또는 멱등 replay의 domain 결과."""

    task: Task
    event: TaskEvent | None
    outbox: OutboxMessage | None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """외부 요청의 identity와 내용 fingerprint 결합."""

    namespace: str
    key: str
    fingerprint: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.namespace, field_name="namespace")
        _require_text(self.key, field_name="key")
        _require_text(self.fingerprint, field_name="fingerprint")
        _require_aware(self.created_at, field_name="created_at")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """한 번 소비된 Approval과 당시 재개 결과 snapshot."""

    approval_id: str
    decision_id: str
    approval: Approval
    consumed_at: datetime
    result_task: Task


@dataclass(frozen=True, slots=True)
class ApprovalDecisionRecord:
    """한 번 소비된 승인·거절 응답과 exact successor 기록."""

    decision_id: str
    task_id: str
    task_version: int
    plan_hash: str
    response: ApprovalResponse
    consumed_at: datetime
    result_task: Task
    failure: FailureCode | None


@dataclass(frozen=True, slots=True)
class ApprovalApplyResult:
    """최초 승인 적용 또는 replay의 결과."""

    task: Task
    status: ApprovalApplyStatus
    record: ApprovalRecord
    event: TaskEvent | None
    outbox: OutboxMessage | None


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    """startup에서 checkpoint thread와 함께 복구할 Task."""

    task: Task
    workflow_run: WorkflowRun | None
    disposition: RecoveryDisposition
    thread_id: str


@dataclass(frozen=True, slots=True)
class RuntimeCommandDraft:
    """Task에 결합해 먼저 저장할 background 실행 의도."""

    command_id: str
    task_id: str
    command_type: RuntimeCommandType
    fingerprint: str
    payload: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.command_id, field_name="command_id")
        _require_text(self.task_id, field_name="task_id")
        if type(self.command_type) is not RuntimeCommandType:
            raise InvalidPersistenceValueError(
                "command_type은 RuntimeCommandType이어야 합니다."
            )
        _require_text(self.fingerprint, field_name="fingerprint")
        _require_payload(self.payload)
        _require_aware(self.created_at, field_name="created_at")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class RuntimeCommandRecord:
    """저장된 runtime command와 처리 결과."""

    command_id: str
    task_id: str
    command_type: RuntimeCommandType
    fingerprint: str
    payload: Mapping[str, object]
    status: RuntimeCommandStatus
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    replayed: bool = False


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
    "TaskEvent",
    "TaskEventDraft",
    "TaskWriteResult",
]
