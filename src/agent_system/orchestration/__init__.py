"""Framework에 독립적인 orchestration 생명주기 모델."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class LifecycleError(Exception):
    """생명주기 불변 조건 위반의 기본 오류다."""


class InvalidLifecycleValueError(LifecycleError, ValueError):
    """생명주기 snapshot의 값이 유효하지 않을 때 발생한다."""


class InvalidStatusTransitionError(LifecycleError):
    """허용되지 않은 Task 상태 전이를 요청했을 때 발생한다."""


class InvalidPhaseTransitionError(LifecycleError):
    """허용되지 않은 WorkflowRun phase 전이를 요청했을 때 발생한다."""


class ExecutionBudgetExhaustedError(LifecycleError):
    """WorkflowRun이 허용된 Agent 호출 횟수를 모두 소비했을 때 발생한다."""


class AgentRunError(LifecycleError):
    """AgentRun 완료 불변 조건 위반의 기본 오류다."""


class AgentRunAgentMismatchError(AgentRunError):
    """호출한 Agent와 완료 결과의 Agent가 다를 때 발생한다."""


class AgentRunAlreadyCompletedError(AgentRunError):
    """완료된 AgentRun을 다시 완료하려 할 때 발생한다."""


class PlanRequiredError(LifecycleError):
    """승인 대기 전이에 필요한 plan이 없을 때 발생한다."""


class PlanUpdateNotAllowedError(LifecycleError):
    """현재 Task 상태에서 plan을 바꿀 수 없을 때 발생한다."""


class ApprovalError(LifecycleError):
    """Approval이 현재 Task snapshot과 호환되지 않을 때 발생한다."""


class ApprovalRequiredError(ApprovalError):
    """승인 없이 승인 대기 Task를 재개하려 할 때 발생한다."""


class ApprovalNotAllowedError(ApprovalError):
    """승인 대기 상태가 아닌 Task에 Approval을 만들 때 발생한다."""


class ApprovalTaskMismatchError(ApprovalError):
    """Approval의 Task 식별자가 현재 Task와 다를 때 발생한다."""


class PlanChangedError(ApprovalError):
    """Approval 이후 Task plan이 변경되었을 때 발생한다."""


class StaleApprovalError(ApprovalError):
    """Approval의 Task version이 현재 snapshot보다 오래되었을 때 발생한다."""


def _require_aware(value: datetime, *, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise InvalidLifecycleValueError(f"{field_name}은 datetime이어야 합니다.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidLifecycleValueError(
            f"{field_name}에는 timezone-aware datetime이 필요합니다."
        )


def _require_not_before(
    value: datetime, earliest: datetime, *, field_name: str
) -> None:
    _require_aware(value, field_name=field_name)
    if value < earliest:
        raise InvalidLifecycleValueError(
            f"{field_name}은 이전 snapshot 시각보다 빠를 수 없습니다."
        )


class Status(StrEnum):
    """Task가 외부에 노출하는 생명주기 상태다."""

    RECEIVED = "RECEIVED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ESCALATED = "ESCALATED"


class Phase(StrEnum):
    """WorkflowRun이 수행 중인 orchestration 단계를 나타낸다."""

    CLASSIFYING = "CLASSIFYING"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    GOVERNING = "GOVERNING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"


_STATUS_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.RECEIVED: frozenset({Status.RUNNING, Status.CANCELLED}),
    Status.RUNNING: frozenset(
        {
            Status.WAITING_APPROVAL,
            Status.COMPLETED,
            Status.REJECTED,
            Status.FAILED,
            Status.CANCELLED,
            Status.ESCALATED,
        }
    ),
    Status.WAITING_APPROVAL: frozenset(
        {Status.RUNNING, Status.REJECTED, Status.CANCELLED, Status.ESCALATED}
    ),
    Status.COMPLETED: frozenset(),
    Status.REJECTED: frozenset(),
    Status.FAILED: frozenset(),
    Status.CANCELLED: frozenset(),
    Status.ESCALATED: frozenset(),
}

_PHASE_TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.CLASSIFYING: frozenset({Phase.ANALYZING}),
    Phase.ANALYZING: frozenset({Phase.PLANNING}),
    Phase.PLANNING: frozenset({Phase.GOVERNING}),
    Phase.GOVERNING: frozenset({Phase.EXECUTING}),
    Phase.EXECUTING: frozenset({Phase.VERIFYING}),
    Phase.VERIFYING: frozenset({Phase.EXECUTING}),
}


@dataclass(frozen=True, slots=True)
class Approval:
    """특정 Task version과 plan에 부여된 사람의 승인 증명이다."""

    task_id: str
    task_version: int
    plan_hash: str
    approved_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise InvalidLifecycleValueError(
                "Approval task_id는 비어 있을 수 없습니다."
            )
        if isinstance(self.task_version, bool) or not isinstance(
            self.task_version, int
        ):
            raise InvalidLifecycleValueError("Approval task_version은 양의 정수입니다.")
        if self.task_version <= 0:
            raise InvalidLifecycleValueError("Approval task_version은 양의 정수입니다.")
        if not isinstance(self.plan_hash, str) or not self.plan_hash.strip():
            raise InvalidLifecycleValueError(
                "Approval plan_hash는 비어 있을 수 없습니다."
            )
        _require_aware(self.approved_at, field_name="approved_at")

    @classmethod
    def grant_for(cls, task: Task, *, at: datetime) -> Approval:
        """현재 승인 대기 snapshot에 결합된 Approval을 만든다."""

        if task.status is not Status.WAITING_APPROVAL:
            raise ApprovalNotAllowedError(
                "WAITING_APPROVAL 상태의 Task만 승인할 수 있습니다."
            )
        if task.plan_hash is None:
            raise PlanRequiredError("plan이 없는 Task에는 승인할 수 없습니다.")
        _require_not_before(at, task.updated_at, field_name="approved_at")
        return cls(
            task_id=task.task_id,
            task_version=task.version,
            plan_hash=task.plan_hash,
            approved_at=at,
        )


@dataclass(frozen=True, slots=True)
class Task:
    """요청과 현재 상태를 보존하는 versioned snapshot이다."""

    task_id: str
    input: str
    status: Status
    version: int
    plan_hash: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise InvalidLifecycleValueError("task_id는 비어 있을 수 없습니다.")
        if not isinstance(self.input, str) or not self.input.strip():
            raise InvalidLifecycleValueError("input은 비어 있을 수 없습니다.")
        if not isinstance(self.status, Status):
            raise InvalidLifecycleValueError("status는 Status 값이어야 합니다.")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise InvalidLifecycleValueError("version은 양의 정수여야 합니다.")
        if self.version <= 0:
            raise InvalidLifecycleValueError("version은 양의 정수여야 합니다.")
        if self.plan_hash is not None and (
            not isinstance(self.plan_hash, str) or not self.plan_hash.strip()
        ):
            raise InvalidLifecycleValueError("plan_hash는 비어 있을 수 없습니다.")
        if self.status is Status.WAITING_APPROVAL and self.plan_hash is None:
            raise InvalidLifecycleValueError(
                "WAITING_APPROVAL 상태에는 plan_hash가 필요합니다."
            )
        _require_aware(self.created_at, field_name="created_at")
        _require_aware(self.updated_at, field_name="updated_at")
        if self.updated_at < self.created_at:
            raise InvalidLifecycleValueError(
                "updated_at은 created_at보다 빠를 수 없습니다."
            )

    @classmethod
    def receive(cls, *, task_id: str, input: str, at: datetime) -> Task:
        """새 요청의 최초 snapshot을 만든다."""

        return cls(
            task_id=task_id,
            input=input,
            status=Status.RECEIVED,
            version=1,
            plan_hash=None,
            created_at=at,
            updated_at=at,
        )

    def transition(
        self,
        target: Status,
        *,
        at: datetime,
        approval: Approval | None = None,
    ) -> Task:
        """허용된 상태로 이동한 새 version의 snapshot을 반환한다."""

        _require_not_before(at, self.updated_at, field_name="at")
        if target not in _STATUS_TRANSITIONS[self.status]:
            raise InvalidStatusTransitionError(
                f"허용되지 않은 Task 상태 전이입니다: {self.status} -> {target}"
            )
        if target is Status.WAITING_APPROVAL and self.plan_hash is None:
            raise PlanRequiredError("승인 대기 전에는 plan이 필요합니다.")
        if self.status is Status.WAITING_APPROVAL and target is Status.RUNNING:
            if approval is None:
                raise ApprovalRequiredError(
                    "승인 대기 Task를 재개하려면 Approval이 필요합니다."
                )
            if approval.task_id != self.task_id:
                raise ApprovalTaskMismatchError(
                    "Approval의 task_id가 현재 Task와 다릅니다."
                )
            if approval.plan_hash != self.plan_hash:
                raise PlanChangedError("Approval 이후 plan이 변경되었습니다.")
            if approval.task_version != self.version:
                raise StaleApprovalError("Approval의 Task version이 오래되었습니다.")
            _require_not_before(at, approval.approved_at, field_name="at")
        return replace(
            self,
            status=target,
            version=self.version + 1,
            updated_at=at,
        )

    def update_plan(self, plan_hash: str, *, at: datetime) -> Task:
        """활성 Task의 plan hash를 새 version에 반영한다."""

        _require_not_before(at, self.updated_at, field_name="at")
        if self.status not in {Status.RUNNING, Status.WAITING_APPROVAL}:
            raise PlanUpdateNotAllowedError(
                f"현재 상태에서는 plan을 바꿀 수 없습니다: {self.status}"
            )
        return replace(
            self,
            plan_hash=plan_hash,
            version=self.version + 1,
            updated_at=at,
        )

    def cancel(self, *, at: datetime) -> Task:
        """활성 Task를 명시적인 취소 결과로 전이한다."""

        return self.transition(Status.CANCELLED, at=at)

    def to_snapshot(self) -> dict[str, object]:
        """영속화 경계에 전달할 JSON 호환 snapshot을 반환한다."""

        return {
            "task_id": self.task_id,
            "input": self.input,
            "status": self.status.value,
            "version": self.version,
            "plan_hash": self.plan_hash,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> Task:
        """영속화 경계의 JSON 호환 값에서 Task를 복원한다."""

        try:
            task_id = snapshot["task_id"]
            input_value = snapshot["input"]
            status_value = snapshot["status"]
            version = snapshot["version"]
            plan_hash = snapshot["plan_hash"]
            created_at_value = snapshot["created_at"]
            updated_at_value = snapshot["updated_at"]
        except KeyError as error:
            raise InvalidLifecycleValueError(
                f"Task snapshot 필드가 없습니다: {error.args[0]}"
            ) from error
        if not isinstance(task_id, str) or not isinstance(input_value, str):
            raise InvalidLifecycleValueError(
                "Task snapshot의 task_id와 input은 문자열이어야 합니다."
            )
        if not isinstance(status_value, str):
            raise InvalidLifecycleValueError(
                "Task snapshot의 status는 문자열이어야 합니다."
            )
        if plan_hash is not None and not isinstance(plan_hash, str):
            raise InvalidLifecycleValueError(
                "Task snapshot의 plan_hash는 문자열 또는 null이어야 합니다."
            )
        if not isinstance(created_at_value, str) or not isinstance(
            updated_at_value, str
        ):
            raise InvalidLifecycleValueError(
                "Task snapshot의 시각은 ISO 8601 문자열이어야 합니다."
            )
        try:
            status = Status(status_value)
            created_at = datetime.fromisoformat(created_at_value)
            updated_at = datetime.fromisoformat(updated_at_value)
        except ValueError as error:
            raise InvalidLifecycleValueError(
                "Task snapshot의 status 또는 시각 형식이 올바르지 않습니다."
            ) from error
        return cls(
            task_id=task_id,
            input=input_value,
            status=status,
            version=version,
            plan_hash=plan_hash,
            created_at=created_at,
            updated_at=updated_at,
        )


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """한 WorkflowRun에서 허용하는 Agent 호출 횟수다."""

    limit: int
    consumed: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise InvalidLifecycleValueError("budget limit은 양의 정수여야 합니다.")
        if self.limit <= 0:
            raise InvalidLifecycleValueError("budget limit은 양의 정수여야 합니다.")
        if isinstance(self.consumed, bool) or not isinstance(self.consumed, int):
            raise InvalidLifecycleValueError("budget consumed는 정수여야 합니다.")
        if not 0 <= self.consumed <= self.limit:
            raise InvalidLifecycleValueError(
                "budget consumed는 0 이상 limit 이하여야 합니다."
            )

    @property
    def remaining(self) -> int:
        """앞으로 허용되는 Agent 호출 횟수다."""

        return self.limit - self.consumed

    def consume(self) -> ExecutionBudget:
        """한 번의 Agent 호출 권한을 소비한다."""

        if self.remaining == 0:
            raise ExecutionBudgetExhaustedError(
                f"Agent 실행 budget을 모두 소비했습니다: {self.limit}"
            )
        return replace(self, consumed=self.consumed + 1)


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """Task의 orchestration phase와 호출 budget을 보존하는 snapshot이다."""

    workflow_run_id: str
    task_id: str
    task_version: int
    phase: Phase
    budget: ExecutionBudget
    started_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.workflow_run_id, str)
            or not self.workflow_run_id.strip()
        ):
            raise InvalidLifecycleValueError("workflow_run_id는 비어 있을 수 없습니다.")
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise InvalidLifecycleValueError("task_id는 비어 있을 수 없습니다.")
        if isinstance(self.task_version, bool) or not isinstance(
            self.task_version, int
        ):
            raise InvalidLifecycleValueError("task_version은 양의 정수여야 합니다.")
        if self.task_version <= 0:
            raise InvalidLifecycleValueError("task_version은 양의 정수여야 합니다.")
        if not isinstance(self.phase, Phase):
            raise InvalidLifecycleValueError("phase는 Phase 값이어야 합니다.")
        if not isinstance(self.budget, ExecutionBudget):
            raise InvalidLifecycleValueError("budget은 ExecutionBudget이어야 합니다.")
        _require_aware(self.started_at, field_name="started_at")
        _require_aware(self.updated_at, field_name="updated_at")
        if self.updated_at < self.started_at:
            raise InvalidLifecycleValueError(
                "updated_at은 started_at보다 빠를 수 없습니다."
            )

    @classmethod
    def start(
        cls,
        *,
        workflow_run_id: str,
        task: Task,
        max_agent_runs: int,
        at: datetime,
    ) -> WorkflowRun:
        """Task에 결합된 WorkflowRun을 최초 phase에서 시작한다."""

        _require_not_before(at, task.updated_at, field_name="at")
        return cls(
            workflow_run_id=workflow_run_id,
            task_id=task.task_id,
            task_version=task.version,
            phase=Phase.CLASSIFYING,
            budget=ExecutionBudget(limit=max_agent_runs),
            started_at=at,
            updated_at=at,
        )

    def advance(self, target: Phase, *, at: datetime) -> WorkflowRun:
        """허용된 다음 phase의 새 snapshot을 반환한다."""

        _require_not_before(at, self.updated_at, field_name="at")
        if target not in _PHASE_TRANSITIONS[self.phase]:
            raise InvalidPhaseTransitionError(
                f"허용되지 않은 WorkflowRun phase 전이입니다: {self.phase} -> {target}"
            )
        return replace(self, phase=target, updated_at=at)

    def consume_budget(self, *, at: datetime) -> WorkflowRun:
        """Agent 호출 전에 budget 한 회를 소비한 snapshot을 반환한다."""

        _require_not_before(at, self.updated_at, field_name="at")
        return replace(self, budget=self.budget.consume(), updated_at=at)


@dataclass(frozen=True, slots=True)
class AgentRun:
    """한 번의 Agent 호출과 그 완료 결과를 연결하는 immutable 기록이다."""

    agent_run_id: str
    workflow_run_id: str
    task_id: str
    task_version: int
    agent_id: str
    phase: Phase
    started_at: datetime
    outcome: str | None = None
    output: str | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        identifiers = {
            "agent_run_id": self.agent_run_id,
            "workflow_run_id": self.workflow_run_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
        }
        for field_name, value in identifiers.items():
            if not isinstance(value, str) or not value.strip():
                raise InvalidLifecycleValueError(
                    f"{field_name}는 비어 있을 수 없습니다."
                )
        if isinstance(self.task_version, bool) or not isinstance(
            self.task_version, int
        ):
            raise InvalidLifecycleValueError("task_version은 양의 정수여야 합니다.")
        if self.task_version <= 0:
            raise InvalidLifecycleValueError("task_version은 양의 정수여야 합니다.")
        if not isinstance(self.phase, Phase):
            raise InvalidLifecycleValueError("phase는 Phase 값이어야 합니다.")
        _require_aware(self.started_at, field_name="started_at")
        completion_values = (self.outcome, self.output, self.completed_at)
        if any(value is None for value in completion_values) and any(
            value is not None for value in completion_values
        ):
            raise InvalidLifecycleValueError(
                "AgentRun 완료 결과와 완료 시각은 함께 기록해야 합니다."
            )
        if self.outcome is not None and (
            not isinstance(self.outcome, str) or not self.outcome.strip()
        ):
            raise InvalidLifecycleValueError("outcome은 비어 있을 수 없습니다.")
        if self.output is not None and not isinstance(self.output, str):
            raise InvalidLifecycleValueError("output은 문자열이어야 합니다.")
        if self.completed_at is not None:
            _require_not_before(
                self.completed_at,
                self.started_at,
                field_name="completed_at",
            )

    @classmethod
    def start(
        cls,
        *,
        agent_run_id: str,
        workflow: WorkflowRun,
        agent_id: str,
        at: datetime,
    ) -> AgentRun:
        """현재 workflow phase에 결합된 열린 AgentRun을 만든다."""

        _require_not_before(at, workflow.updated_at, field_name="at")
        return cls(
            agent_run_id=agent_run_id,
            workflow_run_id=workflow.workflow_run_id,
            task_id=workflow.task_id,
            task_version=workflow.task_version,
            agent_id=agent_id,
            phase=workflow.phase,
            started_at=at,
        )

    @property
    def is_completed(self) -> bool:
        """AgentRun에 완료 결과가 기록되었는지 반환한다."""

        return self.completed_at is not None

    def complete(
        self,
        *,
        agent_id: str,
        outcome: str,
        output: str,
        at: datetime,
    ) -> AgentRun:
        """호출한 Agent의 결과로 열린 AgentRun을 한 번만 완료한다."""

        if self.is_completed:
            raise AgentRunAlreadyCompletedError("AgentRun은 이미 완료되었습니다.")
        if agent_id != self.agent_id:
            raise AgentRunAgentMismatchError(
                "AgentRun의 agent_id와 결과의 agent_id가 다릅니다."
            )
        _require_not_before(at, self.started_at, field_name="at")
        return replace(
            self,
            outcome=outcome,
            output=output,
            completed_at=at,
        )


__all__ = [
    "AgentRun",
    "AgentRunAgentMismatchError",
    "AgentRunAlreadyCompletedError",
    "AgentRunError",
    "Approval",
    "ApprovalError",
    "ApprovalNotAllowedError",
    "ApprovalRequiredError",
    "ApprovalTaskMismatchError",
    "ExecutionBudget",
    "ExecutionBudgetExhaustedError",
    "InvalidLifecycleValueError",
    "InvalidPhaseTransitionError",
    "InvalidStatusTransitionError",
    "LifecycleError",
    "Phase",
    "PlanChangedError",
    "PlanRequiredError",
    "PlanUpdateNotAllowedError",
    "StaleApprovalError",
    "Status",
    "Task",
    "WorkflowRun",
]
