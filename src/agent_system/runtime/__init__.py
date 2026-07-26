"""HTTP와 CLI가 공유하는 application interface와 composition root 공개 값."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel

from agent_system.agents import AgentMetadata, AgentRegistry, EchoAgent
from agent_system.config import RuntimeSettings
from agent_system.models import ModelSettings, create_chat_model
from agent_system.notifications import (
    LoggingNotificationSender,
    NotificationDispatcher,
    NotificationSender,
)
from agent_system.orchestration import (
    AgentRun,
    AlertInput,
    Approval,
    ApprovalConsumeResult,
    ApprovalConsumeStatus,
    ApprovalResponse,
    ApprovalResumeError,
    ChatModelRequestClassifier,
    ExecutionClaimResult,
    ExecutionClaimStatus,
    FailureCode,
    Governance,
    GovernanceDecision,
    OrchestrationCancellationError,
    OrchestrationJournalEntry,
    OrchestrationStartError,
    OrchestratorInput,
    OrchestratorService,
    Phase,
    RequestClassifier,
    RoutingDecision,
    Status,
    Task,
    TicketInput,
    UserTaskInput,
    WorkflowRun,
)
from agent_system.persistence import (
    ApprovalConflictError,
    IdempotencyConflictError,
    IdempotencyKey,
    OptimisticConcurrencyError,
    PersistenceConflictError,
    RecoveryDisposition,
    RuntimeCommandDraft,
    RuntimeCommandRecord,
    RuntimeCommandStatus,
    RuntimeCommandType,
    SQLiteNotificationOutbox,
    SQLiteStore,
    TaskEvent,
    TaskEventDraft,
    upgrade_database,
)


class SubmissionKind(StrEnum):
    """Runtime이 orchestration 입력으로 변환할 외부 요청 종류."""

    USER_TASK = "user_task"
    ALERT = "alert"
    TICKET = "ticket"


class ApprovalDecision(StrEnum):
    """사람 승인 명령의 결정."""

    APPROVE = "approve"
    REJECT = "reject"


class ApplicationError(RuntimeError):
    """전송 adapter가 안정적으로 변환할 application 오류."""


class ApplicationNotFoundError(ApplicationError):
    """요청한 Task가 persistence authority에 없을 때 발생한다."""


class ApplicationConflictError(ApplicationError):
    """멱등성, version, 승인 또는 terminal 상태가 충돌할 때 발생한다."""


class ApplicationBusyError(ApplicationError):
    """Bounded runner가 현재 작업을 더 수락할 수 없을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class Submission:
    """HTTP payload를 provider 중립 runtime 명령으로 묶은 값."""

    kind: SubmissionKind
    payload: Mapping[str, str]
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not SubmissionKind:
            raise TypeError("kind에는 SubmissionKind가 필요합니다.")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload는 mapping이어야 합니다.")
        copied = dict(self.payload)
        if not copied or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in copied.items()
        ):
            raise ValueError(
                "payload에는 비어 있지 않은 문자열 key/value가 필요합니다."
            )
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str)
            or not self.idempotency_key.strip()
        ):
            raise ValueError("idempotency_key는 비어 있을 수 없습니다.")
        object.__setattr__(self, "payload", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class ApprovalCommand:
    """현재 승인 binding과 사람 결정을 함께 전달하는 명령."""

    decision_id: str
    decision: ApprovalDecision
    task_version: int
    plan_hash: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CancelCommand:
    """Optimistic version에 결합된 취소 명령."""

    expected_version: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptedTask:
    """Queue에 수락했거나 멱등 replay한 명령 결과."""

    task_id: str
    status: str
    version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ApprovalView:
    """GET 응답에 노출할 현재 승인 요청 metadata."""

    task_version: int
    plan_hash: str
    plan_summary: str
    plan_steps: tuple[str, ...]
    agent_id: str
    action: str


@dataclass(frozen=True, slots=True)
class ResultView:
    """GET 응답에 노출할 terminal/실행 결과 metadata."""

    output: str | None
    agent_run_count: int


@dataclass(frozen=True, slots=True)
class TaskView:
    """Persistence authority에서 조립한 공개 Task 조회 snapshot."""

    task_id: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    approval: ApprovalView | None
    result: ResultView | None
    errors: tuple[str, ...]


class TaskApplication(Protocol):
    """HTTP adapter가 알아야 하는 작은 runtime application interface."""

    async def start(self) -> None:
        """Background runner와 startup recovery를 시작한다."""

    async def stop(self) -> None:
        """수락을 중단하고 소유 자원을 정리한다."""

    async def submit(self, submission: Submission) -> AcceptedTask:
        """Task를 영속화하고 background queue에 수락한다."""

    async def get_task(self, task_id: str) -> TaskView:
        """현재 durable Task와 공개 metadata를 반환한다."""

    async def approve(
        self,
        task_id: str,
        command: ApprovalCommand,
    ) -> AcceptedTask:
        """승인 또는 거절 재개를 queue에 수락한다."""

    async def cancel(
        self,
        task_id: str,
        command: CancelCommand,
    ) -> AcceptedTask:
        """활성 Task 취소를 적용한다."""


class RuntimeOrchestrator(Protocol):
    """Background runner가 사용하는 provider-neutral orchestration interface."""

    async def start(
        self,
        request: OrchestratorInput,
        *,
        thread_id: str,
        initial_task: Task,
    ) -> object:
        """Durable RECEIVED Task로 새 checkpoint 실행을 시작한다."""

    async def recover(self, *, thread_id: str) -> object:
        """기존 checkpoint의 active 실행을 복구한다."""

    async def resume(self, *, thread_id: str, response: object) -> object:
        """승인 결정을 기존 checkpoint에 전달한다."""

    async def cancel(
        self,
        *,
        thread_id: str,
        reason: str | None = None,
    ) -> object:
        """Checkpoint 실행을 terminal cancellation으로 만든다."""


class SQLiteApprovalConsumer:
    """SQLite transaction을 async ApprovalConsumer interface에 맞추는 adapter."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def consume(
        self,
        *,
        task: Task,
        response: ApprovalResponse,
        at: datetime,
    ) -> ApprovalConsumeResult:
        """승인·거절 CAS를 worker thread에서 적용하고 stable 결과를 반환한다."""

        try:
            return await asyncio.to_thread(
                self._store.consume_approval,
                task=task,
                response=response,
                at=at,
                event=TaskEventDraft(
                    event_id=f"approval:{task.task_id}:{response.decision_id}",
                    event_type=(
                        "TASK_APPROVED" if response.accepted else "TASK_REJECTED"
                    ),
                    payload={
                        "decision_id": response.decision_id,
                        "accepted": response.accepted,
                        "errors": (
                            []
                            if response.accepted
                            else [FailureCode.HUMAN_REJECTED.value]
                        ),
                    },
                    occurred_at=at,
                ),
            )
        except (
            ApprovalConflictError,
            OptimisticConcurrencyError,
            PersistenceConflictError,
        ):
            return ApprovalConsumeResult(
                status=ApprovalConsumeStatus.CONFLICT,
                task=task,
            )


class SQLiteLifecycleJournal:
    """Orchestration aggregate 변경을 SQLite facade로 idempotent하게 기록한다."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def record(
        self,
        entry: OrchestrationJournalEntry,
    ) -> OrchestrationJournalEntry:
        """짧은 sync transaction을 worker thread에서 순서대로 적용한다."""

        return await asyncio.to_thread(self._record, entry)

    def _record(self, entry: OrchestrationJournalEntry) -> OrchestrationJournalEntry:
        current_task = self._store.get_task(entry.task.task_id)
        journal_task = entry.task
        if current_task is None:
            if entry.task.version != 1 or entry.task_event_type is None:
                raise ApplicationConflictError("Journal의 최초 Task entry가 아닙니다.")
            self._store.create_task(
                entry.task,
                event=self._event_draft(entry),
            )
            current_task = entry.task
        elif current_task == entry.task or self._same_task_callback(
            current_task, entry.task
        ):
            if not self._same_version_event_matches(current_task, entry):
                raise ApplicationConflictError("Journal Task event가 충돌했습니다.")
            journal_task = current_task
        elif (
            historical_task := self._historical_task_callback(current_task, entry)
        ) is not None:
            journal_task = historical_task
        elif entry.task.version == current_task.version + 1:
            if entry.task_event_type is None:
                raise ApplicationConflictError("Task 전이에 journal event가 없습니다.")
            self._store.save_task(
                entry.task,
                expected_version=current_task.version,
                event=self._event_draft(entry),
            )
            current_task = entry.task
            journal_task = current_task
        else:
            raise ApplicationConflictError("Journal Task snapshot이 충돌했습니다.")

        if entry.workflow is None:
            return OrchestrationJournalEntry(
                task=journal_task,
                workflow=None,
                task_event_type=entry.task_event_type,
                task_event_payload=entry.task_event_payload,
            )
        current_workflow = self._store.get_workflow_run(entry.workflow.workflow_run_id)
        if current_workflow is None:
            existing_owner = self._store.get_workflow_run_for_task(entry.task.task_id)
            if existing_owner is not None:
                return self._historical_workflow_entry(
                    entry,
                    task=journal_task,
                    current_workflow=existing_owner,
                )
            self._store.create_workflow_run(entry.workflow)
            return entry
        if self._workflow_progress(current_workflow) > self._workflow_progress(
            entry.workflow
        ):
            return self._historical_workflow_entry(
                entry,
                task=journal_task,
                current_workflow=current_workflow,
            )
        if current_workflow == entry.workflow or self._same_workflow_callback(
            current_workflow, entry.workflow
        ):
            authoritative = OrchestrationJournalEntry(
                task=journal_task,
                workflow=current_workflow,
                agent_run=entry.agent_run,
                task_event_type=entry.task_event_type,
                task_event_payload=entry.task_event_payload,
            )
            if entry.agent_run is not None:
                return self._record_existing_agent(authoritative, current_workflow)
            return authoritative

        if (
            entry.agent_run is not None
            and not entry.agent_run.is_completed
            and entry.workflow.budget.consumed == current_workflow.budget.consumed + 1
        ):
            self._store.record_agent_run(
                current_workflow,
                entry.workflow,
                entry.agent_run,
            )
            return entry
        if (
            entry.agent_run is not None
            and not entry.agent_run.is_completed
            and entry.workflow.budget.consumed == current_workflow.budget.consumed
        ):
            existing = self._agent_for_sequence(
                current_workflow,
                entry.agent_run.budget_sequence,
            )
            if existing is not None and not existing.is_completed:
                return OrchestrationJournalEntry(
                    task=journal_task,
                    workflow=current_workflow,
                    agent_run=existing,
                )
        recorded_agent = entry.agent_run
        if entry.agent_run is not None and entry.agent_run.is_completed:
            existing = self._store.get_agent_run(entry.agent_run.agent_run_id)
            if existing is not None and existing.is_completed:
                recorded_agent = existing
            else:
                recorded_agent = self._store.complete_agent_run(entry.agent_run)
        if (
            entry.workflow.phase != current_workflow.phase
            and entry.workflow.task_version == current_workflow.task_version
            and entry.workflow.budget.consumed == current_workflow.budget.consumed
        ):
            self._store.save_workflow_run(current_workflow, entry.workflow)
            return OrchestrationJournalEntry(
                task=journal_task,
                workflow=entry.workflow,
                agent_run=recorded_agent,
                task_event_type=entry.task_event_type,
                task_event_payload=entry.task_event_payload,
            )
        if (
            entry.workflow.phase == current_workflow.phase
            and entry.workflow.task_version != current_workflow.task_version
            and entry.workflow.budget.consumed == current_workflow.budget.consumed
        ):
            self._store.rebind_workflow_run(current_workflow, entry.workflow)
            return entry
        raise ApplicationConflictError("Journal WorkflowRun snapshot이 충돌했습니다.")

    def _record_existing_agent(
        self,
        entry: OrchestrationJournalEntry,
        workflow: WorkflowRun,
    ) -> OrchestrationJournalEntry:
        assert entry.agent_run is not None
        existing = self._store.get_agent_run(entry.agent_run.agent_run_id)
        if existing is None:
            raise ApplicationConflictError("Workflow issuance의 AgentRun이 없습니다.")
        if existing == entry.agent_run:
            return entry
        if existing.is_completed and entry.agent_run.is_completed:
            return OrchestrationJournalEntry(
                task=entry.task,
                workflow=workflow,
                agent_run=existing,
                task_event_type=entry.task_event_type,
                task_event_payload=entry.task_event_payload,
            )
        if entry.agent_run.is_completed and not existing.is_completed:
            self._store.complete_agent_run(entry.agent_run)
            return entry
        raise ApplicationConflictError("Journal AgentRun snapshot이 충돌했습니다.")

    def _agent_for_sequence(
        self,
        workflow: WorkflowRun,
        sequence: int,
    ) -> AgentRun | None:
        for agent_run in self._store.list_agent_runs(workflow.workflow_run_id):
            if agent_run.budget_sequence == sequence:
                return agent_run
        return None

    def _historical_task_callback(
        self,
        current: Task,
        entry: OrchestrationJournalEntry,
    ) -> Task | None:
        if (
            current.version <= entry.task.version
            or current.task_id != entry.task.task_id
            or current.input != entry.task.input
            or current.created_at != entry.task.created_at
        ):
            return None
        event = next(
            (
                candidate
                for candidate in self._store.list_task_events(entry.task.task_id)
                if candidate.task_version == entry.task.version
            ),
            None,
        )
        if event is None:
            return None
        if not (
            entry.task.created_at <= event.occurred_at <= current.updated_at
            and entry.task.updated_at >= event.occurred_at
        ):
            return None
        expected_status = {
            "TASK_RECEIVED": Status.RECEIVED,
            "TASK_STARTED": Status.RUNNING,
            "TASK_PLAN_UPDATED": Status.RUNNING,
            "TASK_WAITING_APPROVAL": Status.WAITING_APPROVAL,
            "TASK_APPROVED": Status.RUNNING,
            "TASK_REJECTED": Status.REJECTED,
            "TASK_COMPLETED": Status.COMPLETED,
            "TASK_FAILED": Status.FAILED,
            "TASK_ESCALATED": Status.ESCALATED,
            "TASK_CANCELLED": Status.CANCELLED,
        }.get(event.event_type)
        if expected_status is None or entry.task.status is not expected_status:
            return None
        if event.event_type == "TASK_PLAN_UPDATED" and (
            event.payload.get("plan_hash") != entry.task.plan_hash
        ):
            return None
        if entry.task_event_type is None:
            if (
                entry.workflow is None
                or entry.workflow.task_version != entry.task.version
            ):
                return None
        elif event.event_type != entry.task_event_type or dict(event.payload) != dict(
            entry.task_event_payload or {}
        ):
            return None
        return replace(entry.task, updated_at=event.occurred_at)

    def _same_version_event_matches(
        self,
        task: Task,
        entry: OrchestrationJournalEntry,
    ) -> bool:
        if entry.task_event_type is None:
            return True
        event = next(
            (
                candidate
                for candidate in self._store.list_task_events(task.task_id)
                if candidate.task_version == task.version
            ),
            None,
        )
        return (
            event is not None
            and event.event_type == entry.task_event_type
            and dict(event.payload) == dict(entry.task_event_payload or {})
        )

    def _historical_workflow_entry(
        self,
        entry: OrchestrationJournalEntry,
        *,
        task: Task,
        current_workflow: WorkflowRun,
    ) -> OrchestrationJournalEntry:
        assert entry.workflow is not None
        if self._workflow_progress(current_workflow) < self._workflow_progress(
            entry.workflow
        ):
            raise ApplicationConflictError(
                "Journal WorkflowRun owner가 callback보다 뒤에 있습니다."
            )
        snapshot = entry.workflow.to_snapshot()
        current_snapshot = current_workflow.to_snapshot()
        snapshot["workflow_run_id"] = current_workflow.workflow_run_id
        snapshot["started_at"] = current_snapshot["started_at"]
        consumed = entry.workflow.budget.consumed
        snapshot["agent_run_issuances"] = current_snapshot["agent_run_issuances"][
            :consumed
        ]
        workflow = WorkflowRun.from_snapshot(snapshot)
        agent_run = None
        if entry.agent_run is not None:
            existing = self._agent_for_sequence(
                current_workflow,
                entry.agent_run.budget_sequence,
            )
            if existing is None:
                raise ApplicationConflictError("Journal replay의 AgentRun이 없습니다.")
            agent_snapshot = existing.to_snapshot()
            if not entry.agent_run.is_completed:
                agent_snapshot.update(
                    {"outcome": None, "output": None, "completed_at": None}
                )
            agent_run = AgentRun.from_snapshot(agent_snapshot, workflow=workflow)
        return OrchestrationJournalEntry(
            task=task,
            workflow=workflow,
            agent_run=agent_run,
            task_event_type=entry.task_event_type,
            task_event_payload=entry.task_event_payload,
        )

    @staticmethod
    def _workflow_progress(workflow: WorkflowRun) -> tuple[int, int]:
        phase_order = {
            Phase.CLASSIFYING: 0,
            Phase.ANALYZING: 1,
            Phase.PLANNING: 2,
            Phase.GOVERNING: 3,
            Phase.EXECUTING: 4,
            Phase.VERIFYING: 5,
        }
        return workflow.budget.consumed, phase_order[workflow.phase]

    @staticmethod
    def _same_task_callback(left: Task, right: Task) -> bool:
        left_snapshot = left.to_snapshot()
        right_snapshot = right.to_snapshot()
        left_snapshot.pop("updated_at")
        right_snapshot.pop("updated_at")
        return left_snapshot == right_snapshot

    @staticmethod
    def _same_workflow_callback(left: WorkflowRun, right: WorkflowRun) -> bool:
        left_snapshot = left.to_snapshot()
        right_snapshot = right.to_snapshot()
        left_snapshot.pop("updated_at")
        right_snapshot.pop("updated_at")
        return left_snapshot == right_snapshot

    @staticmethod
    def _event_draft(entry: OrchestrationJournalEntry) -> TaskEventDraft:
        if entry.task_event_type is None or entry.task_event_payload is None:
            raise ApplicationConflictError("Task journal event가 없습니다.")
        return TaskEventDraft(
            event_id=f"event:{entry.task.task_id}:{entry.task.version}",
            event_type=entry.task_event_type,
            payload=dict(entry.task_event_payload),
            occurred_at=entry.task.updated_at,
        )


