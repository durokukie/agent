"""Provider 중립 supervisor graph와 runtime용 facade."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import CancelledError as ConcurrentCancelledError
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_system.agents import (
    AgentNotFoundError,
    AgentOutcome,
    AgentRegistry,
    AgentRequest,
    AgentResult,
)

from . import (
    AgentRun,
    Approval,
    ApprovalTaskMismatchError,
    LifecycleError,
    Phase,
    PlanChangedError,
    StaleApprovalError,
    Status,
    Task,
    WorkflowRun,
)


class RequestKind(StrEnum):
    """Supervisor가 구분하는 외부 요청 종류다."""

    USER_TASK = "user_task"
    ALERT = "alert"
    TICKET = "ticket"


class ActionKind(StrEnum):
    """Route가 외부 상태를 변경하는지 나타낸다."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


class FailureCode(StrEnum):
    """외부 예외 내용을 노출하지 않는 결정 가능한 실패 분류다."""

    GOVERNANCE_REJECTED = "governance_rejected"
    HUMAN_REJECTED = "human_rejected"
    APPROVAL_TASK_MISMATCH = "approval_task_mismatch"
    APPROVAL_PLAN_CHANGED = "approval_plan_changed"
    APPROVAL_STALE = "approval_stale"
    AGENT_FAILURE = "agent_failure"
    AGENT_EXCEPTION = "agent_exception"
    AGENT_RESULT_MISMATCH = "agent_result_mismatch"
    ROUTE_NOT_FOUND = "route_not_found"
    RETRY_EXHAUSTED = "retry_exhausted"
    CLASSIFICATION_FAILED = "classification_failed"
    GOVERNANCE_FAILED = "governance_failed"


class ApprovalConsumeStatus(StrEnum):
    """Approval decision의 원자적 소비 결과다."""

    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    CONFLICT = "conflict"


class ExecutionClaimStatus(StrEnum):
    """열린 AgentRun 복구 실행권의 획득 결과다."""

    CLAIMED = "claimed"
    BUSY = "busy"


class ClassificationError(ValueError):
    """Classifier 응답이 호출 또는 schema 검증에 실패했을 때 발생한다."""


class ApprovalResumeError(RuntimeError):
    """승인 대기가 없는 checkpoint thread를 재개하려 할 때 발생한다."""


class OrchestrationStartError(RuntimeError):
    """이미 소유된 checkpoint thread로 새 실행을 시작할 때 발생한다."""


class OrchestrationStateError(RuntimeError):
    """요청한 checkpoint thread에 orchestration state가 없을 때 발생한다."""


class OrchestrationDependencyError(RuntimeError):
    """주입된 orchestration seam이 예기치 않게 실패했을 때 발생한다."""


class OrchestrationRecoveryError(RuntimeError):
    """다른 runner가 같은 열린 AgentRun을 복구 중일 때 발생한다."""


def _require_text(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}는 비어 있을 수 없습니다.")


def _snapshot_bool(snapshot: Mapping[str, object], field_name: str) -> bool:
    value = snapshot[field_name]
    if not isinstance(value, bool):
        raise TypeError(f"{field_name}은 bool이어야 합니다.")
    return value


def _snapshot_text(snapshot: Mapping[str, object], field_name: str) -> str:
    value = snapshot[field_name]
    if not isinstance(value, str):
        raise TypeError(f"{field_name}은 문자열이어야 합니다.")
    return value


def _snapshot_integer(snapshot: Mapping[str, object], field_name: str) -> int:
    value = snapshot[field_name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name}은 정수여야 합니다.")
    return value


def _snapshot_mapping(
    snapshot: Mapping[str, object], field_name: str
) -> Mapping[str, object]:
    value = snapshot[field_name]
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name}은 mapping이어야 합니다.")
    return value


