"""Orchestration 생명주기 모델의 공개 동작을 검증한다."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agent_system.orchestration import (
    AgentRun,
    AgentRunAgentMismatchError,
    AgentRunAlreadyCompletedError,
    AgentRunOwnershipError,
    Approval,
    ApprovalNotAllowedError,
    ApprovalRequiredError,
    ApprovalTaskMismatchError,
    ExecutionBudget,
    ExecutionBudgetExhaustedError,
    InvalidLifecycleValueError,
    InvalidPhaseTransitionError,
    InvalidStatusTransitionError,
    Phase,
    PlanChangedError,
    PlanRequiredError,
    PlanUpdateNotAllowedError,
    StaleApprovalError,
    Status,
    Task,
    WorkflowRun,
)

NOW = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=1)


class TaskLifecycleTests(unittest.TestCase):
    """Task 변경이 versioned immutable snapshot을 만든다."""

    def test_receives_a_new_task_as_versioned_snapshot(self) -> None:
        task = Task.receive(
            task_id="task-123",
            input="장애 원인을 분석해 주세요.",
            at=NOW,
        )

        self.assertEqual(task.task_id, "task-123")
        self.assertEqual(task.input, "장애 원인을 분석해 주세요.")
        self.assertEqual(task.status, Status.RECEIVED)
        self.assertEqual(task.version, 1)
        self.assertEqual(task.created_at, NOW)
        self.assertEqual(task.updated_at, NOW)
        self.assertIsNone(task.plan_hash)

    def test_rejects_empty_identity_input_and_naive_timestamp(self) -> None:
        invalid_inputs = (
            {"task_id": "", "input": "요청", "at": NOW},
            {"task_id": "task-123", "input": "", "at": NOW},
            {
                "task_id": "task-123",
                "input": "요청",
                "at": NOW.replace(tzinfo=None),
            },
            {
                "task_id": "task-123",
                "input": "요청",
                "at": "2026-07-26T09:00:00+00:00",
            },
        )

        for values in invalid_inputs:
            with (
                self.subTest(values=values),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                Task.receive(**values)

    def test_orders_timezone_aware_datetimes_by_absolute_utc_instant(self) -> None:
        new_york = ZoneInfo("America/New_York")
        before_fold = datetime(2024, 11, 3, 1, 30, tzinfo=new_york, fold=0)
        later_fold = datetime(2024, 11, 3, 1, 15, tzinfo=new_york, fold=1)
        absolute_earlier = datetime(2024, 11, 3, 1, 45, tzinfo=new_york, fold=0)
        task = Task.receive(task_id="task-dst", input="요청", at=before_fold)

        running = task.transition(Status.RUNNING, at=later_fold)

        self.assertEqual(running.updated_at, later_fold)
        with self.assertRaises(InvalidLifecycleValueError):
            running.transition(Status.COMPLETED, at=absolute_earlier)

    def test_all_non_approval_status_transitions_follow_the_documented_matrix(
        self,
    ) -> None:
        allowed = {
            Status.RECEIVED: {Status.RUNNING, Status.CANCELLED},
            Status.RUNNING: {
                Status.WAITING_APPROVAL,
                Status.COMPLETED,
                Status.REJECTED,
                Status.FAILED,
                Status.CANCELLED,
                Status.ESCALATED,
            },
            Status.WAITING_APPROVAL: {
                Status.REJECTED,
                Status.CANCELLED,
                Status.ESCALATED,
            },
            Status.COMPLETED: set(),
            Status.REJECTED: set(),
            Status.FAILED: set(),
            Status.CANCELLED: set(),
            Status.ESCALATED: set(),
        }

        for current, permitted in allowed.items():
            task = self._task_at(current)
            for target in Status:
                if current is Status.WAITING_APPROVAL and target is Status.RUNNING:
                    continue
                if target in permitted:
                    with self.subTest(current=current, target=target):
                        transitioned = task.transition(target, at=LATER)
                        self.assertEqual(transitioned.status, target)
                        self.assertEqual(transitioned.version, task.version + 1)
                        self.assertEqual(transitioned.updated_at, LATER)
                        self.assertEqual(task.status, current)
                else:
                    with (
                        self.subTest(current=current, target=target),
                        self.assertRaises(InvalidStatusTransitionError),
                    ):
                        task.transition(target, at=LATER)

    def test_requires_a_plan_before_waiting_for_approval(self) -> None:
        task = Task.receive(
            task_id="task-123",
            input="변경 요청",
            at=NOW,
        ).transition(Status.RUNNING, at=LATER)

        with self.assertRaises(PlanRequiredError):
            task.transition(Status.WAITING_APPROVAL, at=LATER + timedelta(minutes=1))

    def test_updates_a_plan_only_while_work_is_active(self) -> None:
        running = Task.receive(
            task_id="task-123",
            input="변경 요청",
            at=NOW,
        ).transition(Status.RUNNING, at=LATER)

        planned = running.update_plan(
            "sha256:plan-v1",
            at=LATER + timedelta(minutes=1),
        )

        self.assertEqual(planned.plan_hash, "sha256:plan-v1")
        self.assertEqual(planned.version, 3)
        self.assertIsNone(running.plan_hash)

        for status in (
            Status.RECEIVED,
            Status.COMPLETED,
            Status.REJECTED,
            Status.FAILED,
            Status.CANCELLED,
            Status.ESCALATED,
        ):
            with (
                self.subTest(status=status),
                self.assertRaises(PlanUpdateNotAllowedError),
            ):
                self._task_at(status).update_plan("sha256:plan-v2", at=LATER)

    def test_resumes_waiting_task_only_with_bound_approval(self) -> None:
        waiting = (
            Task.receive(task_id="task-123", input="변경 요청", at=NOW)
            .transition(Status.RUNNING, at=NOW + timedelta(minutes=1))
            .update_plan("sha256:plan-v1", at=NOW + timedelta(minutes=2))
            .transition(Status.WAITING_APPROVAL, at=NOW + timedelta(minutes=3))
        )

        with self.assertRaises(ApprovalRequiredError):
            waiting.transition(Status.RUNNING, at=NOW + timedelta(minutes=5))

        approval = Approval.grant_for(waiting, at=NOW + timedelta(minutes=4))
        resumed = waiting.transition(
            Status.RUNNING,
            approval=approval,
            at=NOW + timedelta(minutes=5),
        )

        self.assertEqual(approval.task_id, "task-123")
        self.assertEqual(approval.task_version, 4)
        self.assertEqual(approval.plan_hash, "sha256:plan-v1")
        self.assertEqual(resumed.status, Status.RUNNING)
        self.assertEqual(resumed.version, 5)

    def test_rejects_wrong_stale_and_plan_changed_approvals(self) -> None:
        waiting = (
            Task.receive(task_id="task-123", input="변경 요청", at=NOW)
            .transition(Status.RUNNING, at=NOW + timedelta(minutes=1))
            .update_plan("sha256:plan-v1", at=NOW + timedelta(minutes=2))
            .transition(Status.WAITING_APPROVAL, at=NOW + timedelta(minutes=3))
        )
        approved_at = NOW + timedelta(minutes=4)
        wrong_task = Approval(
            task_id="task-other",
            task_version=waiting.version,
            plan_hash="sha256:plan-v1",
            approved_at=approved_at,
        )
        stale = Approval(
            task_id="task-123",
            task_version=waiting.version - 1,
            plan_hash="sha256:plan-v1",
            approved_at=approved_at,
        )
        approval_before_change = Approval.grant_for(waiting, at=approved_at)
        changed = waiting.update_plan(
            "sha256:plan-v2",
            at=NOW + timedelta(minutes=5),
        )

        invalid_cases = (
            (waiting, wrong_task, ApprovalTaskMismatchError),
            (waiting, stale, StaleApprovalError),
            (changed, approval_before_change, PlanChangedError),
        )
        for task, approval, error_type in invalid_cases:
            with self.subTest(error_type=error_type), self.assertRaises(error_type):
                task.transition(
                    Status.RUNNING,
                    approval=approval,
                    at=NOW + timedelta(minutes=6),
                )

    def test_exposes_a_deterministic_binding_for_transactional_replay(self) -> None:
        waiting = (
            Task.receive(task_id="task-123", input="변경 요청", at=NOW)
            .transition(Status.RUNNING, at=NOW + timedelta(minutes=1))
            .update_plan("sha256:plan-v1", at=NOW + timedelta(minutes=2))
            .transition(Status.WAITING_APPROVAL, at=NOW + timedelta(minutes=3))
        )
        first = Approval.grant_for(waiting, at=NOW + timedelta(minutes=4))
        repeated_delivery = Approval.grant_for(
            waiting,
            at=NOW + timedelta(minutes=5),
        )
        changed_plan = waiting.update_plan(
            "sha256:plan-v2",
            at=NOW + timedelta(minutes=6),
        )
        replacement = Approval.grant_for(
            changed_plan,
            at=NOW + timedelta(minutes=7),
        )

        self.assertEqual(
            first.binding,
            ("task-123", 4, "sha256:plan-v1"),
        )
        self.assertEqual(repeated_delivery.binding, first.binding)
        self.assertNotEqual(replacement.binding, first.binding)

    def test_cancels_each_active_status_and_rejects_terminal_cancellation(self) -> None:
        for status in (
            Status.RECEIVED,
            Status.RUNNING,
            Status.WAITING_APPROVAL,
        ):
            with self.subTest(status=status):
                cancelled = self._task_at(status).cancel(at=LATER)
                self.assertEqual(cancelled.status, Status.CANCELLED)
                self.assertGreater(cancelled.version, 1)

        for status in (
            Status.COMPLETED,
            Status.REJECTED,
            Status.FAILED,
            Status.CANCELLED,
            Status.ESCALATED,
        ):
            with (
                self.subTest(status=status),
                self.assertRaises(InvalidStatusTransitionError),
            ):
                self._task_at(status).cancel(at=LATER)

    def test_round_trips_a_task_through_a_json_compatible_snapshot(self) -> None:
        task = (
            Task.receive(task_id="task-123", input="변경 요청", at=NOW)
            .transition(Status.RUNNING, at=NOW + timedelta(minutes=1))
            .update_plan("sha256:plan-v1", at=NOW + timedelta(minutes=2))
            .transition(Status.WAITING_APPROVAL, at=NOW + timedelta(minutes=3))
        )

        encoded = json.dumps(task.to_snapshot())
        restored = Task.from_snapshot(json.loads(encoded))

        self.assertEqual(restored, task)

    def test_rejects_invalid_task_snapshots_and_time_reversal(self) -> None:
        valid = {
            "task_id": "task-123",
            "input": "요청",
            "status": Status.RUNNING,
            "version": 3,
            "plan_hash": "sha256:plan-v1",
            "created_at": NOW,
            "updated_at": LATER,
        }
        invalid_overrides = (
            {"version": 0},
            {"version": True},
            {"status": "RUNNING"},
            {"plan_hash": ""},
            {"status": Status.WAITING_APPROVAL, "plan_hash": None},
            {"updated_at": NOW - timedelta(seconds=1)},
        )

        for override in invalid_overrides:
            with (
                self.subTest(override=override),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                Task(**(valid | override))

        task = Task(**valid)
        with self.assertRaises(InvalidLifecycleValueError):
            task.transition(Status.COMPLETED, at=NOW + timedelta(seconds=30))

    def test_rejects_unreachable_status_version_and_plan_combinations(self) -> None:
        base = {
            "task_id": "task-corrupt",
            "input": "요청",
            "status": Status.RECEIVED,
            "version": 1,
            "plan_hash": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        impossible_overrides = (
            {"status": Status.RECEIVED, "version": 7, "plan_hash": "plan-v1"},
            {"status": Status.RECEIVED, "version": 2, "plan_hash": None},
            {"status": Status.RUNNING, "version": 1, "plan_hash": None},
            {"status": Status.RUNNING, "version": 2, "plan_hash": "plan-v1"},
            {
                "status": Status.WAITING_APPROVAL,
                "version": 3,
                "plan_hash": "plan-v1",
            },
            {"status": Status.COMPLETED, "version": 2, "plan_hash": None},
            {"status": Status.COMPLETED, "version": 3, "plan_hash": "plan-v1"},
            {"status": Status.CANCELLED, "version": 1, "plan_hash": None},
        )

        for override in impossible_overrides:
            with (
                self.subTest(override=override),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                Task(**(base | override))

        corrupt_snapshot = Task.receive(
            task_id="task-corrupt",
            input="요청",
            at=NOW,
        ).to_snapshot() | {"version": 7, "plan_hash": "plan-v1"}
        with self.assertRaises(InvalidLifecycleValueError):
            Task.from_snapshot(corrupt_snapshot)

    def test_enforces_exact_reachable_versions_for_planless_tasks(self) -> None:
        received = Task.receive(task_id="task-planless", input="요청", at=NOW)
        running = received.transition(Status.RUNNING, at=NOW)
        legitimate = (
            received,
            running,
            received.cancel(at=NOW),
            running.cancel(at=NOW),
            running.transition(Status.COMPLETED, at=NOW),
            running.transition(Status.REJECTED, at=NOW),
            running.transition(Status.FAILED, at=NOW),
            running.transition(Status.ESCALATED, at=NOW),
        )
        for task in legitimate:
            with self.subTest(status=task.status, version=task.version):
                self.assertEqual(Task.from_snapshot(task.to_snapshot()), task)

        invalid_versions = {
            Status.RECEIVED: (2, 99),
            Status.RUNNING: (3, 99),
            Status.WAITING_APPROVAL: (4, 99),
            Status.COMPLETED: (2, 4, 99),
            Status.REJECTED: (2, 4, 99),
            Status.FAILED: (2, 4, 99),
            Status.CANCELLED: (1, 4, 99),
            Status.ESCALATED: (2, 4, 99),
        }
        direct_base = {
            "task_id": "task-corrupt",
            "input": "요청",
            "plan_hash": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        snapshot_base = received.to_snapshot() | {"task_id": "task-corrupt"}
        for status, versions in invalid_versions.items():
            for version in versions:
                with (
                    self.subTest(path="direct", status=status, version=version),
                    self.assertRaises(InvalidLifecycleValueError),
                ):
                    Task(**direct_base, status=status, version=version)
                with (
                    self.subTest(path="snapshot", status=status, version=version),
                    self.assertRaises(InvalidLifecycleValueError),
                ):
                    Task.from_snapshot(
                        snapshot_base | {"status": status.value, "version": version}
                    )

    def test_allows_repeated_plan_updates_to_raise_planful_versions(self) -> None:
        running = Task.receive(
            task_id="task-planful",
            input="요청",
            at=NOW,
        ).transition(Status.RUNNING, at=NOW)
        planned = running.update_plan("plan-v1", at=NOW)
        replanned = planned.update_plan("plan-v2", at=NOW)
        waiting = replanned.transition(Status.WAITING_APPROVAL, at=NOW)
        revised_waiting = waiting.update_plan("plan-v3", at=NOW)
        terminal = revised_waiting.transition(Status.REJECTED, at=NOW)

        for task in (planned, replanned, waiting, revised_waiting, terminal):
            with self.subTest(status=task.status, version=task.version):
                self.assertEqual(Task.from_snapshot(task.to_snapshot()), task)

    def test_normalizes_invalid_persisted_snapshots_to_lifecycle_errors(self) -> None:
        snapshot = Task.receive(
            task_id="task-123",
            input="요청",
            at=NOW,
        ).to_snapshot()
        invalid_overrides = (
            {"task_id": None},
            {"version": True},
            {"status": "UNKNOWN"},
            {"created_at": 123},
            {"plan_hash": 456},
        )

        for override in invalid_overrides:
            with (
                self.subTest(override=override),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                Task.from_snapshot(snapshot | override)

    def test_rejects_invalid_or_out_of_order_approval_bindings(self) -> None:
        running = self._task_at(Status.RUNNING)
        with self.assertRaises(ApprovalNotAllowedError):
            Approval.grant_for(running, at=LATER)

        invalid_bindings = (
            {
                "task_id": "",
                "task_version": 7,
                "plan_hash": "sha256:plan-v1",
                "approved_at": LATER,
            },
            {
                "task_id": None,
                "task_version": 7,
                "plan_hash": "sha256:plan-v1",
                "approved_at": LATER,
            },
            {
                "task_id": "task-123",
                "task_version": 0,
                "plan_hash": "sha256:plan-v1",
                "approved_at": LATER,
            },
            {
                "task_id": "task-123",
                "task_version": 7,
                "plan_hash": "",
                "approved_at": LATER,
            },
            {
                "task_id": "task-123",
                "task_version": 7,
                "plan_hash": None,
                "approved_at": LATER,
            },
            {
                "task_id": "task-123",
                "task_version": 7,
                "plan_hash": "sha256:plan-v1",
                "approved_at": LATER.replace(tzinfo=None),
            },
        )
        for binding in invalid_bindings:
            with (
                self.subTest(binding=binding),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                Approval(**binding)

        waiting = self._task_at(Status.WAITING_APPROVAL)
        with self.assertRaises(InvalidLifecycleValueError):
            Approval.grant_for(waiting, at=NOW - timedelta(seconds=1))

        approval = Approval.grant_for(waiting, at=LATER)
        with self.assertRaises(InvalidLifecycleValueError):
            waiting.transition(
                Status.RUNNING,
                approval=approval,
                at=NOW + timedelta(seconds=30),
            )

    @staticmethod
    def _task_at(status: Status) -> Task:
        task = Task.receive(task_id="task-123", input="요청", at=NOW)
        if status is Status.RECEIVED:
            return task
        running = task.transition(Status.RUNNING, at=NOW)
        if status is Status.CANCELLED:
            return task.cancel(at=NOW)
        planned = running.update_plan("plan-v1", at=NOW)
        if status is Status.RUNNING:
            return planned
        if status is Status.WAITING_APPROVAL:
            return planned.transition(Status.WAITING_APPROVAL, at=NOW)
        return planned.transition(status, at=NOW)


class WorkflowRunLifecycleTests(unittest.TestCase):
    """WorkflowRun이 phase와 실행 budget을 추적한다."""

    def test_starts_at_classifying_and_enforces_every_phase_transition(self) -> None:
        task = Task.receive(task_id="task-123", input="요청", at=NOW)
        started = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=task,
            max_agent_runs=3,
            at=NOW,
        )

        self.assertEqual(started.workflow_run_id, "workflow-123")
        self.assertEqual(started.task_id, "task-123")
        self.assertEqual(started.task_version, 1)
        self.assertEqual(started.phase, Phase.CLASSIFYING)
        self.assertEqual(started.budget, ExecutionBudget(limit=3))

        with self.assertRaises(InvalidPhaseTransitionError):
            started.begin_agent_run(
                agent_run_id="agent-run-invalid-retry",
                agent_id="executor",
                retry=True,
                at=LATER,
            )

        allowed = {
            Phase.CLASSIFYING: {Phase.ANALYZING},
            Phase.ANALYZING: {Phase.PLANNING},
            Phase.PLANNING: {Phase.GOVERNING},
            Phase.GOVERNING: {Phase.EXECUTING},
            Phase.EXECUTING: {Phase.VERIFYING},
            Phase.VERIFYING: set(),
        }
        for current, permitted in allowed.items():
            run = WorkflowRun(
                workflow_run_id="workflow-123",
                task_id="task-123",
                task_version=1,
                phase=current,
                budget=ExecutionBudget(limit=3),
                started_at=NOW,
                updated_at=NOW,
            )
            for target in Phase:
                if target in permitted:
                    with self.subTest(current=current, target=target):
                        advanced = run.advance(target, at=LATER)
                        self.assertEqual(advanced.phase, target)
                        self.assertEqual(advanced.updated_at, LATER)
                        self.assertEqual(run.phase, current)
                else:
                    with (
                        self.subTest(current=current, target=target),
                        self.assertRaises(InvalidPhaseTransitionError),
                    ):
                        run.advance(target, at=LATER)

    def test_agent_run_and_retry_atomically_consume_the_execution_budget(self) -> None:
        run = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=Task.receive(task_id="task-123", input="요청", at=NOW),
            max_agent_runs=2,
            at=NOW,
        )

        after_first, first_agent_run = run.begin_agent_run(
            agent_run_id="agent-run-1",
            agent_id="classifier",
            at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(after_first.budget, ExecutionBudget(limit=2, consumed=1))
        self.assertEqual(first_agent_run.phase, Phase.CLASSIFYING)
        self.assertEqual(first_agent_run.budget_sequence, 1)
        self.assertEqual(run.budget.consumed, 0)

        verifying = after_first
        for phase in (
            Phase.ANALYZING,
            Phase.PLANNING,
            Phase.GOVERNING,
            Phase.EXECUTING,
            Phase.VERIFYING,
        ):
            verifying = verifying.advance(
                phase,
                at=verifying.updated_at + timedelta(minutes=1),
            )

        after_retry, retry_agent_run = verifying.begin_agent_run(
            agent_run_id="agent-run-2",
            agent_id="executor",
            retry=True,
            at=verifying.updated_at + timedelta(minutes=1),
        )

        self.assertEqual(after_retry.phase, Phase.EXECUTING)
        self.assertEqual(after_retry.budget, ExecutionBudget(limit=2, consumed=2))
        self.assertEqual(retry_agent_run.phase, Phase.EXECUTING)
        self.assertEqual(retry_agent_run.budget_sequence, 2)

        exhausted = after_retry.advance(
            Phase.VERIFYING,
            at=after_retry.updated_at + timedelta(minutes=1),
        )
        with self.assertRaises(ExecutionBudgetExhaustedError):
            exhausted.begin_agent_run(
                agent_run_id="agent-run-3",
                agent_id="executor",
                retry=True,
                at=exhausted.updated_at + timedelta(minutes=1),
            )

    def test_rejects_invalid_budget_run_identity_and_run_time_reversal(self) -> None:
        invalid_budgets = (
            {"limit": 0},
            {"limit": -1},
            {"limit": True},
            {"limit": 1.5},
            {"limit": 2, "consumed": -1},
            {"limit": 2, "consumed": 3},
            {"limit": 2, "consumed": True},
        )
        for values in invalid_budgets:
            with (
                self.subTest(values=values),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                ExecutionBudget(**values)

        task = Task.receive(task_id="task-123", input="요청", at=NOW)
        with self.assertRaises(InvalidLifecycleValueError):
            WorkflowRun.start(
                workflow_run_id="",
                task=task,
                max_agent_runs=2,
                at=NOW,
            )

        run = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=task,
            max_agent_runs=2,
            at=LATER,
        )
        valid_run = {
            "workflow_run_id": "workflow-123",
            "task_id": "task-123",
            "task_version": 1,
            "phase": Phase.CLASSIFYING,
            "budget": ExecutionBudget(limit=2),
            "started_at": NOW,
            "updated_at": NOW,
        }
        for override in (
            {"task_version": True},
            {"phase": "CLASSIFYING"},
            {"budget": None},
        ):
            with (
                self.subTest(override=override),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                WorkflowRun(**(valid_run | override))

        for operation in (
            lambda: run.advance(Phase.ANALYZING, at=NOW),
            lambda: run.begin_agent_run(
                agent_run_id="agent-run-123",
                agent_id="analysis",
                at=NOW,
            ),
        ):
            with self.assertRaises(InvalidLifecycleValueError):
                operation()


class AgentRunLifecycleTests(unittest.TestCase):
    """AgentRun이 한 번의 Agent 호출을 독립적인 실행 이력으로 남긴다."""

    def test_starts_open_and_completes_once_with_the_selected_agent(self) -> None:
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=Task.receive(task_id="task-123", input="요청", at=NOW),
            max_agent_runs=2,
            at=NOW,
        )
        workflow, started = workflow.begin_agent_run(
            agent_run_id="agent-run-123",
            agent_id="analysis",
            at=NOW,
        )

        self.assertEqual(started.workflow_run_id, "workflow-123")
        self.assertEqual(started.task_id, "task-123")
        self.assertEqual(started.task_version, 1)
        self.assertEqual(started.agent_id, "analysis")
        self.assertEqual(started.phase, Phase.CLASSIFYING)
        self.assertFalse(started.is_completed)

        with self.assertRaises(AgentRunAgentMismatchError):
            started.complete(
                agent_id="other",
                outcome="success",
                output="결과",
                at=LATER,
            )

        completed = started.complete(
            agent_id="analysis",
            outcome="success",
            output="결과",
            at=LATER,
        )

        self.assertTrue(completed.is_completed)
        self.assertEqual(completed.outcome, "success")
        self.assertEqual(completed.output, "결과")
        self.assertEqual(completed.completed_at, LATER)
        self.assertFalse(started.is_completed)

        with self.assertRaises(AgentRunAlreadyCompletedError):
            completed.complete(
                agent_id="analysis",
                outcome="failure",
                output="재완료",
                at=LATER + timedelta(minutes=1),
            )

    def test_rejects_invalid_agent_run_snapshots_and_time_reversal(self) -> None:
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=Task.receive(task_id="task-123", input="요청", at=NOW),
            max_agent_runs=2,
            at=LATER,
        )
        invalid_starts = (
            {"agent_run_id": "", "agent_id": "analysis", "at": LATER},
            {"agent_run_id": "agent-run-123", "agent_id": "", "at": LATER},
            {"agent_run_id": "agent-run-123", "agent_id": "analysis", "at": NOW},
            {
                "agent_run_id": "agent-run-123",
                "agent_id": "analysis",
                "at": LATER.replace(tzinfo=None),
            },
        )
        for values in invalid_starts:
            with (
                self.subTest(values=values),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                workflow.begin_agent_run(**values)

        _, started = workflow.begin_agent_run(
            agent_run_id="agent-run-123",
            agent_id="analysis",
            at=LATER,
        )
        invalid_completions = (
            {"outcome": "", "at": LATER},
            {"outcome": "success", "at": NOW},
        )
        for values in invalid_completions:
            with (
                self.subTest(values=values),
                self.assertRaises(InvalidLifecycleValueError),
            ):
                started.complete(
                    agent_id="analysis",
                    output="결과",
                    **values,
                )

        with self.assertRaises(InvalidLifecycleValueError):
            AgentRun.from_snapshot(
                started.to_snapshot() | {"outcome": "success"},
                workflow=workflow,
            )

        with self.assertRaises(InvalidLifecycleValueError):
            AgentRun.from_snapshot(
                started.to_snapshot() | {"phase": "UNKNOWN"},
                workflow=workflow,
            )

    def test_rejects_agent_run_fabrication_without_matching_consumed_budget(
        self,
    ) -> None:
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=Task.receive(task_id="task-123", input="요청", at=NOW),
            max_agent_runs=2,
            at=NOW,
        )
        workflow, started = workflow.begin_agent_run(
            agent_run_id="agent-run-123",
            agent_id="analysis",
            at=LATER,
        )

        with self.assertRaises(AgentRunOwnershipError):
            AgentRun(
                agent_run_id="fabricated-run",
                workflow_run_id=workflow.workflow_run_id,
                task_id=workflow.task_id,
                task_version=workflow.task_version,
                agent_id="analysis",
                phase=workflow.phase,
                budget_sequence=1,
                started_at=LATER,
            )

        corrupt_snapshots = (
            started.to_snapshot() | {"workflow_run_id": "other-workflow"},
            started.to_snapshot() | {"task_id": "other-task"},
            started.to_snapshot() | {"task_version": 2},
            started.to_snapshot() | {"budget_sequence": 2},
            started.to_snapshot()
            | {"started_at": (LATER + timedelta(minutes=1)).isoformat()},
        )
        for snapshot in corrupt_snapshots:
            with (
                self.subTest(snapshot=snapshot),
                self.assertRaises(AgentRunOwnershipError),
            ):
                AgentRun.from_snapshot(snapshot, workflow=workflow)

        restored = AgentRun.from_snapshot(
            json.loads(json.dumps(started.to_snapshot())),
            workflow=workflow,
        )
        self.assertEqual(restored, started)


class SnapshotSerializationTests(unittest.TestCase):
    """Persisted aggregate가 framework 없는 JSON snapshot으로 왕복된다."""

    def test_round_trips_every_persisted_aggregate_with_literal_wire_values(
        self,
    ) -> None:
        waiting = (
            Task.receive(task_id="task-123", input="변경 요청", at=NOW)
            .transition(Status.RUNNING, at=NOW + timedelta(minutes=1))
            .update_plan("sha256:plan-v1", at=NOW + timedelta(minutes=2))
            .transition(Status.WAITING_APPROVAL, at=NOW + timedelta(minutes=3))
        )
        approval = Approval.grant_for(waiting, at=NOW + timedelta(minutes=4))
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=waiting,
            max_agent_runs=2,
            at=NOW + timedelta(minutes=4),
        )
        workflow, agent_run = workflow.begin_agent_run(
            agent_run_id="agent-run-123",
            agent_id="analysis",
            at=NOW + timedelta(minutes=5),
        )
        agent_run = agent_run.complete(
            agent_id="analysis",
            outcome="success",
            output="결과",
            at=NOW + timedelta(minutes=6),
        )

        self.assertEqual(
            approval.to_snapshot(),
            {
                "task_id": "task-123",
                "task_version": 4,
                "plan_hash": "sha256:plan-v1",
                "approved_at": "2026-07-26T09:04:00+00:00",
            },
        )
        self.assertEqual(
            workflow.to_snapshot(),
            {
                "workflow_run_id": "workflow-123",
                "task_id": "task-123",
                "task_version": 4,
                "phase": "CLASSIFYING",
                "budget": {"limit": 2, "consumed": 1},
                "started_at": "2026-07-26T09:04:00+00:00",
                "updated_at": "2026-07-26T09:05:00+00:00",
            },
        )
        self.assertEqual(
            agent_run.to_snapshot(),
            {
                "agent_run_id": "agent-run-123",
                "workflow_run_id": "workflow-123",
                "task_id": "task-123",
                "task_version": 4,
                "agent_id": "analysis",
                "phase": "CLASSIFYING",
                "budget_sequence": 1,
                "started_at": "2026-07-26T09:05:00+00:00",
                "outcome": "success",
                "output": "결과",
                "completed_at": "2026-07-26T09:06:00+00:00",
            },
        )

        round_trips = (
            (Approval, approval, {}),
            (ExecutionBudget, workflow.budget, {}),
            (WorkflowRun, workflow, {}),
            (AgentRun, agent_run, {"workflow": workflow}),
        )
        for aggregate_type, aggregate, restore_arguments in round_trips:
            with self.subTest(aggregate_type=aggregate_type):
                encoded = json.dumps(aggregate.to_snapshot())
                restored = aggregate_type.from_snapshot(
                    json.loads(encoded),
                    **restore_arguments,
                )
                self.assertEqual(restored, aggregate)

    def test_rejects_malformed_persisted_aggregate_snapshots(self) -> None:
        task = Task.receive(task_id="task-123", input="요청", at=NOW)
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-123",
            task=task,
            max_agent_runs=2,
            at=NOW,
        )
        workflow, agent_run = workflow.begin_agent_run(
            agent_run_id="agent-run-123",
            agent_id="analysis",
            at=NOW,
        )
        waiting = (
            task.transition(Status.RUNNING, at=NOW)
            .update_plan("sha256:plan-v1", at=NOW)
            .transition(Status.WAITING_APPROVAL, at=NOW)
        )
        approval = Approval.grant_for(waiting, at=NOW)
        missing_phase = workflow.to_snapshot()
        del missing_phase["phase"]

        malformed_loaders = (
            lambda: Approval.from_snapshot(
                approval.to_snapshot() | {"task_version": True}
            ),
            lambda: ExecutionBudget.from_snapshot({"limit": 2, "consumed": 3}),
            lambda: WorkflowRun.from_snapshot(missing_phase),
            lambda: WorkflowRun.from_snapshot(
                workflow.to_snapshot() | {"budget": "two"}
            ),
            lambda: AgentRun.from_snapshot(
                agent_run.to_snapshot() | {"completed_at": "not-a-date"},
                workflow=workflow,
            ),
            lambda: AgentRun.from_snapshot(
                agent_run.to_snapshot() | {"budget_sequence": False},
                workflow=workflow,
            ),
        )

        for load in malformed_loaders:
            with self.subTest(load=load), self.assertRaises(InvalidLifecycleValueError):
                load()