class InProcessExecutionCoordinator:
    """모든 runtime service가 공유하는 단일 process execution coordinator."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._claims: set[tuple[str, str]] = set()

    async def claim(
        self,
        *,
        thread_id: str,
        claim_key: str,
    ) -> ExecutionClaimResult:
        key = (thread_id, claim_key)
        async with self._lock:
            if key in self._claims:
                return ExecutionClaimResult(ExecutionClaimStatus.BUSY)
            self._claims.add(key)
            return ExecutionClaimResult(ExecutionClaimStatus.CLAIMED)

    async def release(self, *, thread_id: str, claim_key: str) -> None:
        async with self._lock:
            self._claims.discard((thread_id, claim_key))


class _HumanApprovalGovernance:
    """Mutating plan을 사람 승인 단계로 보내는 기본 runtime 정책."""

    async def evaluate(
        self,
        _request: OrchestratorInput,
        _routing: RoutingDecision,
    ) -> GovernanceDecision:
        return GovernanceDecision(approved=True, reason="human approval required")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


def build_runtime(
    settings: RuntimeSettings,
    *,
    classifier: RequestClassifier | None = None,
    governance: Governance | None = None,
    registry: AgentRegistry | None = None,
    coordinator: InProcessExecutionCoordinator | None = None,
    model_factory: Callable[[ModelSettings], BaseChatModel] = create_chat_model,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[], str] = _uuid,
    notification_sender: NotificationSender | None = None,
    notification_id_factory: Callable[[], str] = _uuid,
) -> RuntimeApplication:
    """설정과 주입 adapter를 하나의 소유권 명확한 runtime으로 조립한다."""

    upgrade_database(settings.db_path)
    store = SQLiteStore(settings.db_path)
    checkpointer_context = store.open_checkpointer()
    try:
        checkpointer = checkpointer_context.__enter__()
        if registry is None:
            registry = AgentRegistry()
            registry.register(
                EchoAgent(
                    AgentMetadata(
                        agent_id="operations-agent",
                        name="Operations Agent",
                        description="기본 provider 중립 실행 Agent",
                    )
                )
            )
        if classifier is None:
            classifier = ChatModelRequestClassifier(
                model_factory(settings.model_settings)
            )
        shared_coordinator = coordinator or InProcessExecutionCoordinator()
        service = OrchestratorService(
            classifier=classifier,
            governance=governance or _HumanApprovalGovernance(),
            approval_consumer=SQLiteApprovalConsumer(store),
            execution_coordinator=shared_coordinator,
            journal=SQLiteLifecycleJournal(store),
            registry=registry,
            max_agent_runs=settings.max_agent_runs,
            checkpointer=checkpointer,
            clock=clock,
            id_factory=id_factory,
        )
        notification_dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(store),
            sender=(
                notification_sender
                if notification_sender is not None
                else LoggingNotificationSender()
            ),
            clock=clock,
            id_factory=notification_id_factory,
        )

        def close_resources() -> None:
            try:
                checkpointer_context.__exit__(None, None, None)
            finally:
                store.close()

        return RuntimeApplication(
            store=store,
            orchestrator=service,
            queue_capacity=settings.queue_capacity,
            worker_count=settings.worker_count,
            clock=clock,
            id_factory=id_factory,
            notification_dispatcher=notification_dispatcher,
            close_resources=close_resources,
        )
    except BaseException:
        checkpointer_context.__exit__(None, None, None)
        store.close()
        raise


@dataclass(frozen=True, slots=True)
class _StartWork:
    request: OrchestratorInput
    initial_task: Task
    command_id: str | None = None
    fingerprint: str = "start"

    @property
    def task_id(self) -> str:
        return self.initial_task.task_id


@dataclass(frozen=True, slots=True)
class _RecoverWork:
    task_id: str
    thread_id: str
    command_id: str | None = None
    fingerprint: str = "recover"


@dataclass(frozen=True, slots=True)
class _ResumeWork:
    task_id: str
    thread_id: str
    response: ApprovalResponse
    command_id: str | None = None
    fingerprint: str = "approval"


@dataclass(frozen=True, slots=True)
class _CancelWork:
    task_id: str
    thread_id: str
    reason: str | None = None
    command_id: str | None = None
    fingerprint: str = "cancel"


_WorkItem = _StartWork | _RecoverWork | _ResumeWork | _CancelWork


class RuntimeApplication:
    """Durable command 수락과 bounded background 실행을 숨기는 application module."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        orchestrator: RuntimeOrchestrator,
        queue_capacity: int,
        worker_count: int,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        notification_dispatcher: NotificationDispatcher | None = None,
        close_resources: Callable[[], None] | None = None,
    ) -> None:
        if (
            isinstance(queue_capacity, bool)
            or not isinstance(queue_capacity, int)
            or queue_capacity <= 0
        ):
            raise ValueError("queue_capacity은 양의 정수여야 합니다.")
        if (
            isinstance(worker_count, bool)
            or not isinstance(worker_count, int)
            or worker_count <= 0
        ):
            raise ValueError("worker_count는 양의 정수여야 합니다.")
        self._store = store
        self._orchestrator = orchestrator
        self._queue: asyncio.Queue[_WorkItem | None] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._worker_count = worker_count
        self._clock = clock
        self._id_factory = id_factory
        self._notification_dispatcher = notification_dispatcher
        self._close_resources = close_resources
        self._workers: list[asyncio.Task[None]] = []
        self._deferred_work: deque[_WorkItem] = deque()
        self._deferred_keys: set[tuple[str, str]] = set()
        self._retry_wakeups: dict[str, asyncio.Task[None]] = {}
        self._active: dict[str, asyncio.Task[object]] = {}
        self._queued_commands: set[tuple[str, str]] = set()
        self._active_fingerprints: dict[str, str] = {}
        self._background_errors: dict[str, str] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._stopping = False
        self._prefer_deferred = False
        self._closed = False

    async def start(self) -> None:
        """Worker를 시작하고 durable recovery candidate를 한 번 enqueue한다."""

        if self._closed:
            raise RuntimeError("종료한 RuntimeApplication은 다시 시작할 수 없습니다.")
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(), name=f"agent-worker-{index}")
            for index in range(self._worker_count)
        ]
        if self._notification_dispatcher is not None:
            await self._notification_dispatcher.start()
        await self._pump_pending_commands()
        durable_task_ids = {
            command.task_id
            for command in await asyncio.to_thread(self._store.list_runtime_commands)
        }
        candidates = await asyncio.to_thread(self._store.list_recovery_candidates)
        for candidate in candidates:
            if candidate.task.task_id in durable_task_ids:
                continue
            if candidate.disposition is RecoveryDisposition.WAITING_APPROVAL:
                continue
            if candidate.workflow_run is None:
                events = await asyncio.to_thread(
                    self._store.list_task_events,
                    candidate.task.task_id,
                )
                request = self._request_from_events(candidate.task, events)
                await self._enqueue_or_defer(
                    _StartWork(request=request, initial_task=candidate.task)
                )
            else:
                await self._enqueue_or_defer(
                    _RecoverWork(
                        task_id=candidate.task.task_id,
                        thread_id=candidate.thread_id,
                    )
                )

    async def stop(self) -> None:
        """수락을 멈추고 queue를 drain한 뒤 worker와 소유 자원을 닫는다."""

        async with self._lifecycle_lock:
            if self._closed:
                return
            self._stopping = True
            if self._started:
                wakeups = tuple(self._retry_wakeups.values())
                for wakeup in wakeups:
                    wakeup.cancel()
                if wakeups:
                    await asyncio.gather(*wakeups, return_exceptions=True)
                self._retry_wakeups.clear()
                await self.drain()
                for _worker in self._workers:
                    await self._queue.put(None)
                await asyncio.gather(*self._workers)
                self._workers.clear()
                self._started = False
            if self._notification_dispatcher is not None:
                await self._notification_dispatcher.stop()
            if self._close_resources is not None:
                await asyncio.to_thread(self._close_resources)
            self._closed = True

    async def drain(self) -> None:
        """현재 queue와 active child 실행이 모두 끝날 때까지 기다린다."""

        await self._queue.join()
        if self._notification_dispatcher is not None:
            await self._notification_dispatcher.drain()

    async def submit(self, submission: Submission) -> AcceptedTask:
        """RECEIVED snapshot을 먼저 저장한 뒤 background start를 수락한다."""

        self._require_started()
        candidate_task_id = self._id_factory()
        request = self._request_from_submission(candidate_task_id, submission)
        now = self._clock()
        task = Task.receive(task_id=candidate_task_id, input=request.text, at=now)
        fingerprint = self._fingerprint(submission)
        idempotency = self._idempotency(submission, fingerprint, at=now)
        start_fingerprint = f"start:{fingerprint}"
        start_command = self._runtime_command(
            task_id=task.task_id,
            command_type=RuntimeCommandType.START,
            fingerprint=start_fingerprint,
            payload={"request": request.to_snapshot()},
            at=now,
        )
        try:
            write = await asyncio.to_thread(
                self._store.create_task,
                task,
                event=TaskEventDraft(
                    event_id=f"event:{task.task_id}:1",
                    event_type="TASK_RECEIVED",
                    payload={"request": request.to_snapshot()},
                    occurred_at=now,
                ),
                idempotency=idempotency,
                command=start_command,
            )
        except IdempotencyConflictError:
            raise ApplicationConflictError(
                "같은 멱등성 identity가 다른 payload에 사용되었습니다."
            ) from None
        persisted = write.task
        persisted_request = self._request_from_submission(
            persisted.task_id,
            submission,
        )
        persisted_command = self._runtime_command(
            task_id=persisted.task_id,
            command_type=RuntimeCommandType.START,
            fingerprint=start_fingerprint,
            payload={"request": persisted_request.to_snapshot()},
            at=now,
        )
        command_record = await asyncio.to_thread(
            self._store.put_runtime_command,
            persisted_command,
            expected_task=persisted,
        )
        if command_record.status is RuntimeCommandStatus.PENDING:
            await self._enqueue_durable(
                _StartWork(
                    request=persisted_request,
                    initial_task=persisted,
                    command_id=command_record.command_id,
                    fingerprint=command_record.fingerprint,
                )
            )
        return AcceptedTask(
            task_id=persisted.task_id,
            status=persisted.status.value,
            version=persisted.version,
            replayed=write.replayed,
        )

    async def get_task(self, task_id: str) -> TaskView:
        """SQLite Task와 journal metadata를 공개 조회 값으로 조립한다."""

        task = await asyncio.to_thread(self._store.get_task, task_id)
        if task is None:
            raise ApplicationNotFoundError("Task가 없습니다.")
        events = await asyncio.to_thread(self._store.list_task_events, task_id)
        workflow = await self._workflow_for_task(task_id)
        agent_runs = (
            ()
            if workflow is None
            else await asyncio.to_thread(
                self._store.list_agent_runs,
                workflow.workflow_run_id,
            )
        )
        approval = (
            self._approval_view(events)
            if task.status is Status.WAITING_APPROVAL
            else None
        )
        output, errors = self._result_metadata(events)
        if task_id in self._background_errors:
            errors = (*errors, self._background_errors[task_id])
        result = None
        if (
            output is not None
            or agent_runs
            or task.status
            in {
                Status.COMPLETED,
                Status.REJECTED,
                Status.FAILED,
                Status.CANCELLED,
                Status.ESCALATED,
            }
        ):
            result = ResultView(output=output, agent_run_count=len(agent_runs))
        return TaskView(
            task_id=task.task_id,
            status=task.status.value,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
            approval=approval,
            result=result,
            errors=errors,
        )

    async def approve(
        self,
        task_id: str,
        command: ApprovalCommand,
    ) -> AcceptedTask:
        """현재 approval binding을 검증하고 background resume을 수락한다."""

        self._require_started()
        task = await asyncio.to_thread(self._store.get_task, task_id)
        if task is None:
            raise ApplicationNotFoundError("Task가 없습니다.")
        fingerprint = self._command_fingerprint(
            RuntimeCommandType.APPROVAL,
            {
                "decision_id": command.decision_id,
                "decision": command.decision.value,
                "task_version": command.task_version,
                "plan_hash": command.plan_hash,
                "reason": command.reason,
            },
        )
        existing = await self._runtime_command_replay(
            task_id,
            RuntimeCommandType.APPROVAL,
            fingerprint,
        )
        if existing is not None:
            if existing.status is RuntimeCommandStatus.PENDING:
                await self._enqueue_durable(await self._work_from_command(existing))
            return AcceptedTask(task.task_id, task.status.value, task.version, True)
        if (
            task.status is not Status.WAITING_APPROVAL
            or task.version != command.task_version
            or task.plan_hash != command.plan_hash
        ):
            raise ApplicationConflictError(
                "Approval command가 현재 Task binding과 다릅니다."
            )
        now = self._clock()
        if command.decision is ApprovalDecision.APPROVE:
            response = ApprovalResponse(
                decision_id=command.decision_id,
                accepted=True,
                approval=Approval(
                    task_id=task.task_id,
                    task_version=task.version,
                    plan_hash=task.plan_hash or "",
                    approved_at=now,
                ),
            )
        else:
            if command.reason is None:
                raise ApplicationConflictError("거절 결정에는 reason이 필요합니다.")
            response = ApprovalResponse.reject(
                decision_id=command.decision_id,
                reason=command.reason,
            )
        draft = self._runtime_command(
            task_id=task.task_id,
            command_type=RuntimeCommandType.APPROVAL,
            fingerprint=fingerprint,
            payload={"response": response.to_snapshot()},
            at=now,
        )
        try:
            record = await asyncio.to_thread(
                self._store.put_runtime_command,
                draft,
                expected_task=task,
            )
        except (OptimisticConcurrencyError, PersistenceConflictError):
            raise ApplicationConflictError(
                "Approval command가 현재 Task binding과 충돌했습니다."
            ) from None
        await self._enqueue_durable(await self._work_from_command(record))
        return AcceptedTask(
            task.task_id,
            task.status.value,
            task.version,
            record.replayed,
        )

    async def cancel(
        self,
        task_id: str,
        command: CancelCommand,
    ) -> AcceptedTask:
        """Optimistic active cancellation을 저장하거나 checkpoint 작업에 수락한다."""

        self._require_started()
        task = await asyncio.to_thread(self._store.get_task, task_id)
        if task is None:
            raise ApplicationNotFoundError("Task가 없습니다.")
        fingerprint = self._command_fingerprint(
            RuntimeCommandType.CANCEL,
            {"expected_version": command.expected_version, "reason": command.reason},
        )
        existing = await self._runtime_command_replay(
            task_id,
            RuntimeCommandType.CANCEL,
            fingerprint,
        )
        if existing is not None:
            if existing.status is RuntimeCommandStatus.PENDING:
                await self._enqueue_durable(await self._work_from_command(existing))
            return AcceptedTask(task.task_id, task.status.value, task.version, True)
        if task.version != command.expected_version or task.status in {
            Status.COMPLETED,
            Status.REJECTED,
            Status.FAILED,
            Status.CANCELLED,
            Status.ESCALATED,
        }:
            raise ApplicationConflictError("Task가 terminal이거나 version이 다릅니다.")
        draft = self._runtime_command(
            task_id=task.task_id,
            command_type=RuntimeCommandType.CANCEL,
            fingerprint=fingerprint,
            payload={
                "expected_version": command.expected_version,
                "reason": command.reason,
            },
            at=self._clock(),
        )
        try:
            record = await asyncio.to_thread(
                self._store.put_runtime_command,
                draft,
                expected_task=task,
            )
        except (OptimisticConcurrencyError, PersistenceConflictError):
            raise ApplicationConflictError(
                "Task cancellation이 다른 command와 충돌했습니다."
            ) from None
        await self._enqueue_durable(await self._work_from_command(record))
        return AcceptedTask(
            task.task_id,
            task.status.value,
            task.version,
            record.replayed,
        )

    async def _workflow_for_task(self, task_id: str) -> object | None:
        candidates = await asyncio.to_thread(self._store.list_recovery_candidates)
        for candidate in candidates:
            if candidate.task.task_id == task_id:
                return candidate.workflow_run
        # Terminal Task는 recovery 목록에 없으므로 event에 기록된 workflow ID 대신
        # 현재 schema의 Task당 하나인 run을 public store 조회로 찾는다.
        return await asyncio.to_thread(self._store.get_workflow_run_for_task, task_id)

    async def _enqueue(self, work: _WorkItem) -> None:
        key = (work.task_id, work.fingerprint)
        if key in self._queued_commands:
            return
        active_fingerprint = self._active_fingerprints.get(work.task_id)
        if active_fingerprint is not None and active_fingerprint != work.fingerprint:
            raise ApplicationConflictError(
                "Task에 다른 background command가 실행 중입니다."
            )
        if any(
            task_id == work.task_id and fingerprint != work.fingerprint
            for task_id, fingerprint in self._queued_commands
        ):
            raise ApplicationConflictError(
                "Task에 다른 background command가 대기 중입니다."
            )
        try:
            self._queue.put_nowait(work)
        except asyncio.QueueFull:
            raise ApplicationBusyError("Background queue가 가득 찼습니다.") from None
        self._queued_commands.add(key)

    async def _enqueue_durable(self, work: _WorkItem) -> None:
        """이미 commit된 command는 queue pressure와 무관하게 수락한다."""

        if self._stopping:
            return
        if work.command_id is not None:
            commands = await asyncio.to_thread(
                self._store.list_runtime_commands, work.task_id
            )
            if not any(
                command.command_id == work.command_id
                and command.status is RuntimeCommandStatus.PENDING
                for command in commands
            ):
                return
        try:
            await self._enqueue(work)
        except ApplicationBusyError:
            return

    async def _enqueue_or_defer(self, work: _WorkItem) -> None:
        """Command row가 없는 legacy recovery를 process queue 뒤에 보존한다."""

        try:
            await self._enqueue(work)
        except ApplicationBusyError:
            key = (work.task_id, work.fingerprint)
            if key not in self._deferred_keys:
                self._deferred_work.append(work)
                self._deferred_keys.add(key)

    async def _worker(self) -> None:
        while True:
            work = await self._queue.get()
            if work is None:
                self._queue.task_done()
                return
            child = asyncio.create_task(self._dispatch(work))
            self._active[work.task_id] = child
            self._active_fingerprints[work.task_id] = work.fingerprint
            try:
                await child
                if work.command_id is not None:
                    await asyncio.to_thread(
                        self._store.complete_runtime_command,
                        work.command_id,
                        at=self._clock(),
                    )
                    wakeup = self._retry_wakeups.pop(work.command_id, None)
                    if wakeup is not None:
                        wakeup.cancel()
            except asyncio.CancelledError:
                if not child.cancelled():
                    raise
            except Exception:  # noqa: BLE001 - 다음 startup에서 durable state를 복구한다.
                self._background_errors[work.task_id] = "background_execution_failed"
                if work.command_id is not None:
                    await asyncio.to_thread(
                        self._store.record_runtime_command_failure,
                        work.command_id,
                        error_code="background_execution_failed",
                        at=self._clock(),
                    )
            finally:
                self._active.pop(work.task_id, None)
                self._active_fingerprints.pop(work.task_id, None)
                self._queued_commands.discard((work.task_id, work.fingerprint))
                if not self._stopping:
                    if self._prefer_deferred:
                        await self._pump_deferred_work()
                        await self._pump_pending_commands()
                    else:
                        await self._pump_pending_commands()
                        await self._pump_deferred_work()
                    self._prefer_deferred = not self._prefer_deferred
                self._queue.task_done()

    async def _dispatch(self, work: _WorkItem) -> object:
        if isinstance(work, _StartWork):
            try:
                return await self._orchestrator.start(
                    work.request,
                    thread_id=work.task_id,
                    initial_task=work.initial_task,
                )
            except OrchestrationStartError:
                return await self._orchestrator.recover(thread_id=work.task_id)
        if isinstance(work, _RecoverWork):
            return await self._orchestrator.recover(thread_id=work.thread_id)
        if isinstance(work, _ResumeWork):
            try:
                return await self._orchestrator.resume(
                    thread_id=work.thread_id,
                    response=work.response,
                )
            except ApprovalResumeError:
                result = await self._orchestrator.recover(thread_id=work.thread_id)
                if not await self._terminal_command_matches(work, result):
                    raise
                return result
        try:
            return await self._orchestrator.cancel(
                thread_id=work.thread_id,
                reason=work.reason,
            )
        except OrchestrationCancellationError:
            result = await self._orchestrator.recover(thread_id=work.thread_id)
            if not await self._terminal_command_matches(work, result):
                raise
            return result

    async def _terminal_command_matches(
        self,
        work: _ResumeWork | _CancelWork,
        recovery_result: object,
    ) -> bool:
        task = await asyncio.to_thread(self._store.get_task, work.task_id)
        recovered_task = getattr(recovery_result, "task", None)
        if task is None or recovered_task != task:
            return False
        events = await asyncio.to_thread(self._store.list_task_events, work.task_id)
        if isinstance(work, _CancelWork):
            return task.status is Status.CANCELLED and any(
                event.task_version == task.version
                and event.event_type == "TASK_CANCELLED"
                and event.payload.get("reason") == work.reason
                and event.occurred_at == task.updated_at
                for event in events
            )
        decision_id = work.response.decision_id
        expected_type = "TASK_APPROVED" if work.response.accepted else "TASK_REJECTED"
        if work.response.accepted:
            approval = work.response.approval
            if (
                approval is None
                or approval.task_id != task.task_id
                or approval.plan_hash != task.plan_hash
            ):
                return False
            expected_version = approval.task_version + 1
        else:
            if task.status is not Status.REJECTED:
                return False
            expected_version = task.version
        return any(
            event.event_type == expected_type
            and event.task_version == expected_version
            and event.payload.get("decision_id") == decision_id
            and event.payload.get("accepted") is work.response.accepted
            and (
                event.occurred_at <= task.updated_at
                if work.response.accepted
                else event.occurred_at == task.updated_at
            )
            for event in events
        )

    async def _pump_pending_commands(
        self,
    ) -> None:
        """Queue의 남은 용량만큼 durable pending command를 다시 올린다."""

        if self._stopping:
            return
        commands = await asyncio.to_thread(self._store.list_pending_runtime_commands)
        for command in commands:
            if self._stopping:
                return
            eligible_at = command.updated_at + self._retry_delay(command.attempt_count)
            if command.attempt_count > 0 and eligible_at > self._clock():
                self._schedule_retry(command.command_id, eligible_at)
                continue
            try:
                work = await self._work_from_command(command)
                await self._enqueue(work)
            except ApplicationBusyError:
                return

    async def _pump_deferred_work(self) -> None:
        if self._stopping:
            return
        while self._deferred_work:
            if self._stopping:
                return
            work = self._deferred_work[0]
            try:
                await self._enqueue(work)
            except ApplicationBusyError:
                return
            self._deferred_work.popleft()
            self._deferred_keys.discard((work.task_id, work.fingerprint))

    @staticmethod
    def _retry_delay(attempt_count: int) -> timedelta:
        """실패 횟수에 따라 최대 5분까지 durable exponential backoff한다."""

        if attempt_count <= 0:
            return timedelta(0)
        exponent = min(attempt_count - 1, 4)
        return timedelta(seconds=min(30 * (2**exponent), 300))

    def _schedule_retry(self, command_id: str, eligible_at: datetime) -> None:
        if self._stopping or command_id in self._retry_wakeups:
            return
        delay = max((eligible_at - self._clock()).total_seconds(), 0)
        self._retry_wakeups[command_id] = asyncio.create_task(
            self._retry_when_eligible(command_id, delay),
            name=f"runtime-command-retry:{command_id}",
        )

    async def _retry_when_eligible(self, command_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._retry_wakeups.pop(command_id, None)
        if self._started and not self._stopping and not self._closed:
            await self._pump_pending_commands()

    async def _work_from_command(self, command: RuntimeCommandRecord) -> _WorkItem:
        task = await asyncio.to_thread(self._store.get_task, command.task_id)
        if task is None:
            raise ApplicationConflictError("Runtime command의 Task가 없습니다.")
        if command.command_type is RuntimeCommandType.START:
            events = await asyncio.to_thread(
                self._store.list_task_events,
                task.task_id,
            )
            request = self._request_from_events(task, events)
            initial_task = Task.receive(
                task_id=task.task_id,
                input=task.input,
                at=task.created_at,
            )
            return _StartWork(
                request=request,
                initial_task=initial_task,
                command_id=command.command_id,
                fingerprint=command.fingerprint,
            )
        if command.command_type is RuntimeCommandType.APPROVAL:
            response = command.payload.get("response")
            if not isinstance(response, Mapping):
                raise ApplicationConflictError(
                    "Approval command payload가 올바르지 않습니다."
                )
            try:
                restored = ApprovalResponse.from_snapshot(response)
            except (KeyError, TypeError, ValueError):
                raise ApplicationConflictError(
                    "Approval command payload가 올바르지 않습니다."
                ) from None
            return _ResumeWork(
                task_id=task.task_id,
                thread_id=task.task_id,
                response=restored,
                command_id=command.command_id,
                fingerprint=command.fingerprint,
            )
        if command.command_type is RuntimeCommandType.CANCEL:
            reason = command.payload.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise ApplicationConflictError(
                    "Cancel command payload가 올바르지 않습니다."
                )
            return _CancelWork(
                task_id=task.task_id,
                thread_id=task.task_id,
                reason=reason,
                command_id=command.command_id,
                fingerprint=command.fingerprint,
            )
        raise ApplicationConflictError("지원하지 않는 Runtime command입니다.")

    async def _runtime_command_replay(
        self,
        task_id: str,
        command_type: RuntimeCommandType,
        fingerprint: str,
    ) -> RuntimeCommandRecord | None:
        commands = await asyncio.to_thread(self._store.list_runtime_commands, task_id)
        for command in commands:
            if command.fingerprint == fingerprint:
                if command.command_type is not command_type:
                    raise ApplicationConflictError(
                        "같은 command fingerprint의 종류가 다릅니다."
                    )
                return command
        return None

    @staticmethod
    def _command_fingerprint(
        command_type: RuntimeCommandType,
        payload: Mapping[str, object],
    ) -> str:
        canonical = json.dumps(
            {"command_type": command_type.value, "payload": dict(payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _runtime_command(
        *,
        task_id: str,
        command_type: RuntimeCommandType,
        fingerprint: str,
        payload: Mapping[str, object],
        at: datetime,
    ) -> RuntimeCommandDraft:
        identity = sha256(f"{task_id}:{fingerprint}".encode()).hexdigest()
        return RuntimeCommandDraft(
            command_id=f"command:{identity}",
            task_id=task_id,
            command_type=command_type,
            fingerprint=fingerprint,
            payload=payload,
            created_at=at,
        )

    def _require_started(self) -> None:
        if not self._started or self._stopping or self._closed:
            raise ApplicationBusyError("RuntimeApplication이 실행 중이 아닙니다.")

    @staticmethod
    def _fingerprint(submission: Submission) -> str:
        canonical = json.dumps(
            {"kind": submission.kind.value, "payload": dict(submission.payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _idempotency(
        submission: Submission,
        fingerprint: str,
        *,
        at: datetime,
    ) -> IdempotencyKey | None:
        if submission.kind is SubmissionKind.USER_TASK:
            if submission.idempotency_key is None:
                return None
            namespace, key = "task", submission.idempotency_key
        elif submission.kind is SubmissionKind.ALERT:
            namespace, key = "webhook.alert", submission.payload.get("alert_id", "")
        else:
            namespace, key = "webhook.ticket", submission.payload.get("ticket_id", "")
        return IdempotencyKey(namespace, key, fingerprint, at)

    @staticmethod
    def _request_from_submission(
        task_id: str,
        submission: Submission,
    ) -> OrchestratorInput:
        payload = submission.payload
        try:
            if submission.kind is SubmissionKind.USER_TASK:
                if set(payload) != {"input"}:
                    raise ValueError
                return UserTaskInput(task_id=task_id, input=payload["input"])
            if submission.kind is SubmissionKind.ALERT:
                if set(payload) != {"alert_id", "severity", "message"}:
                    raise ValueError
                return AlertInput(
                    task_id=task_id,
                    alert_id=payload["alert_id"],
                    severity=payload["severity"],
                    message=payload["message"],
                )
            if set(payload) != {"ticket_id", "subject", "description"}:
                raise ValueError
            return TicketInput(
                task_id=task_id,
                ticket_id=payload["ticket_id"],
                subject=payload["subject"],
                description=payload["description"],
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "Submission payload가 요청 종류와 일치하지 않습니다."
            ) from None

    @staticmethod
    def _request_from_events(
        task: Task, events: tuple[TaskEvent, ...]
    ) -> OrchestratorInput:
        if not events:
            raise ApplicationConflictError("RECEIVED Task의 요청 event가 없습니다.")
        request = events[0].payload.get("request")
        if not isinstance(request, Mapping):
            raise ApplicationConflictError("RECEIVED Task 요청 metadata가 없습니다.")
        kind = request.get("kind")
        if kind == "user_task":
            submission = Submission(
                SubmissionKind.USER_TASK, {"input": request.get("input")}
            )
        elif kind == "alert":
            submission = Submission(
                SubmissionKind.ALERT,
                {
                    "alert_id": request.get("alert_id"),
                    "severity": request.get("severity"),
                    "message": request.get("message"),
                },
            )
        elif kind == "ticket":
            submission = Submission(
                SubmissionKind.TICKET,
                {
                    "ticket_id": request.get("ticket_id"),
                    "subject": request.get("subject"),
                    "description": request.get("description"),
                },
            )
        else:
            raise ApplicationConflictError(
                "RECEIVED Task 요청 종류가 올바르지 않습니다."
            )
        return RuntimeApplication._request_from_submission(task.task_id, submission)

    @staticmethod
    def _approval_view(events: tuple[TaskEvent, ...]) -> ApprovalView | None:
        for event in reversed(events):
            value = event.payload.get("approval_request")
            if not isinstance(value, Mapping):
                continue
            plan = value.get("plan")
            if not isinstance(plan, Mapping):
                continue
            steps = plan.get("steps")
            if not isinstance(steps, list) or not all(
                isinstance(step, str) for step in steps
            ):
                continue
            try:
                return ApprovalView(
                    task_version=int(value["task_version"]),
                    plan_hash=str(value["plan_hash"]),
                    plan_summary=str(plan["summary"]),
                    plan_steps=tuple(steps),
                    agent_id=str(value["agent_id"]),
                    action=str(value["action"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _result_metadata(
        events: tuple[TaskEvent, ...],
    ) -> tuple[str | None, tuple[str, ...]]:
        output: str | None = None
        errors: tuple[str, ...] = ()
        for event in events:
            candidate_output = event.payload.get("output")
            if isinstance(candidate_output, str):
                output = candidate_output
            candidate_errors = event.payload.get("errors")
            if isinstance(candidate_errors, list) and all(
                isinstance(error, str) for error in candidate_errors
            ):
                errors = tuple(candidate_errors)
        return output, errors


__all__ = [
    "AcceptedTask",
    "ApplicationBusyError",
    "ApplicationConflictError",
    "ApplicationError",
    "ApplicationNotFoundError",
    "ApprovalCommand",
    "ApprovalDecision",
    "ApprovalView",
    "CancelCommand",
    "InProcessExecutionCoordinator",
    "ResultView",
    "RuntimeApplication",
    "RuntimeOrchestrator",
    "SQLiteApprovalConsumer",
    "SQLiteLifecycleJournal",
    "Submission",
    "SubmissionKind",
    "TaskApplication",
    "TaskView",
    "build_runtime",
]