def _snapshot_list(snapshot: Mapping[str, object], field_name: str) -> list[object]:
    value = snapshot[field_name]
    if not isinstance(value, list):
        raise TypeError(f"{field_name}은 list여야 합니다.")
    return value


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """승인 binding에 사용하는 변경 불가능한 action plan이다."""

    summary: str
    steps: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.summary, field_name="plan summary")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ValueError("plan steps에는 하나 이상의 단계가 필요합니다.")
        for step in self.steps:
            _require_text(step, field_name="plan step")

    @property
    def plan_hash(self) -> str:
        """내용 전체에 결합된 canonical SHA-256 hash를 반환한다."""

        encoded = json.dumps(
            {"steps": self.steps, "summary": self.summary},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"sha256:{sha256(encoded).hexdigest()}"

    def to_snapshot(self) -> dict[str, object]:
        return {"summary": self.summary, "steps": list(self.steps)}

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> ActionPlan:
        steps = snapshot["steps"]
        if not isinstance(steps, list):
            raise TypeError("plan steps snapshot은 list여야 합니다.")
        if not all(isinstance(step, str) for step in steps):
            raise TypeError("plan steps 항목은 문자열이어야 합니다.")
        return cls(summary=_snapshot_text(snapshot, "summary"), steps=tuple(steps))


@dataclass(frozen=True, slots=True)
class UserTaskInput:
    """사용자가 직접 제출한 일반 작업 입력이다."""

    task_id: str
    input: str

    def __post_init__(self) -> None:
        _require_text(self.task_id, field_name="task_id")
        _require_text(self.input, field_name="input")

    @property
    def kind(self) -> RequestKind:
        return RequestKind.USER_TASK

    @property
    def text(self) -> str:
        return self.input

    def to_snapshot(self) -> dict[str, object]:
        return {"kind": self.kind.value, "task_id": self.task_id, "input": self.input}


@dataclass(frozen=True, slots=True)
class AlertInput:
    """관측 시스템에서 들어온 경보 입력이다."""

    task_id: str
    alert_id: str
    severity: str
    message: str

    def __post_init__(self) -> None:
        _require_text(self.task_id, field_name="task_id")
        _require_text(self.alert_id, field_name="alert_id")
        _require_text(self.severity, field_name="severity")
        _require_text(self.message, field_name="message")

    @property
    def kind(self) -> RequestKind:
        return RequestKind.ALERT

    @property
    def text(self) -> str:
        return self.message

    def to_snapshot(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "task_id": self.task_id,
            "alert_id": self.alert_id,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TicketInput:
    """외부 ticket에서 들어온 orchestration 입력이다."""

    task_id: str
    ticket_id: str
    subject: str
    description: str

    def __post_init__(self) -> None:
        _require_text(self.task_id, field_name="task_id")
        _require_text(self.ticket_id, field_name="ticket_id")
        _require_text(self.subject, field_name="subject")
        _require_text(self.description, field_name="description")

    @property
    def kind(self) -> RequestKind:
        return RequestKind.TICKET

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.description}"

    def to_snapshot(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "task_id": self.task_id,
            "ticket_id": self.ticket_id,
            "subject": self.subject,
            "description": self.description,
        }


OrchestratorInput = UserTaskInput | AlertInput | TicketInput


def _input_from_snapshot(snapshot: Mapping[str, object]) -> OrchestratorInput:
    kind = RequestKind(_snapshot_text(snapshot, "kind"))
    if kind is RequestKind.USER_TASK:
        return UserTaskInput(
            task_id=_snapshot_text(snapshot, "task_id"),
            input=_snapshot_text(snapshot, "input"),
        )
    if kind is RequestKind.ALERT:
        return AlertInput(
            task_id=_snapshot_text(snapshot, "task_id"),
            alert_id=_snapshot_text(snapshot, "alert_id"),
            severity=_snapshot_text(snapshot, "severity"),
            message=_snapshot_text(snapshot, "message"),
        )
    return TicketInput(
        task_id=_snapshot_text(snapshot, "task_id"),
        ticket_id=_snapshot_text(snapshot, "ticket_id"),
        subject=_snapshot_text(snapshot, "subject"),
        description=_snapshot_text(snapshot, "description"),
    )


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """분류기가 남기는 추적 가능한 동적 routing 결정이다."""

    request_kind: RequestKind
    agent_id: str
    action: ActionKind
    reason: str
    plan: ActionPlan | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_kind, RequestKind):
            raise TypeError("request_kind는 RequestKind여야 합니다.")
        if not isinstance(self.action, ActionKind):
            raise TypeError("action은 ActionKind여야 합니다.")
        _require_text(self.agent_id, field_name="agent_id")
        _require_text(self.reason, field_name="reason")
        if self.action is ActionKind.MUTATING and self.plan is None:
            raise ValueError("mutating route에는 ActionPlan이 필요합니다.")
        if self.action is ActionKind.READ_ONLY and self.plan is not None:
            raise ValueError("read-only route에는 ActionPlan을 지정할 수 없습니다.")

    def to_snapshot(self) -> dict[str, object]:
        return {
            "request_kind": self.request_kind.value,
            "agent_id": self.agent_id,
            "action": self.action.value,
            "reason": self.reason,
            "plan": None if self.plan is None else self.plan.to_snapshot(),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> RoutingDecision:
        plan = snapshot["plan"]
        return cls(
            request_kind=RequestKind(_snapshot_text(snapshot, "request_kind")),
            agent_id=_snapshot_text(snapshot, "agent_id"),
            action=ActionKind(_snapshot_text(snapshot, "action")),
            reason=_snapshot_text(snapshot, "reason"),
            plan=(
                None
                if plan is None
                else ActionPlan.from_snapshot(_snapshot_mapping(snapshot, "plan"))
            ),
        )


class RequestClassifier(Protocol):
    """외부 요청을 provider 중립 routing 결정으로 분류한다."""

    async def classify(self, request: OrchestratorInput) -> RoutingDecision:
        """요청의 route와 action 성격을 반환한다."""


class FakeRequestClassifier:
    """요청 종류별 고정 결정을 반환하는 결정 가능한 classifier다."""

    def __init__(self, decisions: Mapping[RequestKind, RoutingDecision]) -> None:
        self._decisions = dict(decisions)
        self.received_requests: list[OrchestratorInput] = []

    async def classify(self, request: OrchestratorInput) -> RoutingDecision:
        """요청을 기록하고 설정된 결정을 반환한다."""

        self.received_requests.append(request)
        return self._decisions[request.kind]


class _ActionPlanSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    steps: tuple[str, ...] = Field(min_length=1)


class _RoutingDecisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_kind: RequestKind
    agent_id: str = Field(min_length=1)
    action: ActionKind
    reason: str = Field(min_length=1)
    plan: _ActionPlanSchema | None


class ChatModelRequestClassifier:
    """주입된 LangChain ChatModel 응답을 검증된 routing 결정으로 변환한다."""

    def __init__(self, model: BaseChatModel) -> None:
        if not isinstance(model, BaseChatModel):
            raise TypeError("model은 BaseChatModel이어야 합니다.")
        self._model = model

    async def classify(self, request: OrchestratorInput) -> RoutingDecision:
        """Provider 중립 JSON schema로 model 응답을 검증한다."""

        prompt = json.dumps(
            {
                "instruction": (
                    "요청을 분류해 request_kind, agent_id, action, reason, plan을 "
                    "JSON object로 반환하세요. mutating action에만 plan을 지정하세요."
                ),
                "request": request.to_snapshot(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            message = await self._model.ainvoke([HumanMessage(content=prompt)])
        except (asyncio.CancelledError, ConcurrentCancelledError):
            raise asyncio.CancelledError from None
        except Exception:  # noqa: BLE001 - provider 상세를 경계에서 제거한다.
            raise ClassificationError("Classifier model 호출에 실패했습니다.") from None
        try:
            if not isinstance(message.content, str):
                raise ClassificationError("Classifier 응답은 JSON 문자열이어야 합니다.")
            parsed = _RoutingDecisionSchema.model_validate_json(message.content)
            plan = (
                None
                if parsed.plan is None
                else ActionPlan(
                    summary=parsed.plan.summary,
                    steps=parsed.plan.steps,
                )
            )
            decision = RoutingDecision(
                request_kind=parsed.request_kind,
                agent_id=parsed.agent_id,
                action=parsed.action,
                reason=parsed.reason,
                plan=plan,
            )
            if decision.request_kind is not request.kind:
                raise ClassificationError(
                    "Classifier request_kind가 실제 입력 종류와 다릅니다."
                )
            return decision
        except ClassificationError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise ClassificationError(
                "Classifier 응답이 routing schema와 맞지 않습니다."
            ) from None


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    """변경 plan에 대한 governance 판정이다."""

    approved: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise TypeError("governance approved는 bool이어야 합니다.")
        _require_text(self.reason, field_name="governance reason")

    def to_snapshot(self) -> dict[str, object]:
        return {"approved": self.approved, "reason": self.reason}

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> GovernanceDecision:
        return cls(
            approved=_snapshot_bool(snapshot, "approved"),
            reason=_snapshot_text(snapshot, "reason"),
        )


class Governance(Protocol):
    """변경 plan을 승인 절차에 올릴 수 있는지 판정한다."""

    async def evaluate(
        self,
        request: OrchestratorInput,
        routing: RoutingDecision,
    ) -> GovernanceDecision:
        """정책 판정과 근거를 반환한다."""


class FakeGovernance:
    """고정된 governance 판정을 반환하는 test adapter다."""

    def __init__(self, *, approved: bool, reason: str) -> None:
        self._decision = GovernanceDecision(approved=approved, reason=reason)
        self.received_requests: list[tuple[OrchestratorInput, RoutingDecision]] = []

    async def evaluate(
        self,
        request: OrchestratorInput,
        routing: RoutingDecision,
    ) -> GovernanceDecision:
        self.received_requests.append((request, routing))
        return self._decision


class _GraphState(TypedDict, total=False):
    request: dict[str, object]
    task: dict[str, object]
    workflow: dict[str, object]
    routing: dict[str, object]
    governance: dict[str, object]
    approval: dict[str, object]
    agent_runs: list[dict[str, object]]
    output: str | None
    errors: list[str]


class _AsyncCheckpointerAdapter(BaseCheckpointSaver[object]):
    """동기 saver도 async graph에서 사용할 수 있게 public API만 중계한다."""

    def __init__(self, inner: BaseCheckpointSaver) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner

    @property
    def config_specs(self) -> list:
        return self._inner.config_specs

    def get_next_version(self, current: object | None, channel: None) -> object:
        return self._inner.get_next_version(current, channel)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        try:
            return await self._inner.aget_tuple(config)
        except NotImplementedError:
            return await asyncio.to_thread(self._inner.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, object] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        try:
            async for checkpoint in self._inner.alist(
                config,
                filter=filter,
                before=before,
                limit=limit,
            ):
                yield checkpoint
            return
        except NotImplementedError:
            checkpoints = await asyncio.to_thread(
                lambda: list(
                    self._inner.list(
                        config,
                        filter=filter,
                        before=before,
                        limit=limit,
                    )
                )
            )
        for checkpoint in checkpoints:
            yield checkpoint

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        try:
            return await self._inner.aput(
                config,
                checkpoint,
                metadata,
                new_versions,
            )
        except NotImplementedError:
            return await asyncio.to_thread(
                self._inner.put,
                config,
                checkpoint,
                metadata,
                new_versions,
            )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        try:
            await self._inner.aput_writes(config, writes, task_id, task_path)
        except NotImplementedError:
            await asyncio.to_thread(
                self._inner.put_writes,
                config,
                writes,
                task_id,
                task_path,
            )

    async def adelete_thread(self, thread_id: str) -> None:
        try:
            await self._inner.adelete_thread(thread_id)
        except NotImplementedError:
            await asyncio.to_thread(self._inner.delete_thread, thread_id)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Graph interrupt가 사람에게 노출하는 plan 승인 요청이다."""

    task_id: str
    task_version: int
    plan_hash: str
    plan: ActionPlan
    agent_id: str
    action: ActionKind

    def __post_init__(self) -> None:
        _require_text(self.task_id, field_name="approval task_id")
        if isinstance(self.task_version, bool) or not isinstance(
            self.task_version, int
        ):
            raise TypeError("approval task_version은 정수여야 합니다.")
        if self.task_version <= 0:
            raise ValueError("approval task_version은 양수여야 합니다.")
        _require_text(self.plan_hash, field_name="approval plan_hash")
        if not isinstance(self.plan, ActionPlan):
            raise TypeError("approval plan은 ActionPlan이어야 합니다.")
        _require_text(self.agent_id, field_name="approval agent_id")
        if not isinstance(self.action, ActionKind):
            raise TypeError("approval action은 ActionKind여야 합니다.")
        if self.plan.plan_hash != self.plan_hash:
            raise ValueError("ApprovalRequest plan과 plan_hash가 일치하지 않습니다.")
        if self.action is not ActionKind.MUTATING:
            raise ValueError("ApprovalRequest는 mutating action만 허용합니다.")

    @property
    def plan_summary(self) -> str:
        return self.plan.summary

    @property
    def binding(self) -> tuple[str, int, str]:
        return self.task_id, self.task_version, self.plan_hash

    def to_snapshot(self) -> dict[str, object]:
        return {
            "kind": "approval_required",
            "task_id": self.task_id,
            "task_version": self.task_version,
            "plan_hash": self.plan_hash,
            "plan": self.plan.to_snapshot(),
            "agent_id": self.agent_id,
            "action": self.action.value,
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> ApprovalRequest:
        if _snapshot_text(snapshot, "kind") != "approval_required":
            raise ValueError("ApprovalRequest kind가 올바르지 않습니다.")
        return cls(
            task_id=_snapshot_text(snapshot, "task_id"),
            task_version=_snapshot_integer(snapshot, "task_version"),
            plan_hash=_snapshot_text(snapshot, "plan_hash"),
            plan=ActionPlan.from_snapshot(_snapshot_mapping(snapshot, "plan")),
            agent_id=_snapshot_text(snapshot, "agent_id"),
            action=ActionKind(_snapshot_text(snapshot, "action")),
        )


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    """사람이 interrupt에 제출하는 승인 또는 거절 결정이다."""

    decision_id: str
    accepted: bool
    approval: Approval | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.decision_id, field_name="decision_id")
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted는 bool이어야 합니다.")
        if self.reason is not None:
            _require_text(self.reason, field_name="approval reason")
        if self.accepted and self.approval is None:
            raise ValueError("승인 응답에는 Approval binding이 필요합니다.")
        if not self.accepted:
            if self.approval is not None:
                raise ValueError("거절 응답에는 Approval을 지정할 수 없습니다.")
            if self.reason is None:
                raise ValueError("거절 응답에는 reason이 필요합니다.")

    @classmethod
    def reject(cls, *, decision_id: str, reason: str) -> ApprovalResponse:
        """사람의 거절 응답을 만든다."""

        return cls(decision_id=decision_id, accepted=False, reason=reason)

    @classmethod
    def approve(
        cls,
        request: ApprovalRequest,
        *,
        decision_id: str,
        at: datetime,
    ) -> ApprovalResponse:
        """interrupt binding과 정확히 일치하는 승인 응답을 만든다."""

        return cls(
            decision_id=decision_id,
            accepted=True,
            approval=Approval(
                task_id=request.task_id,
                task_version=request.task_version,
                plan_hash=request.plan_hash,
                approved_at=at,
            ),
        )

    def to_snapshot(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "accepted": self.accepted,
            "approval": None if self.approval is None else self.approval.to_snapshot(),
            "reason": self.reason,
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> ApprovalResponse:
        approval = snapshot["approval"]
        reason = snapshot["reason"]
        return cls(
            decision_id=_snapshot_text(snapshot, "decision_id"),
            accepted=_snapshot_bool(snapshot, "accepted"),
            approval=(
                None
                if approval is None
                else Approval.from_snapshot(_snapshot_mapping(snapshot, "approval"))
            ),
            reason=None if reason is None else _snapshot_text(snapshot, "reason"),
        )


@dataclass(frozen=True, slots=True)
class ApprovalConsumeResult:
    """ApprovalConsumer가 저장한 exact Task successor와 소비 상태다."""

    status: ApprovalConsumeStatus
    task: Task
    failure: FailureCode | None = None


class ApprovalConsumer(Protocol):
    """Approval decision을 Task binding과 원자적으로 소비한다."""

    async def consume(
        self,
        *,
        task: Task,
        response: ApprovalResponse,
        at: datetime,
    ) -> ApprovalConsumeResult:
        """정확한 persisted successor 또는 replay/conflict를 반환한다."""


class FakeApprovalConsumer:
    """한 process 안에서 원자적 소비를 재현하는 결정 가능한 adapter다."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._decisions: dict[str, tuple[dict[str, object], ApprovalConsumeResult]] = {}
        self._bindings: dict[
            tuple[str, int, str], tuple[str, ApprovalConsumeResult]
        ] = {}

    async def consume(
        self,
        *,
        task: Task,
        response: ApprovalResponse,
        at: datetime,
    ) -> ApprovalConsumeResult:
        async with self._lock:
            snapshot = response.to_snapshot()
            previous = self._decisions.get(response.decision_id)
            if previous is not None:
                previous_snapshot, result = previous
                status = (
                    ApprovalConsumeStatus.ALREADY_APPLIED
                    if previous_snapshot == snapshot
                    else ApprovalConsumeStatus.CONFLICT
                )
                return ApprovalConsumeResult(status, result.task, result.failure)
            failure: FailureCode | None = None
            if not response.accepted:
                successor = task.transition(Status.REJECTED, at=at)
                failure = FailureCode.HUMAN_REJECTED
            else:
                assert response.approval is not None
                try:
                    successor = task.transition(
                        Status.RUNNING, approval=response.approval, at=at
                    )
                except ApprovalTaskMismatchError:
                    successor = task.transition(Status.REJECTED, at=at)
                    failure = FailureCode.APPROVAL_TASK_MISMATCH
                except PlanChangedError:
                    successor = task.transition(Status.REJECTED, at=at)
                    failure = FailureCode.APPROVAL_PLAN_CHANGED
                except StaleApprovalError:
                    successor = task.transition(Status.REJECTED, at=at)
                    failure = FailureCode.APPROVAL_STALE
                binding = response.approval.binding
                existing = self._bindings.get(binding)
                if existing is not None:
                    _, existing_result = existing
                    return ApprovalConsumeResult(
                        ApprovalConsumeStatus.CONFLICT,
                        existing_result.task,
                        existing_result.failure,
                    )
            result = ApprovalConsumeResult(
                ApprovalConsumeStatus.APPLIED, successor, failure
            )
            self._decisions[response.decision_id] = (snapshot, result)
            if response.approval is not None:
                self._bindings[response.approval.binding] = (
                    response.decision_id,
                    result,
                )
            return result


@dataclass(frozen=True, slots=True)
class ExecutionClaimResult:
    """ExecutionCoordinator의 결정 가능한 claim 결과다."""

    status: ExecutionClaimStatus

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExecutionClaimStatus):
            raise TypeError("status는 ExecutionClaimStatus여야 합니다.")


class ExecutionCoordinator(Protocol):
    """열린 AgentRun의 process-local 복구 실행권을 조정한다."""

    async def claim(self, *, thread_id: str, agent_run_id: str) -> ExecutionClaimResult:
        """thread와 AgentRun에 결합된 실행권 획득 결과를 반환한다."""

    async def release(self, *, thread_id: str, agent_run_id: str) -> None:
        """획득했던 실행권을 해제한다."""


class FakeExecutionCoordinator:
    """단일 process 복구 경합을 결정 가능하게 조정하는 test adapter다."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._claims: set[tuple[str, str]] = set()

    async def claim(self, *, thread_id: str, agent_run_id: str) -> ExecutionClaimResult:
        key = (thread_id, agent_run_id)
        async with self._lock:
            if key in self._claims:
                return ExecutionClaimResult(ExecutionClaimStatus.BUSY)
            self._claims.add(key)
            return ExecutionClaimResult(ExecutionClaimStatus.CLAIMED)

    async def release(self, *, thread_id: str, agent_run_id: str) -> None:
        async with self._lock:
            self._claims.discard((thread_id, agent_run_id))


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """runtime과 background runner가 소비하는 orchestration 결과다."""

    task: Task
    workflow: WorkflowRun
    routing: RoutingDecision | None
    governance: GovernanceDecision | None
    agent_runs: tuple[AgentRun, ...]
    output: str | None
    interrupt: ApprovalRequest | None
    errors: tuple[FailureCode, ...]


class OrchestratorService:
    """Compiled LangGraph와 checkpoint 세부사항을 숨기는 실행 facade다."""

    def __init__(
        self,
        *,
        classifier: RequestClassifier,
        governance: Governance,
        approval_consumer: ApprovalConsumer,
        execution_coordinator: ExecutionCoordinator,
        registry: AgentRegistry,
        max_agent_runs: int,
        checkpointer: BaseCheckpointSaver,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
    ) -> None:
        self._classifier = classifier
        self._governance = governance
        self._approval_consumer = approval_consumer
        self._execution_coordinator = execution_coordinator
        self._registry = registry
        self._max_agent_runs = max_agent_runs
        self._clock = clock
        self._id_factory = id_factory
        self._thread_locks: dict[str, asyncio.Lock] = {}
        graph_checkpointer = _AsyncCheckpointerAdapter(checkpointer)
        self._graph = self._build_graph().compile(checkpointer=graph_checkpointer)

    def _build_graph(self) -> StateGraph[_GraphState]:
        graph = StateGraph(_GraphState)
        graph.add_node("classify", self._classify)
        graph.add_node("prepare_read_only", self._prepare_read_only)
        graph.add_node("govern", self._govern)
        graph.add_node("approval", self._approval)
        graph.add_node("issue_agent_run", self._issue_agent_run)
        graph.add_node("call_agent", self._call_agent)
        graph.add_node("escalate", self._escalate)
        graph.add_edge(START, "classify")
        graph.add_conditional_edges(
            "classify",
            self._after_classification,
            {
                "read_only": "prepare_read_only",
                "mutating": "govern",
                "terminal": END,
            },
        )
        graph.add_edge("prepare_read_only", "issue_agent_run")
        graph.add_conditional_edges(
            "govern",
            self._after_governance,
            {"approval": "approval", "terminal": END},
        )
        graph.add_conditional_edges(
            "approval",
            self._after_approval,
            {"execute": "issue_agent_run", "terminal": END},
        )
        graph.add_edge("issue_agent_run", "call_agent")
        graph.add_conditional_edges(
            "call_agent",
            self._after_execution,
            {
                "retry": "issue_agent_run",
                "escalate": "escalate",
                "terminal": END,
            },
        )
        graph.add_edge("escalate", END)
        return graph

    async def _classify(self, state: _GraphState) -> _GraphState:
        request = _input_from_snapshot(state["request"])
        workflow = WorkflowRun.from_snapshot(state["workflow"])
        try:
            routing = await self._classifier.classify(request)
            if (
                not isinstance(routing, RoutingDecision)
                or routing.request_kind is not request.kind
            ):
                raise ClassificationError(
                    "Classifier 결정이 실제 입력 종류와 일치하지 않습니다."
                )
        except Exception:  # noqa: BLE001 - classifier 경계의 상세 예외를 정규화한다.
            task = Task.from_snapshot(state["task"])
            task = task.transition(Status.FAILED, at=self._clock())
            return {
                "task": task.to_snapshot(),
                "errors": [FailureCode.CLASSIFICATION_FAILED.value],
            }
        for phase in (Phase.ANALYZING, Phase.PLANNING):
            workflow = workflow.advance(phase, at=self._clock())
        return {"routing": routing.to_snapshot(), "workflow": workflow.to_snapshot()}

    @staticmethod
    def _after_classification(state: _GraphState) -> str:
        if state.get("routing") is None:
            return "terminal"
        routing = RoutingDecision.from_snapshot(state["routing"])
        return routing.action.value

    def _prepare_read_only(self, state: _GraphState) -> _GraphState:
        workflow = WorkflowRun.from_snapshot(state["workflow"])
        workflow = workflow.advance(Phase.GOVERNING, at=self._clock())
        workflow = workflow.advance(Phase.EXECUTING, at=self._clock())
        return {"workflow": workflow.to_snapshot()}

    async def _govern(self, state: _GraphState) -> _GraphState:
        request = _input_from_snapshot(state["request"])
        routing = RoutingDecision.from_snapshot(state["routing"])
        task = Task.from_snapshot(state["task"])
        workflow = WorkflowRun.from_snapshot(state["workflow"])
        workflow = workflow.advance(Phase.GOVERNING, at=self._clock())
        try:
            decision = await self._governance.evaluate(request, routing)
            if not isinstance(decision, GovernanceDecision):
                raise TypeError("governance 결과가 GovernanceDecision이 아닙니다.")
        except Exception:  # noqa: BLE001 - governance 경계 예외를 격리한다.
            task = task.transition(Status.ESCALATED, at=self._clock())
            return {
                "task": task.to_snapshot(),
                "workflow": workflow.to_snapshot(),
                "errors": [FailureCode.GOVERNANCE_FAILED.value],
            }
        if not decision.approved:
            task = task.transition(Status.REJECTED, at=self._clock())
        else:
            assert routing.plan is not None
            task = task.update_plan(routing.plan.plan_hash, at=self._clock())
            task = task.transition(Status.WAITING_APPROVAL, at=self._clock())
        return {
            "task": task.to_snapshot(),
            "workflow": workflow.to_snapshot(),
            "governance": decision.to_snapshot(),
            "errors": (
                [FailureCode.GOVERNANCE_REJECTED.value] if not decision.approved else []
            ),
        }

    @staticmethod
    def _after_governance(state: _GraphState) -> str:
        task = Task.from_snapshot(state["task"])
        return "approval" if task.status is Status.WAITING_APPROVAL else "terminal"

    def _approval(self, state: _GraphState) -> _GraphState:
        task = Task.from_snapshot(state["task"])
        routing = RoutingDecision.from_snapshot(state["routing"])
        assert routing.plan is not None
        approval_request = ApprovalRequest(
            task_id=task.task_id,
            task_version=task.version,
            plan_hash=task.plan_hash or "",
            plan=routing.plan,
            agent_id=routing.agent_id,
            action=routing.action,
        )
        payload = interrupt(approval_request.to_snapshot())
        response = ApprovalResponse.from_snapshot(payload["response"])
        successor = Task.from_snapshot(payload["successor"])
        failure_value = payload.get("failure")
        failure = None if failure_value is None else FailureCode(failure_value)
        if failure is not None:
            expected = task.transition(Status.REJECTED, at=successor.updated_at)
            if successor != expected:
                raise OrchestrationStateError(
                    "ApprovalConsumer rejected successor가 정확하지 않습니다."
                )
            return {"task": successor.to_snapshot(), "errors": [failure.value]}
        assert response.approval is not None
        expected = task.transition(
            Status.RUNNING,
            approval=response.approval,
            at=successor.updated_at,
        )
        if successor != expected:
            raise OrchestrationStateError(
                "ApprovalConsumer approved successor가 정확하지 않습니다."
            )
        task = successor
        workflow = WorkflowRun.from_snapshot(state["workflow"])
        workflow = workflow.rebind_task(task, at=self._clock())
        workflow = workflow.advance(Phase.EXECUTING, at=self._clock())
        return {
            "task": task.to_snapshot(),
            "workflow": workflow.to_snapshot(),
            "approval": response.approval.to_snapshot(),
        }

    @staticmethod
    def _after_approval(state: _GraphState) -> str:
        task = Task.from_snapshot(state["task"])
        return "execute" if task.status is Status.RUNNING else "terminal"

    def _issue_agent_run(self, state: _GraphState) -> _GraphState:
        workflow = WorkflowRun.from_snapshot(state["workflow"])
        routing = RoutingDecision.from_snapshot(state["routing"])
        workflow, agent_run = workflow.begin_agent_run(
            agent_run_id=self._id_factory(),
            agent_id=routing.agent_id,
            at=self._clock(),
            retry=workflow.phase is Phase.VERIFYING,
        )
        return {
            "workflow": workflow.to_snapshot(),
            "agent_runs": [*state.get("agent_runs", []), agent_run.to_snapshot()],
        }

    async def _call_agent(self, state: _GraphState) -> _GraphState:
        request = _input_from_snapshot(state["request"])
        task = Task.from_snapshot(state["task"])
        workflow = WorkflowRun.from_snapshot(state["workflow"])
        routing = RoutingDecision.from_snapshot(state["routing"])
        agent_run_snapshots = list(state["agent_runs"])
        agent_run = AgentRun.from_snapshot(agent_run_snapshots[-1], workflow=workflow)
        failure: FailureCode | None = None
        result: AgentResult | None = None
        try:
            agent = self._registry.get(routing.agent_id)
            context: dict[str, object] = {
                "request_kind": request.kind.value,
                "routing": routing.reason,
                "routing_action": routing.action.value,
                "routing_agent_id": routing.agent_id,
                "execution_task_version": task.version,
                "agent_run_id": agent_run.agent_run_id,
                "budget_sequence": agent_run.budget_sequence,
            }
            if state.get("approval") is not None:
                approval = Approval.from_snapshot(state["approval"])
                assert routing.plan is not None
                context.update(
                    {
                        "approved_plan": routing.plan.to_snapshot(),
                        "plan_hash": approval.plan_hash,
                        "approval": approval.to_snapshot(),
                        "approved_task_version": approval.task_version,
                    }
                )
            candidate = await agent.run(
                AgentRequest(
                    task_id=task.task_id,
                    input=request.text,
                    idempotency_key=agent_run.agent_run_id,
                    context=context,
                )
            )
        except AgentNotFoundError:
            failure = FailureCode.ROUTE_NOT_FOUND
        except Exception:  # noqa: BLE001 - Agent 구현 예외를 실패 결과로 정규화한다.
            failure = FailureCode.AGENT_EXCEPTION
        else:
            if (
                not isinstance(candidate, AgentResult)
                or candidate.agent_id != routing.agent_id
                or not isinstance(candidate.outcome, AgentOutcome)
                or not isinstance(candidate.output, str)
            ):
                failure = FailureCode.AGENT_RESULT_MISMATCH
            elif candidate.outcome is AgentOutcome.FAILURE:
                failure = FailureCode.AGENT_FAILURE
            else:
                result = candidate

        outcome = AgentOutcome.SUCCESS if result is not None else AgentOutcome.FAILURE
        recorded_output = result.output if result is not None else failure.value
        agent_run = agent_run.complete(
            agent_id=routing.agent_id,
            outcome=outcome.value,
            output=recorded_output,
            at=self._clock(),
        )
        workflow = workflow.advance(Phase.VERIFYING, at=self._clock())
        if result is not None:
            task = task.transition(Status.COMPLETED, at=self._clock())
        errors = list(state.get("errors", []))
        if failure is not None:
            errors.append(failure.value)
        agent_run_snapshots[-1] = agent_run.to_snapshot()
        return {
            "task": task.to_snapshot(),
            "workflow": workflow.to_snapshot(),
            "agent_runs": agent_run_snapshots,
            "output": None if result is None else result.output,
            "errors": errors,
        }

    @staticmethod
    def _after_execution(state: _GraphState) -> str:
        task = Task.from_snapshot(state["task"])
        if task.status is Status.COMPLETED:
            return "terminal"
        workflow = WorkflowRun.from_snapshot(state["workflow"])
        return "retry" if workflow.budget.remaining > 0 else "escalate"

    def _escalate(self, state: _GraphState) -> _GraphState:
        task = Task.from_snapshot(state["task"])
        task = task.transition(Status.ESCALATED, at=self._clock())
        return {
            "task": task.to_snapshot(),
            "errors": [
                *state.get("errors", []),
                FailureCode.RETRY_EXHAUSTED.value,
            ],
        }

    async def start(
        self,
        request: OrchestratorInput,
        *,
        thread_id: str,
    ) -> OrchestrationResult:
        """새 Task를 시작해 terminal 결과 또는 interrupt를 반환한다."""

        _require_text(thread_id, field_name="thread_id")
        config = {"configurable": {"thread_id": thread_id}}
        async with self._thread_locks.setdefault(thread_id, asyncio.Lock()):
            checkpoint = await self._graph.aget_state(config)
            if checkpoint.values:
                raise OrchestrationStartError(
                    "이미 orchestration 실행이 존재하는 checkpoint thread입니다."
                )
            now = self._clock()
            task = Task.receive(task_id=request.task_id, input=request.text, at=now)
            task = task.transition(Status.RUNNING, at=self._clock())
            workflow = WorkflowRun.start(
                workflow_run_id=self._id_factory(),
                task=task,
                max_agent_runs=self._max_agent_runs,
                at=self._clock(),
            )
            state: _GraphState = {
                "request": request.to_snapshot(),
                "task": task.to_snapshot(),
                "workflow": workflow.to_snapshot(),
                "agent_runs": [],
                "output": None,
                "errors": [],
            }
            result = await self._graph.ainvoke(
                state,
                config=config,
            )
            return self._result_from_state(result)

    async def resume(
        self,
        *,
        thread_id: str,
        response: ApprovalResponse,
    ) -> OrchestrationResult:
        """대기 중인 같은 checkpoint thread에 사람의 결정을 전달한다."""

        _require_text(thread_id, field_name="thread_id")
        config = {"configurable": {"thread_id": thread_id}}
        async with self._thread_locks.setdefault(thread_id, asyncio.Lock()):
            snapshot = await self._graph.aget_state(config)
            if "approval" not in snapshot.next or not snapshot.interrupts:
                raise ApprovalResumeError(
                    "해당 checkpoint thread에 대기 중인 승인이 없습니다."
                )
            try:
                claim = await self._execution_coordinator.claim(
                    thread_id=thread_id,
                    agent_run_id=f"approval:{response.decision_id}",
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - seam 상세를 경계에서 제거한다.
                raise OrchestrationDependencyError(
                    "ExecutionCoordinator claim에 실패했습니다."
                ) from None
            if not isinstance(claim, ExecutionClaimResult):
                raise OrchestrationDependencyError(
                    "ExecutionCoordinator가 잘못된 claim 결과를 반환했습니다."
                )
            if claim.status is ExecutionClaimStatus.BUSY:
                raise ApprovalResumeError(
                    "다른 runner가 같은 Approval을 재개하고 있습니다."
                )
            if claim.status is not ExecutionClaimStatus.CLAIMED:
                raise OrchestrationDependencyError(
                    "ExecutionCoordinator가 잘못된 claim 결과를 반환했습니다."
                )
            claim_key = thread_id, f"approval:{response.decision_id}"
            cancelled = False
            try:
                try:
                    current_task = Task.from_snapshot(snapshot.values["task"])
                except (KeyError, TypeError, ValueError, LifecycleError):
                    raise OrchestrationStateError(
                        "Approval checkpoint state가 올바르지 않습니다."
                    ) from None
                try:
                    consumed = await self._approval_consumer.consume(
                        task=current_task,
                        response=response,
                        at=self._clock(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - seam 상세를 경계에서 제거한다.
                    raise OrchestrationDependencyError(
                        "ApprovalConsumer 호출에 실패했습니다."
                    ) from None
                if not isinstance(consumed, ApprovalConsumeResult):
                    raise OrchestrationDependencyError(
                        "ApprovalConsumer가 잘못된 결과를 반환했습니다."
                    )
                if consumed.status not in {
                    ApprovalConsumeStatus.APPLIED,
                    ApprovalConsumeStatus.ALREADY_APPLIED,
                }:
                    raise ApprovalResumeError(
                        "Approval decision을 소비할 수 없습니다: "
                        f"{consumed.status.value}"
                    )
                resume_payload = {
                    "response": response.to_snapshot(),
                    "successor": consumed.task.to_snapshot(),
                    "failure": (
                        None if consumed.failure is None else consumed.failure.value
                    ),
                }
                try:
                    result = await self._graph.ainvoke(
                        Command(resume=resume_payload),
                        config=config,
                    )
                except (KeyError, TypeError, ValueError, LifecycleError):
                    raise OrchestrationStateError(
                        "Approval checkpoint state가 올바르지 않습니다."
                    ) from None
                return self._result_from_state(result)
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                if cancelled:
                    await self._release_after_cancellation(*claim_key)
                else:
                    await self._release_execution_claim(*claim_key)

    async def get_result(self, *, thread_id: str) -> OrchestrationResult:
        """background runner가 checkpoint의 현재 공개 결과를 조회한다."""

        _require_text(thread_id, field_name="thread_id")
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            raise OrchestrationStateError(
                "해당 checkpoint thread에 orchestration state가 없습니다."
            )
        state = dict(snapshot.values)
        if snapshot.interrupts:
            state["__interrupt__"] = snapshot.interrupts
        return self._result_from_state(state)

    async def recover(self, *, thread_id: str) -> OrchestrationResult:
        """저장된 non-approval checkpoint의 persisted next node부터 재개한다."""

        _require_text(thread_id, field_name="thread_id")
        config = {"configurable": {"thread_id": thread_id}}
        async with self._thread_locks.setdefault(thread_id, asyncio.Lock()):
            snapshot = await self._graph.aget_state(config)
            if not snapshot.values:
                raise OrchestrationStateError(
                    "해당 checkpoint thread에 orchestration state가 없습니다."
                )
            current = dict(snapshot.values)
            if snapshot.interrupts:
                current["__interrupt__"] = snapshot.interrupts
            try:
                _input_from_snapshot(_snapshot_mapping(current, "request"))
                current_result = self._result_from_state(current)
            except (KeyError, TypeError, ValueError, LifecycleError):
                raise OrchestrationStateError(
                    "Checkpoint state가 올바르지 않습니다."
                ) from None
            if (
                current_result.task.status is Status.WAITING_APPROVAL
                or not snapshot.next
            ):
                return current_result
            claim_key: tuple[str, str] | None = None
            if "call_agent" in snapshot.next:
                if not current_result.agent_runs:
                    raise OrchestrationStateError(
                        "열린 AgentRun이 없는 call_agent checkpoint입니다."
                    )
                agent_run = current_result.agent_runs[-1]
                if agent_run.is_completed:
                    raise OrchestrationStateError(
                        "완료된 AgentRun은 call_agent에서 복구할 수 없습니다."
                    )
                try:
                    claim = await self._execution_coordinator.claim(
                        thread_id=thread_id,
                        agent_run_id=agent_run.agent_run_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - seam 상세를 경계에서 제거한다.
                    raise OrchestrationDependencyError(
                        "ExecutionCoordinator claim에 실패했습니다."
                    ) from None
                if not isinstance(claim, ExecutionClaimResult):
                    raise OrchestrationDependencyError(
                        "ExecutionCoordinator가 잘못된 claim 결과를 반환했습니다."
                    )
                if claim.status is ExecutionClaimStatus.BUSY:
                    raise OrchestrationRecoveryError(
                        "다른 runner가 같은 AgentRun을 복구하고 있습니다."
                    )
                if claim.status is not ExecutionClaimStatus.CLAIMED:
                    raise OrchestrationDependencyError(
                        "ExecutionCoordinator가 잘못된 claim 결과를 반환했습니다."
                    )
                claim_key = thread_id, agent_run.agent_run_id
            try:
                result = await self._graph.ainvoke(None, config=config)
            except asyncio.CancelledError:
                if claim_key is not None:
                    await self._release_after_cancellation(*claim_key)
                raise
            except Exception:  # noqa: BLE001 - checkpoint parse 오류를 정규화한다.
                if claim_key is not None:
                    await self._release_execution_claim(*claim_key)
                raise OrchestrationStateError(
                    "Checkpoint state를 복구할 수 없습니다."
                ) from None
            if claim_key is not None:
                await self._release_execution_claim(*claim_key)
            return self._result_from_state(result)

    async def _release_execution_claim(self, thread_id: str, agent_run_id: str) -> None:
        try:
            await self._execution_coordinator.release(
                thread_id=thread_id,
                agent_run_id=agent_run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - seam 상세를 경계에서 제거한다.
            raise OrchestrationDependencyError(
                "ExecutionCoordinator release에 실패했습니다."
            ) from None

    async def _release_after_cancellation(
        self, thread_id: str, agent_run_id: str
    ) -> None:
        try:
            await asyncio.shield(
                self._execution_coordinator.release(
                    thread_id=thread_id,
                    agent_run_id=agent_run_id,
                )
            )
        except BaseException:  # noqa: BLE001 - 원래 cancellation을 보존한다.
            return

    @staticmethod
    def _result_from_state(state: Mapping[str, object]) -> OrchestrationResult:
        try:
            workflow = WorkflowRun.from_snapshot(_snapshot_mapping(state, "workflow"))
            agent_run_snapshots = _snapshot_list(state, "agent_runs")
            agent_runs = tuple(
                AgentRun.from_snapshot(
                    _snapshot_mapping({"agent_run": snapshot}, "agent_run"),
                    workflow=workflow,
                )
                for snapshot in agent_run_snapshots
            )
            output = state["output"]
            if output is not None and not isinstance(output, str):
                raise TypeError("output은 문자열 또는 None이어야 합니다.")
            error_values = _snapshot_list(state, "errors")
            if not all(isinstance(code, str) for code in error_values):
                raise TypeError("errors 항목은 문자열이어야 합니다.")
            return OrchestrationResult(
                task=Task.from_snapshot(_snapshot_mapping(state, "task")),
                workflow=workflow,
                routing=(
                    None
                    if state.get("routing") is None
                    else RoutingDecision.from_snapshot(
                        _snapshot_mapping(state, "routing")
                    )
                ),
                governance=(
                    None
                    if state.get("governance") is None
                    else GovernanceDecision.from_snapshot(
                        _snapshot_mapping(state, "governance")
                    )
                ),
                agent_runs=agent_runs,
                output=output,
                interrupt=OrchestratorService._interrupt_from_state(state),
                errors=tuple(FailureCode(code) for code in error_values),
            )
        except OrchestrationStateError:
            raise
        except (KeyError, TypeError, ValueError, LifecycleError):
            raise OrchestrationStateError(
                "Checkpoint state가 올바르지 않습니다."
            ) from None

    @staticmethod
    def _interrupt_from_state(state: Mapping[str, object]) -> ApprovalRequest | None:
        interrupts = state.get("__interrupt__", ())
        if not interrupts:
            return None
        return ApprovalRequest.from_snapshot(interrupts[0].value)


__all__ = [
    "ActionKind",
    "ActionPlan",
    "AlertInput",
    "ApprovalConsumeResult",
    "ApprovalConsumeStatus",
    "ApprovalConsumer",
    "ApprovalRequest",
    "ApprovalResponse",
    "ApprovalResumeError",
    "ChatModelRequestClassifier",
    "ClassificationError",
    "ExecutionClaimResult",
    "ExecutionClaimStatus",
    "ExecutionCoordinator",
    "FailureCode",
    "FakeApprovalConsumer",
    "FakeExecutionCoordinator",
    "FakeGovernance",
    "FakeRequestClassifier",
    "Governance",
    "GovernanceDecision",
    "OrchestrationDependencyError",
    "OrchestrationRecoveryError",
    "OrchestrationResult",
    "OrchestrationStartError",
    "OrchestrationStateError",
    "OrchestratorInput",
    "OrchestratorService",
    "RequestClassifier",
    "RequestKind",
    "RoutingDecision",
    "TicketInput",
    "UserTaskInput",
]
