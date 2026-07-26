"""Framework에 독립적인 orchestration 생명주기 모델."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from ._support import (
    AgentRunAgentMismatchError,
    AgentRunAlreadyCompletedError,
    AgentRunError,
    ApprovalError,
    ApprovalNotAllowedError,
    ApprovalRequiredError,
    ApprovalTaskMismatchError,
    ExecutionBudgetExhaustedError,
    InvalidLifecycleValueError,
    InvalidPhaseTransitionError,
    InvalidStatusTransitionError,
    LifecycleError,
    PlanChangedError,
    PlanRequiredError,
    PlanUpdateNotAllowedError,
    StaleApprovalError,
)
from ._support import (
    require_aware as _require_aware,
)
from ._support import (
    require_not_before as _require_not_before,
)
from ._support import (
    snapshot_datetime as _snapshot_datetime,
)
from ._support import (
    snapshot_enum as _snapshot_enum,
)
from ._support import (
    snapshot_integer as _snapshot_integer,
)
from ._support import (
    snapshot_mapping as _snapshot_mapping,
)
from ._support import (
    snapshot_optional_string as _snapshot_optional_string,
)
from ._support import (
    snapshot_string as _snapshot_string,
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
    Phase.VERIFYING: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Approval:
    """특정 Task version과 plan에 부여된 사람의 승인 증명이다."""

    task_id: str
    task_version: int
    plan_hash: str
    approved_at: datetime

    @property
    def binding(self) -> tuple[str, int, str]:
        """멱등 처리에 사용하는 안정적인 Task/version/plan 결합을 반환한다."""

        return self.task_id, self.task_version, self.plan_hash

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

    def to_snapshot(self) -> dict[str, object]:
        """Approval을 JSON 호환 snapshot으로 반환한다."""

        return {
            "task_id": self.task_id,
            "task_version": self.task_version,
            "plan_hash": self.plan_hash,
            "approved_at": self.approved_at.isoformat(),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> Approval:
        """JSON 호환 snapshot에서 Approval을 복원한다."""

        return cls(
            task_id=_snapshot_string(snapshot, "task_id"),
            task_version=_snapshot_integer(snapshot, "task_version"),
            plan_hash=_snapshot_string(snapshot, "plan_hash"),
            approved_at=_snapshot_datetime(snapshot, "approved_at"),
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
        if self.status is Status.RECEIVED:
            if self.version != 1 or self.plan_hash is not None:
                raise InvalidLifecycleValueError(
                    "RECEIVED Task는 version 1이며 plan이 없어야 합니다."
                )
        elif self.version < 2:
            raise InvalidLifecycleValueError(
                "RECEIVED 이후 status에는 version 2 이상이 필요합니다."
            )
        if self.status is Status.WAITING_APPROVAL and self.version < 4:
            raise InvalidLifecycleValueError(
                "WAITING_APPROVAL status에는 version 4 이상이 필요합니다."
            )
        if (
            self.status
            in {
                Status.COMPLETED,
                Status.REJECTED,
                Status.FAILED,
                Status.ESCALATED,
            }
            and self.version < 3
        ):
            raise InvalidLifecycleValueError(
                "이 terminal status에는 version 3 이상이 필요합니다."
            )
        if self.plan_hash is not None:
            minimum_plan_version = 3 if self.status is Status.RUNNING else 4
            if self.version < minimum_plan_version:
                raise InvalidLifecycleValueError(
                    "현재 status에서 plan을 가진 Task version이 도달 불가능합니다."
                )
        _require_aware(self.created_at, field_name="created_at")
        _require_not_before(
            self.updated_at,
            self.created_at,
            field_name="updated_at",
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

        return cls(
            task_id=_snapshot_string(snapshot, "task_id"),
            input=_snapshot_string(snapshot, "input"),
            status=_snapshot_enum(snapshot, "status", Status),
            version=_snapshot_integer(snapshot, "version"),
            plan_hash=_snapshot_optional_string(snapshot, "plan_hash"),
            created_at=_snapshot_datetime(snapshot, "created_at"),
            updated_at=_snapshot_datetime(snapshot, "updated_at"),
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

    def to_snapshot(self) -> dict[str, int]:
        """Budget을 JSON 호환 snapshot으로 반환한다."""

        return {"limit": self.limit, "consumed": self.consumed}

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> ExecutionBudget:
        """JSON 호환 snapshot에서 Budget을 복원한다."""

        return cls(
            limit=_snapshot_integer(snapshot, "limit"),
            consumed=_snapshot_integer(snapshot, "consumed"),
        )


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
        _require_not_before(
            self.updated_at,
            self.started_at,
            field_name="updated_at",
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

    def begin_agent_run(
        self,
        *,
        agent_run_id: str,
        agent_id: str,
        at: datetime,
        retry: bool = False,
    ) -> tuple[WorkflowRun, AgentRun]:
        """Budget을 소비하며 AgentRun과 갱신된 WorkflowRun을 함께 만든다."""

        _require_not_before(at, self.updated_at, field_name="at")
        if retry and self.phase is not Phase.VERIFYING:
            raise InvalidPhaseTransitionError(
                "Agent 재실행은 VERIFYING phase에서만 시작할 수 있습니다."
            )
        budget = self.budget.consume()
        phase = Phase.EXECUTING if retry else self.phase
        workflow = replace(
            self,
            phase=phase,
            budget=budget,
            updated_at=at,
        )
        agent_run = AgentRun(
            agent_run_id=agent_run_id,
            workflow_run_id=workflow.workflow_run_id,
            task_id=workflow.task_id,
            task_version=workflow.task_version,
            agent_id=agent_id,
            phase=workflow.phase,
            budget_sequence=workflow.budget.consumed,
            started_at=at,
        )
        return workflow, agent_run

    def to_snapshot(self) -> dict[str, object]:
        """WorkflowRun을 JSON 호환 snapshot으로 반환한다."""

        return {
            "workflow_run_id": self.workflow_run_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "phase": self.phase.value,
            "budget": self.budget.to_snapshot(),
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> WorkflowRun:
        """JSON 호환 snapshot에서 WorkflowRun을 복원한다."""

        return cls(
            workflow_run_id=_snapshot_string(snapshot, "workflow_run_id"),
            task_id=_snapshot_string(snapshot, "task_id"),
            task_version=_snapshot_integer(snapshot, "task_version"),
            phase=_snapshot_enum(snapshot, "phase", Phase),
            budget=ExecutionBudget.from_snapshot(_snapshot_mapping(snapshot, "budget")),
            started_at=_snapshot_datetime(snapshot, "started_at"),
            updated_at=_snapshot_datetime(snapshot, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class AgentRun:
    """한 번의 Agent 호출과 그 완료 결과를 연결하는 immutable 기록이다."""

    agent_run_id: str
    workflow_run_id: str
    task_id: str
    task_version: int
    agent_id: str
    phase: Phase
    budget_sequence: int
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
        if isinstance(self.budget_sequence, bool) or not isinstance(
            self.budget_sequence, int
        ):
            raise InvalidLifecycleValueError("budget_sequence는 양의 정수여야 합니다.")
        if self.budget_sequence <= 0:
            raise InvalidLifecycleValueError("budget_sequence는 양의 정수여야 합니다.")
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

    def to_snapshot(self) -> dict[str, object]:
        """AgentRun을 JSON 호환 snapshot으로 반환한다."""

        return {
            "agent_run_id": self.agent_run_id,
            "workflow_run_id": self.workflow_run_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "agent_id": self.agent_id,
            "phase": self.phase.value,
            "budget_sequence": self.budget_sequence,
            "started_at": self.started_at.isoformat(),
            "outcome": self.outcome,
            "output": self.output,
            "completed_at": (
                None if self.completed_at is None else self.completed_at.isoformat()
            ),
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> AgentRun:
        """JSON 호환 snapshot에서 AgentRun을 복원한다."""

        return cls(
            agent_run_id=_snapshot_string(snapshot, "agent_run_id"),
            workflow_run_id=_snapshot_string(snapshot, "workflow_run_id"),
            task_id=_snapshot_string(snapshot, "task_id"),
            task_version=_snapshot_integer(snapshot, "task_version"),
            agent_id=_snapshot_string(snapshot, "agent_id"),
            phase=_snapshot_enum(snapshot, "phase", Phase),
            budget_sequence=_snapshot_integer(snapshot, "budget_sequence"),
            started_at=_snapshot_datetime(snapshot, "started_at"),
            outcome=_snapshot_optional_string(snapshot, "outcome"),
            output=_snapshot_optional_string(snapshot, "output"),
            completed_at=_snapshot_datetime(
                snapshot,
                "completed_at",
                optional=True,
            ),
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
