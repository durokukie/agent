"""Runtime application과 bounded background runner의 통합 계약."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from agent_system.agents import AgentMetadata, AgentRegistry, FakeAgent
from agent_system.config import RuntimeSettings
from agent_system.http import create_app
from agent_system.models import ModelSettings
from agent_system.notifications import FakeNotificationSender
from agent_system.orchestration import (
    ActionKind,
    ActionPlan,
    AlertInput,
    Approval,
    ApprovalConsumeStatus,
    ApprovalResponse,
    ApprovalResumeError,
    FakeExecutionCoordinator,
    FakeGovernance,
    FakeRequestClassifier,
    OrchestrationCancellationError,
    OrchestrationJournalEntry,
    OrchestratorService,
    Phase,
    RequestKind,
    RoutingDecision,
    Status,
    Task,
    UserTaskInput,
    WorkflowRun,
)
from agent_system.persistence import (
    IdempotencyKey,
    RuntimeCommandDraft,
    RuntimeCommandType,
    SQLiteStore,
    TaskEventDraft,
    upgrade_database,
)
from agent_system.runtime import (
    ApplicationBusyError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApprovalCommand,
    ApprovalDecision,
    CancelCommand,
    RuntimeApplication,
    SQLiteApprovalConsumer,
    SQLiteLifecycleJournal,
    Submission,
    SubmissionKind,
    build_runtime,
)

NOW = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)


class _RecordingOrchestrator:
    """Background dispatch를 기록하는 runtime용 orchestration fake."""

    def __init__(self) -> None:
        self.starts: list[tuple[object, str, Task]] = []
        self.recoveries: list[str] = []
        self.resumes: list[tuple[str, object]] = []
        self.cancellations: list[str] = []
        self.cancellation_reasons: list[str | None] = []

    async def start(
        self,
        request: object,
        *,
        thread_id: str,
        initial_task: Task,
    ) -> None:
        self.starts.append((request, thread_id, initial_task))

    async def recover(self, *, thread_id: str) -> None:
        self.recoveries.append(thread_id)

    async def resume(self, *, thread_id: str, response: object) -> None:
        self.resumes.append((thread_id, response))

    async def cancel(self, *, thread_id: str, reason: str | None = None) -> None:
        self.cancellations.append(thread_id)
        self.cancellation_reasons.append(reason)


class _FailingOrchestrator(_RecordingOrchestrator):
    """Background failure를 재현하는 runtime orchestration fake."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def start(
        self,
        request: object,
        *,
        thread_id: str,
        initial_task: Task,
    ) -> None:
        await super().start(request, thread_id=thread_id, initial_task=initial_task)
        if self.fail:
            raise RuntimeError("background-secret")


class _BlockingResumeOrchestrator(_RecordingOrchestrator):
    """202 반환 뒤 durable approval intent를 관찰하도록 resume을 막는다."""

    def __init__(self) -> None:
        super().__init__()
        self.resume_entered = asyncio.Event()
        self.release_resume = asyncio.Event()

    async def resume(self, *, thread_id: str, response: object) -> None:
        await super().resume(thread_id=thread_id, response=response)
        self.resume_entered.set()
        await self.release_resume.wait()


class _TerminalCommandReplayOrchestrator(_RecordingOrchestrator):
    """Checkpoint에는 이미 적용됐지만 command만 pending인 crash를 재현한다."""

    def __init__(self, recovered_tasks: dict[str, Task]) -> None:
        super().__init__()
        self.recovered_tasks = recovered_tasks

    async def recover(self, *, thread_id: str) -> object:
        await super().recover(thread_id=thread_id)
        return SimpleNamespace(task=self.recovered_tasks[thread_id])

    async def resume(self, *, thread_id: str, response: object) -> None:
        await super().resume(thread_id=thread_id, response=response)
        raise ApprovalResumeError("이미 terminal인 approval checkpoint")

    async def cancel(self, *, thread_id: str, reason: str | None = None) -> None:
        await super().cancel(thread_id=thread_id, reason=reason)
        raise OrchestrationCancellationError("이미 terminal인 cancel checkpoint")


class _MixedFailureOrchestrator(_RecordingOrchestrator):
    """한 poison command와 늦게 완료되는 정상 command를 함께 실행한다."""

    def __init__(self) -> None:
        super().__init__()
        self.poison_attempts = 0
        self.poison_failed = asyncio.Event()
        self.release_healthy = asyncio.Event()

    async def start(
        self,
        request: object,
        *,
        thread_id: str,
        initial_task: Task,
    ) -> None:
        await super().start(request, thread_id=thread_id, initial_task=initial_task)
        assert isinstance(request, UserTaskInput)
        if request.input == "poison":
            self.poison_attempts += 1
            self.poison_failed.set()
            raise RuntimeError("poison command")
        await self.release_healthy.wait()


class _DelayedEnqueueApplication(RuntimeApplication):
    """Durable commit과 process queue enqueue 사이 shutdown 경합을 재현한다."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.enqueue_entered = asyncio.Event()
        self.release_enqueue = asyncio.Event()

    async def _enqueue_durable(self, work: object) -> None:
        self.enqueue_entered.set()
        await self.release_enqueue.wait()
        await super()._enqueue_durable(work)  # type: ignore[arg-type]


class RuntimeApplicationTests(unittest.IsolatedAsyncioTestCase):
    """SQLite authority와 queue dispatch를 application interface에서 검증한다."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "runtime.sqlite3"
        upgrade_database(self.database_path)
        self.store = SQLiteStore(self.database_path)
        self.orchestrator = _RecordingOrchestrator()
        self.ids = iter(f"task-{number}" for number in range(1, 100))
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=self.orchestrator,
            queue_capacity=8,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=lambda: next(self.ids),
        )
        await self.application.start()

    async def asyncTearDown(self) -> None:
        await self.application.stop()
        self.store.close()
        self.directory.cleanup()

    async def test_persists_received_before_acceptance_and_dispatches_in_background(
        self,
    ) -> None:
        """HTTP 수락 전에 durable v1 또는 queue 분리가 빠지면 실패한다."""

        accepted = await self.application.submit(
            Submission(
                SubmissionKind.USER_TASK,
                {"input": "장애 범위를 읽기 전용으로 조사해 주세요."},
                "request-1",
            )
        )

        self.assertEqual(
            (accepted.task_id, accepted.status, accepted.version, accepted.replayed),
            ("task-1", "RECEIVED", 1, False),
        )
        persisted = self.store.get_task("task-1")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status.value, "RECEIVED")

        await self.application.drain()

        self.assertEqual(len(self.orchestrator.starts), 1)
        request, thread_id, initial_task = self.orchestrator.starts[0]
        self.assertEqual(
            request,
            UserTaskInput(
                task_id="task-1",
                input="장애 범위를 읽기 전용으로 조사해 주세요.",
            ),
        )
        self.assertEqual(thread_id, "task-1")
        self.assertEqual(initial_task, persisted)

    async def test_webhook_replays_same_payload_and_conflicts_on_changed_payload(
        self,
    ) -> None:
        """Canonical fingerprint나 stable webhook identity가 빠지면 실패한다."""

        first = await self.application.submit(
            Submission(
                SubmissionKind.ALERT,
                {
                    "alert_id": "alert-7",
                    "severity": "critical",
                    "message": "database unavailable",
                },
            )
        )
        await self.application.drain()
        replay = await self.application.submit(
            Submission(
                SubmissionKind.ALERT,
                {
                    "message": "database unavailable",
                    "severity": "critical",
                    "alert_id": "alert-7",
                },
            )
        )

        with self.assertRaises(ApplicationConflictError):
            await self.application.submit(
                Submission(
                    SubmissionKind.ALERT,
                    {
                        "alert_id": "alert-7",
                        "severity": "warning",
                        "message": "database unavailable",
                    },
                )
            )

        self.assertEqual(first.task_id, replay.task_id)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        await self.application.drain()
        self.assertEqual(len(self.orchestrator.starts), 1)
        self.assertEqual(
            self.orchestrator.starts[0][0],
            AlertInput(
                task_id=first.task_id,
                alert_id="alert-7",
                severity="critical",
                message="database unavailable",
            ),
        )

    async def test_queue_full_keeps_durable_start_intent_and_pumps_it_later(
        self,
    ) -> None:
        """QueueFull 뒤 commit된 Task가 같은 process에서도 고아가 되면 실패한다."""

        await self.application.stop()
        orchestrator = _BlockingOrchestrator()
        ids = iter(("queue-1", "queue-2", "queue-3"))
        application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=lambda: next(ids),
        )
        self.application = application
        await application.start()

        await application.submit(
            Submission(SubmissionKind.USER_TASK, {"input": "첫 번째"})
        )
        await orchestrator.entered.wait()
        await application.submit(
            Submission(SubmissionKind.USER_TASK, {"input": "두 번째"})
        )
        try:
            third = await application.submit(
                Submission(SubmissionKind.USER_TASK, {"input": "세 번째"})
            )
            self.assertIsNotNone(self.store.get_task("queue-3"))
            self.assertEqual(third.task_id, "queue-3")
            self.assertCountEqual(
                (
                    command.task_id
                    for command in self.store.list_pending_runtime_commands()
                ),
                ("queue-1", "queue-2", "queue-3"),
            )
        finally:
            orchestrator.release.set()
        await application.drain()

        self.assertEqual(
            [call[1] for call in orchestrator.starts],
            ["queue-1", "queue-2", "queue-3"],
        )
        self.assertEqual(self.store.list_pending_runtime_commands(), ())

    async def test_stop_blocks_admission_and_post_sentinel_enqueue(self) -> None:
        """Stop 시작 뒤 accepted work가 종료된 worker queue에 들어가면 실패한다."""

        await self.application.stop()
        application = _DelayedEnqueueApplication(
            store=self.store,
            orchestrator=self.orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=iter(("stop-race", "late-admission")).__next__,
        )
        self.application = application
        await application.start()
        submit = asyncio.create_task(
            application.submit(
                Submission(SubmissionKind.USER_TASK, {"input": "종료 경합"})
            )
        )
        await application.enqueue_entered.wait()

        stopping = asyncio.create_task(application.stop())
        await asyncio.sleep(0)
        with self.assertRaises(ApplicationBusyError):
            await application.submit(
                Submission(SubmissionKind.USER_TASK, {"input": "늦은 수락"})
            )
        await stopping
        application.release_enqueue.set()
        accepted = await submit

        self.assertEqual(accepted.task_id, "stop-race")
        self.assertEqual(self.orchestrator.starts, [])
        pending = self.store.list_pending_runtime_commands()
        self.assertEqual([command.task_id for command in pending], ["stop-race"])

    async def test_concurrent_stop_closes_resources_once(self) -> None:
        """동시 stop은 worker와 소유 자원을 한 번만 종료해야 한다."""

        await self.application.stop()
        close_calls = 0

        def close_resources() -> None:
            nonlocal close_calls
            close_calls += 1

        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=self.orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=lambda: "unused",
            close_resources=close_resources,
        )
        await self.application.start()

        await asyncio.gather(self.application.stop(), self.application.stop())

        self.assertEqual(close_calls, 1)

    async def test_other_worker_completion_does_not_retry_a_poison_command(
        self,
    ) -> None:
        """다른 command 완료가 실패 command의 즉시 재시도 trigger가 되면 실패한다."""

        await self.application.stop()
        orchestrator = _MixedFailureOrchestrator()
        ids = iter(("poison-task", "healthy-task"))
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=4,
            worker_count=2,
            clock=lambda: NOW,
            id_factory=lambda: next(ids),
        )
        await self.application.start()

        await self.application.submit(
            Submission(SubmissionKind.USER_TASK, {"input": "poison"}, "poison")
        )
        await self.application.submit(
            Submission(SubmissionKind.USER_TASK, {"input": "healthy"}, "healthy")
        )
        await orchestrator.poison_failed.wait()
        orchestrator.release_healthy.set()
        await self.application.drain()

        self.assertEqual(orchestrator.poison_attempts, 1)
        pending = self.store.list_pending_runtime_commands()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].task_id, "poison-task")
        self.assertEqual(pending[0].attempt_count, 1)

    async def test_background_failure_stays_pending_without_a_tight_retry_loop(
        self,
    ) -> None:
        """실패 command를 즉시 무한 재queue하거나 완료 처리하면 실패한다."""

        await self.application.stop()
        orchestrator = _FailingOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=lambda: "failure-task",
        )
        await self.application.start()
        await self.application.submit(
            Submission(
                SubmissionKind.USER_TASK,
                {"input": "실패 재현"},
                "failure-key",
            )
        )
        try:
            await asyncio.wait_for(self.application.drain(), timeout=0.1)
        finally:
            orchestrator.fail = False

        pending = self.store.list_pending_runtime_commands()
        self.assertEqual(len(orchestrator.starts), 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempt_count, 1)
        self.assertEqual(pending[0].last_error, "background_execution_failed")

        replay = await self.application.submit(
            Submission(
                SubmissionKind.USER_TASK,
                {"input": "실패 재현"},
                "failure-key",
            )
        )
        await self.application.drain()

        self.assertTrue(replay.replayed)
        self.assertEqual(len(orchestrator.starts), 2)
        self.assertEqual(self.store.list_pending_runtime_commands(), ())

    async def test_restart_preserves_failed_command_retry_eligibility(self) -> None:
        """재시작이 durable backoff를 지우거나 영구 suppression하면 실패한다."""

        await self.application.stop()
        failing = _FailingOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=failing,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=lambda: "retry-restart",
        )
        await self.application.start()
        await self.application.submit(
            Submission(SubmissionKind.USER_TASK, {"input": "재시작 backoff"})
        )
        await self.application.drain()
        await self.application.stop()

        before_eligible = _RecordingOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=before_eligible,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=1),
            id_factory=lambda: "unused-before",
        )
        await self.application.start()
        await asyncio.sleep(0)
        self.assertEqual(before_eligible.starts, [])
        self.assertEqual(
            self.store.list_pending_runtime_commands()[0].attempt_count,
            1,
        )
        await self.application.stop()

        eligible = _RecordingOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=eligible,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=31),
            id_factory=lambda: "unused-after",
        )
        await self.application.start()
        await self.application.drain()

        self.assertEqual([call[1] for call in eligible.starts], ["retry-restart"])
        self.assertEqual(self.store.list_pending_runtime_commands(), ())

    async def test_startup_recovers_a_durable_received_task_once(self) -> None:
        """Process restart가 queue 이전 RECEIVED Task를 잃으면 실패한다."""

        await self.application.stop()
        task = Task.receive(task_id="task-restart", input="재시작 복구", at=NOW)
        self.store.create_task(
            task,
            event=TaskEventDraft(
                event_id="event:task-restart:1",
                event_type="TASK_RECEIVED",
                payload={
                    "request": {
                        "kind": "user_task",
                        "task_id": "task-restart",
                        "input": "재시작 복구",
                    }
                },
                occurred_at=NOW,
            ),
            idempotency=IdempotencyKey(
                namespace="task",
                key="restart-key",
                fingerprint="literal-fingerprint",
                created_at=NOW,
            ),
        )
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=self.orchestrator,
            queue_capacity=8,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=lambda: next(self.ids),
        )

        await self.application.start()
        await self.application.drain()
        await self.application.start()
        await self.application.drain()

        recovered_starts = [
            call for call in self.orchestrator.starts if call[1] == "task-restart"
        ]
        self.assertEqual(len(recovered_starts), 1)

    async def test_startup_defers_legacy_recovery_beyond_queue_capacity(self) -> None:
        """Migration 전 Task가 queue 용량보다 많아도 startup은 성공해야 한다."""

        await self.application.stop()
        for index in range(3):
            task = Task.receive(
                task_id=f"legacy-{index}",
                input="legacy recovery",
                at=NOW + timedelta(seconds=index),
            )
            self.store.create_task(
                task,
                event=TaskEventDraft(
                    event_id=f"event:legacy-{index}:1",
                    event_type="TASK_RECEIVED",
                    payload={
                        "request": {
                            "kind": "user_task",
                            "task_id": task.task_id,
                            "input": task.input,
                        }
                    },
                    occurred_at=task.updated_at,
                ),
            )
        orchestrator = _BlockingOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=10),
            id_factory=lambda: "unused",
        )

        await self.application.start()
        await orchestrator.entered.wait()
        orchestrator.release.set()
        await self.application.drain()

        self.assertCountEqual(
            (thread_id for _, thread_id, _ in orchestrator.starts),
            ("legacy-0", "legacy-1", "legacy-2"),
        )

    async def test_startup_recovers_active_checkpoint_by_runtime_task_thread(
        self,
    ) -> None:
        """Workflow ID를 checkpoint thread로 오인하면 실패한다."""

        await self.application.stop()
        received = Task.receive(task_id="active-thread", input="복구", at=NOW)
        self.store.create_task(
            received,
            event=TaskEventDraft(
                "event:active-thread:1",
                "TASK_RECEIVED",
                {
                    "request": {
                        "kind": "user_task",
                        "task_id": "active-thread",
                        "input": "복구",
                    }
                },
                NOW,
            ),
        )
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        self.store.save_task(
            running,
            expected_version=1,
            event=TaskEventDraft(
                "event:active-thread:2", "TASK_STARTED", {}, running.updated_at
            ),
        )
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-not-thread",
            task=running,
            max_agent_runs=2,
            at=running.updated_at,
        )
        self.store.create_workflow_run(workflow)
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=self.orchestrator,
            queue_capacity=8,
            worker_count=1,
            clock=lambda: NOW,
            id_factory=lambda: next(self.ids),
        )

        await self.application.start()
        await self.application.drain()

        self.assertIn("active-thread", self.orchestrator.recoveries)
        self.assertNotIn("workflow-not-thread", self.orchestrator.recoveries)

    async def test_queues_bound_accept_and_reject_responses(self) -> None:
        """Approval command의 version/plan binding 검증이 빠지면 실패한다."""

        first = self._persist_waiting_task("approval-1")
        second = self._persist_waiting_task("approval-2")

        accepted = await self.application.approve(
            first.task_id,
            ApprovalCommand(
                decision_id="decision-accept",
                decision=ApprovalDecision.APPROVE,
                task_version=first.version,
                plan_hash=first.plan_hash or "",
            ),
        )
        rejected = await self.application.approve(
            second.task_id,
            ApprovalCommand(
                decision_id="decision-reject",
                decision=ApprovalDecision.REJECT,
                task_version=second.version,
                plan_hash=second.plan_hash or "",
                reason="변경 창구가 닫혔습니다.",
            ),
        )
        await self.application.drain()

        self.assertEqual(accepted.status, "WAITING_APPROVAL")
        self.assertEqual(rejected.status, "WAITING_APPROVAL")
        self.assertEqual(len(self.orchestrator.resumes), 2)
        accept_response = self.orchestrator.resumes[0][1]
        reject_response = self.orchestrator.resumes[1][1]
        self.assertTrue(accept_response.accepted)
        self.assertEqual(
            accept_response.approval.binding,
            (first.task_id, first.version, first.plan_hash),
        )
        self.assertFalse(reject_response.accepted)
        self.assertEqual(reject_response.reason, "변경 창구가 닫혔습니다.")

        with self.assertRaises(ApplicationConflictError):
            await self.application.approve(
                first.task_id,
                ApprovalCommand(
                    decision_id="stale",
                    decision=ApprovalDecision.APPROVE,
                    task_version=first.version - 1,
                    plan_hash=first.plan_hash or "",
                ),
            )

    async def test_approval_202_is_durable_and_heterogeneous_cancel_conflicts(
        self,
    ) -> None:
        """승인 intent 미저장 또는 Task 단위 이종 command 덮어쓰기를 막는다."""

        await self.application.stop()
        waiting = self._persist_waiting_task("durable-approval")
        orchestrator = _BlockingResumeOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=2,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=4),
            id_factory=lambda: "unused",
        )
        await self.application.start()

        accepted = await self.application.approve(
            waiting.task_id,
            ApprovalCommand(
                decision_id="durable-decision",
                decision=ApprovalDecision.APPROVE,
                task_version=waiting.version,
                plan_hash=waiting.plan_hash or "",
            ),
        )
        await orchestrator.resume_entered.wait()
        try:
            pending = self.store.list_pending_runtime_commands()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].command_type, RuntimeCommandType.APPROVAL)
            self.assertEqual(
                pending[0].payload["response"]["decision_id"],
                "durable-decision",
            )
            with self.assertRaises(ApplicationConflictError):
                await self.application.cancel(
                    waiting.task_id,
                    CancelCommand(expected_version=waiting.version),
                )
        finally:
            orchestrator.release_resume.set()
        await self.application.drain()

        self.assertEqual(accepted.status, Status.WAITING_APPROVAL.value)
        self.assertEqual(self.store.list_pending_runtime_commands(), ())

    async def test_sqlite_approval_consumer_maps_atomic_rejection_and_replay(
        self,
    ) -> None:
        """Async adapter가 SQLite decision CAS 결과를 잃으면 실패한다."""

        waiting = self._persist_waiting_task("consumer-1")
        consumer = SQLiteApprovalConsumer(self.store)
        response = ApprovalResponse.reject(
            decision_id="decision-consumer",
            reason="승인하지 않습니다.",
        )

        first = await consumer.consume(
            task=waiting, response=response, at=NOW + timedelta(seconds=4)
        )
        replay = await consumer.consume(
            task=waiting, response=response, at=NOW + timedelta(seconds=5)
        )

        self.assertEqual(first.status, ApprovalConsumeStatus.APPLIED)
        self.assertEqual(replay.status, ApprovalConsumeStatus.ALREADY_APPLIED)
        self.assertEqual(first.task, replay.task)
        self.assertEqual(first.task.status, Status.REJECTED)
        self.assertEqual(len(self.store.list_approval_decisions(waiting.task_id)), 1)
        canonical_reject = {
            "decision_id": response.decision_id,
            "accepted": False,
            "errors": ["human_rejected"],
        }
        rejected_replay = await SQLiteLifecycleJournal(self.store).record(
            OrchestrationJournalEntry(
                task=first.task,
                workflow=None,
                task_event_type="TASK_REJECTED",
                task_event_payload=canonical_reject,
            )
        )
        self.assertEqual(rejected_replay.task, first.task)

        accepted_waiting = self._persist_waiting_task("consumer-accepted")
        accepted_response = ApprovalResponse(
            decision_id="decision-consumer-accepted",
            accepted=True,
            approval=Approval(
                task_id=accepted_waiting.task_id,
                task_version=accepted_waiting.version,
                plan_hash=accepted_waiting.plan_hash or "",
                approved_at=NOW + timedelta(seconds=4),
            ),
        )
        accepted_result = await consumer.consume(
            task=accepted_waiting,
            response=accepted_response,
            at=NOW + timedelta(seconds=4),
        )
        accepted_replay = await SQLiteLifecycleJournal(self.store).record(
            OrchestrationJournalEntry(
                task=accepted_result.task,
                workflow=None,
                task_event_type="TASK_APPROVED",
                task_event_payload={
                    "decision_id": accepted_response.decision_id,
                    "accepted": True,
                    "errors": [],
                },
            )
        )
        self.assertEqual(accepted_replay.task, accepted_result.task)

    async def test_rejects_approval_cas_without_preceding_durable_intent(
        self,
    ) -> None:
        """SQLite intent 없이 먼저 적용된 CAS를 command로 오인하지 않는다."""

        waiting = self._persist_waiting_task("approval-crash")
        response = ApprovalResponse(
            decision_id="decision-crash",
            accepted=True,
            approval=Approval(
                task_id=waiting.task_id,
                task_version=waiting.version,
                plan_hash=waiting.plan_hash or "",
                approved_at=NOW + timedelta(seconds=4),
            ),
        )
        await SQLiteApprovalConsumer(self.store).consume(
            task=waiting,
            response=response,
            at=NOW + timedelta(seconds=4),
        )

        with self.assertRaises(ApplicationConflictError):
            await self.application.approve(
                waiting.task_id,
                ApprovalCommand(
                    decision_id=response.decision_id,
                    decision=ApprovalDecision.APPROVE,
                    task_version=waiting.version,
                    plan_hash=waiting.plan_hash or "",
                ),
            )

    async def test_startup_replays_durable_approval_commands_after_cas_crash(
        self,
    ) -> None:
        """Client retry 없이 accept/reject CAS 뒤 checkpoint를 자동 복구한다."""

        await self.application.stop()
        accepted = self._persist_waiting_task("startup-approval")
        rejected = self._persist_waiting_task("startup-rejection")
        responses = (
            ApprovalResponse(
                decision_id="startup-accept",
                accepted=True,
                approval=Approval(
                    task_id=accepted.task_id,
                    task_version=accepted.version,
                    plan_hash=accepted.plan_hash or "",
                    approved_at=NOW + timedelta(seconds=4),
                ),
            ),
            ApprovalResponse.reject(
                decision_id="startup-reject",
                reason="승인하지 않습니다.",
            ),
        )
        for task, response in zip((accepted, rejected), responses, strict=True):
            self.store.put_runtime_command(
                RuntimeCommandDraft(
                    command_id=f"command:{response.decision_id}",
                    task_id=task.task_id,
                    command_type=RuntimeCommandType.APPROVAL,
                    fingerprint=f"approval:{response.decision_id}",
                    payload={"response": response.to_snapshot()},
                    created_at=NOW + timedelta(seconds=4),
                ),
                expected_task=task,
            )
            await SQLiteApprovalConsumer(self.store).consume(
                task=task,
                response=response,
                at=NOW + timedelta(seconds=4),
            )

        orchestrator = _RecordingOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=5),
            id_factory=lambda: "unused",
        )
        await self.application.start()
        await self.application.drain()

        self.assertCountEqual(
            (response for _, response in orchestrator.resumes),
            responses,
        )
        self.assertEqual(self.store.list_pending_runtime_commands(), ())

    async def test_startup_completes_commands_already_terminal_in_checkpoint(
        self,
    ) -> None:
        """Graph terminal 후 command 완료 전 crash는 exact recover로 종료해야 한다."""

        await self.application.stop()
        approval_task = self._persist_waiting_task("terminal-approval")
        approval_response = ApprovalResponse.reject(
            decision_id="terminal-decision",
            reason="승인하지 않음",
        )
        self.store.put_runtime_command(
            RuntimeCommandDraft(
                command_id="command:terminal-approval",
                task_id=approval_task.task_id,
                command_type=RuntimeCommandType.APPROVAL,
                fingerprint="approval:terminal",
                payload={"response": approval_response.to_snapshot()},
                created_at=NOW + timedelta(seconds=4),
            ),
            expected_task=approval_task,
        )
        await SQLiteApprovalConsumer(self.store).consume(
            task=approval_task,
            response=approval_response,
            at=NOW + timedelta(seconds=4),
        )

        cancel_task = self._persist_waiting_task("terminal-cancel")
        cancel_reason = "운영자 중단"
        self.store.put_runtime_command(
            RuntimeCommandDraft(
                command_id="command:terminal-cancel",
                task_id=cancel_task.task_id,
                command_type=RuntimeCommandType.CANCEL,
                fingerprint="cancel:terminal",
                payload={
                    "expected_version": cancel_task.version,
                    "reason": cancel_reason,
                },
                created_at=NOW + timedelta(seconds=4),
            ),
            expected_task=cancel_task,
        )
        cancelled = cancel_task.cancel(at=NOW + timedelta(seconds=4))
        self.store.save_task(
            cancelled,
            expected_version=cancel_task.version,
            event=TaskEventDraft(
                "event:terminal-cancel:5",
                "TASK_CANCELLED",
                {"reason": cancel_reason, "errors": []},
                cancelled.updated_at,
            ),
        )
        orchestrator = _TerminalCommandReplayOrchestrator(
            {
                approval_task.task_id: self.store.get_task(approval_task.task_id),
                cancel_task.task_id: cancelled,
            }
        )
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=2,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=5),
            id_factory=lambda: "unused",
        )

        await self.application.start()
        await self.application.drain()

        self.assertCountEqual(
            orchestrator.recoveries,
            ("terminal-approval", "terminal-cancel"),
        )
        self.assertEqual(self.store.list_pending_runtime_commands(), ())

    async def test_terminal_reconcile_rejects_unrelated_terminal_provenance(
        self,
    ) -> None:
        """다른 terminal event를 approval command 성공으로 오인하면 실패한다."""

        await self.application.stop()
        waiting = self._persist_waiting_task("terminal-mismatch")
        response = ApprovalResponse.reject(
            decision_id="expected-rejection",
            reason="거절",
        )
        self.store.put_runtime_command(
            RuntimeCommandDraft(
                command_id="command:terminal-mismatch",
                task_id=waiting.task_id,
                command_type=RuntimeCommandType.APPROVAL,
                fingerprint="approval:mismatch",
                payload={"response": response.to_snapshot()},
                created_at=NOW + timedelta(seconds=4),
            ),
            expected_task=waiting,
        )
        cancelled = waiting.cancel(at=NOW + timedelta(seconds=4))
        self.store.save_task(
            cancelled,
            expected_version=waiting.version,
            event=TaskEventDraft(
                "event:terminal-mismatch:cancelled",
                "TASK_CANCELLED",
                {"reason": "unrelated", "errors": []},
                cancelled.updated_at,
            ),
        )
        orchestrator = _TerminalCommandReplayOrchestrator({waiting.task_id: cancelled})
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=5),
            id_factory=lambda: "unused",
        )

        await self.application.start()
        await self.application.drain()

        pending = self.store.list_pending_runtime_commands()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempt_count, 1)
        self.assertEqual(pending[0].last_error, "background_execution_failed")

    async def test_sqlite_journal_persists_each_aggregate_step_and_replays_it(
        self,
    ) -> None:
        """Final snapshot만 저장하거나 journal replay가 비멱등이면 실패한다."""

        received = Task.receive(task_id="journal-1", input="journal 대상", at=NOW)
        self.store.create_task(
            received,
            event=TaskEventDraft(
                "event:journal-1:1",
                "TASK_RECEIVED",
                {
                    "request": {
                        "kind": "user_task",
                        "task_id": "journal-1",
                        "input": "journal 대상",
                    }
                },
                NOW,
            ),
        )
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-journal-1",
            task=running,
            max_agent_runs=2,
            at=NOW + timedelta(seconds=1),
        )
        journal = SQLiteLifecycleJournal(self.store)
        started_entry = OrchestrationJournalEntry(
            task=running,
            workflow=workflow,
            task_event_type="TASK_STARTED",
            task_event_payload={},
        )

        self.assertEqual(await journal.record(started_entry), started_entry)
        analyzing = workflow.advance(Phase.ANALYZING, at=NOW + timedelta(seconds=2))
        analyzing_entry = OrchestrationJournalEntry(task=running, workflow=analyzing)
        self.assertEqual(await journal.record(analyzing_entry), analyzing_entry)
        replayed_analyzing = await journal.record(
            OrchestrationJournalEntry(
                task=replace(
                    running, updated_at=running.updated_at + timedelta(seconds=9)
                ),
                workflow=replace(
                    analyzing,
                    updated_at=analyzing.updated_at + timedelta(seconds=9),
                ),
            )
        )
        self.assertEqual(replayed_analyzing.task, running)
        self.assertEqual(replayed_analyzing.workflow, analyzing)
        planning = analyzing.advance(Phase.PLANNING, at=NOW + timedelta(seconds=3))
        planning_entry = OrchestrationJournalEntry(task=running, workflow=planning)
        self.assertEqual(await journal.record(planning_entry), planning_entry)
        governing = planning.advance(Phase.GOVERNING, at=NOW + timedelta(seconds=4))
        await journal.record(
            OrchestrationJournalEntry(task=running, workflow=governing)
        )
        executing = governing.advance(Phase.EXECUTING, at=NOW + timedelta(seconds=5))
        await journal.record(
            OrchestrationJournalEntry(task=running, workflow=executing)
        )
        issued_workflow, open_run = executing.begin_agent_run(
            agent_run_id="agent-run-journal-1",
            agent_id="operations-agent",
            at=NOW + timedelta(seconds=6),
        )
        issuance = OrchestrationJournalEntry(
            task=running,
            workflow=issued_workflow,
            agent_run=open_run,
        )
        self.assertEqual(await journal.record(issuance), issuance)
        completed_run = open_run.complete(
            agent_id="operations-agent",
            outcome="success",
            output="완료",
            at=NOW + timedelta(seconds=7),
        )
        verifying = issued_workflow.advance(
            Phase.VERIFYING,
            at=NOW + timedelta(seconds=7),
        )
        completion = OrchestrationJournalEntry(
            task=running,
            workflow=verifying,
            agent_run=completed_run,
        )
        self.assertEqual(await journal.record(completion), completion)
        terminal = running.transition(Status.COMPLETED, at=NOW + timedelta(seconds=8))
        terminal_entry = OrchestrationJournalEntry(
            task=terminal,
            workflow=verifying,
            task_event_type="TASK_COMPLETED",
            task_event_payload={"output": "완료", "errors": []},
        )
        self.assertEqual(await journal.record(terminal_entry), terminal_entry)

        # Crash callback replay는 event·issuance를 중복하지 않는다.
        self.assertEqual(await journal.record(terminal_entry), terminal_entry)
        self.assertEqual(self.store.get_task(terminal.task_id), terminal)
        self.assertEqual(len(self.store.list_task_events(terminal.task_id)), 3)
        self.assertEqual(
            self.store.list_agent_runs(verifying.workflow_run_id),
            (completed_run,),
        )

        # DB가 checkpoint보다 terminal까지 앞선 fault를 주입하고 모든 callback을 재생한다.
        replayed_received = await journal.record(
            OrchestrationJournalEntry(
                task=received,
                workflow=None,
                task_event_type="TASK_RECEIVED",
                task_event_payload={
                    "request": {
                        "kind": "user_task",
                        "task_id": "journal-1",
                        "input": "journal 대상",
                    }
                },
            )
        )
        self.assertEqual(replayed_received.task, received)
        replay_running = received.transition(
            Status.RUNNING,
            at=NOW + timedelta(seconds=20),
        )
        replay_workflow = WorkflowRun.start(
            workflow_run_id="different-workflow-after-crash",
            task=replay_running,
            max_agent_runs=2,
            at=replay_running.updated_at,
        )
        replayed_started = await journal.record(
            OrchestrationJournalEntry(
                task=replay_running,
                workflow=replay_workflow,
                task_event_type="TASK_STARTED",
                task_event_payload={},
            )
        )
        self.assertEqual(replayed_started.task.status, Status.RUNNING)
        self.assertEqual(
            replayed_started.workflow.workflow_run_id,
            "workflow-journal-1",
        )
        replay_workflow = replayed_started.workflow
        assert replay_workflow is not None
        for index, phase in enumerate(
            (
                Phase.ANALYZING,
                Phase.PLANNING,
                Phase.GOVERNING,
                Phase.EXECUTING,
            ),
            start=21,
        ):
            replay_workflow = replay_workflow.advance(
                phase,
                at=NOW + timedelta(seconds=index),
            )
            phase_entry = await journal.record(
                OrchestrationJournalEntry(
                    task=replayed_started.task,
                    workflow=replay_workflow,
                )
            )
            assert phase_entry.workflow is not None
            replay_workflow = phase_entry.workflow
        replay_issued, replay_open = replay_workflow.begin_agent_run(
            agent_run_id="different-agent-run-after-crash",
            agent_id="operations-agent",
            at=NOW + timedelta(seconds=25),
        )
        replayed_issuance = await journal.record(
            OrchestrationJournalEntry(
                task=replayed_started.task,
                workflow=replay_issued,
                agent_run=replay_open,
            )
        )
        self.assertEqual(
            replayed_issuance.agent_run.agent_run_id,
            "agent-run-journal-1",
        )
        assert replayed_issuance.workflow is not None
        assert replayed_issuance.agent_run is not None
        replay_completed = replayed_issuance.agent_run.complete(
            agent_id="operations-agent",
            outcome="success",
            output="완료",
            at=NOW + timedelta(seconds=26),
        )
        replay_verifying = replayed_issuance.workflow.advance(
            Phase.VERIFYING,
            at=NOW + timedelta(seconds=26),
        )
        replayed_completion = await journal.record(
            OrchestrationJournalEntry(
                task=replayed_started.task,
                workflow=replay_verifying,
                agent_run=replay_completed,
            )
        )
        self.assertEqual(replayed_completion.agent_run, completed_run)
        replay_terminal = replayed_started.task.transition(
            Status.COMPLETED,
            at=NOW + timedelta(seconds=27),
        )
        replayed_terminal = await journal.record(
            OrchestrationJournalEntry(
                task=replay_terminal,
                workflow=replayed_completion.workflow,
                task_event_type="TASK_COMPLETED",
                task_event_payload={"output": "완료", "errors": []},
            )
        )
        self.assertEqual(replayed_terminal.task, terminal)

    async def test_journal_replays_the_actual_received_event_after_started_commit(
        self,
    ) -> None:
        """STARTED commit 후 첫 checkpoint 전 crash를 실제 request event로 재생한다."""

        received = Task.receive(task_id="received-crash", input="복구", at=NOW)
        request_payload = {
            "request": {
                "kind": "user_task",
                "task_id": received.task_id,
                "input": received.input,
            }
        }
        self.store.create_task(
            received,
            event=TaskEventDraft(
                "event:received-crash:1",
                "TASK_RECEIVED",
                request_payload,
                received.updated_at,
            ),
        )
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-received-crash",
            task=running,
            max_agent_runs=2,
            at=running.updated_at,
        )
        journal = SQLiteLifecycleJournal(self.store)
        await journal.record(
            OrchestrationJournalEntry(
                task=running,
                workflow=workflow,
                task_event_type="TASK_STARTED",
                task_event_payload={},
            )
        )

        replayed = await journal.record(
            OrchestrationJournalEntry(
                task=replace(received, updated_at=NOW + timedelta(minutes=1)),
                workflow=None,
                task_event_type="TASK_RECEIVED",
                task_event_payload=request_payload,
            )
        )

        self.assertEqual(replayed.task, received)

    async def test_journal_rejects_historical_task_with_wrong_event_semantics(
        self,
    ) -> None:
        """Event row 존재만으로 잘못된 historical Task snapshot을 승인하지 않는다."""

        received = Task.receive(task_id="semantic-task", input="검증", at=NOW)
        self.store.create_task(
            received,
            event=TaskEventDraft(
                "event:semantic-task:1", "TASK_RECEIVED", {}, received.updated_at
            ),
        )
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        workflow = WorkflowRun.start(
            workflow_run_id="workflow-semantic-task",
            task=running,
            max_agent_runs=2,
            at=running.updated_at,
        )
        journal = SQLiteLifecycleJournal(self.store)
        await journal.record(
            OrchestrationJournalEntry(
                task=running,
                workflow=workflow,
                task_event_type="TASK_STARTED",
                task_event_payload={},
            )
        )
        analyzing = workflow.advance(Phase.ANALYZING, at=NOW + timedelta(seconds=2))
        await journal.record(
            OrchestrationJournalEntry(task=running, workflow=analyzing)
        )
        completed = running.transition(
            Status.COMPLETED,
            at=NOW + timedelta(seconds=3),
        )
        await journal.record(
            OrchestrationJournalEntry(
                task=completed,
                workflow=analyzing,
                task_event_type="TASK_COMPLETED",
                task_event_payload={"output": "완료", "errors": []},
            )
        )
        forged = received.cancel(at=NOW + timedelta(seconds=4))

        with self.assertRaises(ApplicationConflictError):
            await journal.record(
                OrchestrationJournalEntry(task=forged, workflow=analyzing)
            )

    async def test_journal_rejects_historical_task_with_forged_plan_hash(
        self,
    ) -> None:
        """Event payload와 다른 historical plan hash를 승인하면 실패한다."""

        received = Task.receive(task_id="forged-plan", input="검증", at=NOW)
        self.store.create_task(
            received,
            event=TaskEventDraft(
                "event:forged-plan:1", "TASK_RECEIVED", {}, received.updated_at
            ),
        )
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        self.store.save_task(
            running,
            expected_version=received.version,
            event=TaskEventDraft(
                "event:forged-plan:2", "TASK_STARTED", {}, running.updated_at
            ),
        )
        planned = running.update_plan("sha256:real", at=NOW + timedelta(seconds=2))
        self.store.save_task(
            planned,
            expected_version=running.version,
            event=TaskEventDraft(
                "event:forged-plan:3",
                "TASK_PLAN_UPDATED",
                {"plan_hash": "sha256:real"},
                planned.updated_at,
            ),
        )
        completed = planned.transition(Status.COMPLETED, at=NOW + timedelta(seconds=3))
        self.store.save_task(
            completed,
            expected_version=planned.version,
            event=TaskEventDraft(
                "event:forged-plan:4", "TASK_COMPLETED", {}, completed.updated_at
            ),
        )
        forged = replace(planned, plan_hash="sha256:forged")

        with self.assertRaises(ApplicationConflictError):
            await SQLiteLifecycleJournal(self.store).record(
                OrchestrationJournalEntry(
                    task=forged,
                    workflow=None,
                    task_event_type="TASK_PLAN_UPDATED",
                    task_event_payload={"plan_hash": "sha256:real"},
                )
            )

    async def test_queues_active_cancellation_and_rejects_unknown_or_terminal(
        self,
    ) -> None:
        """취소 optimistic version과 terminal 정책이 빠지면 실패한다."""

        active = Task.receive(task_id="cancel-1", input="취소 대상", at=NOW)
        self.store.create_task(
            active,
            event=TaskEventDraft(
                "event:cancel-1:1",
                "TASK_RECEIVED",
                {
                    "request": {
                        "kind": "user_task",
                        "task_id": "cancel-1",
                        "input": "취소 대상",
                    }
                },
                NOW,
            ),
        )
        result = await self.application.cancel(
            "cancel-1",
            CancelCommand(expected_version=1, reason="사용자 요청"),
        )
        await self.application.drain()

        self.assertEqual(result.status, "RECEIVED")
        self.assertEqual(self.orchestrator.cancellations, ["cancel-1"])
        self.assertEqual(self.orchestrator.cancellation_reasons, ["사용자 요청"])
        self.assertEqual(self.store.get_task("cancel-1").status, Status.RECEIVED)
        with self.assertRaises(ApplicationNotFoundError):
            await self.application.cancel("missing", CancelCommand(1))

        with self.assertRaises(ApplicationConflictError):
            await self.application.cancel("cancel-1", CancelCommand(2))

    async def test_startup_finishes_terminal_cancel_command_with_reason_audit(
        self,
    ) -> None:
        """Journal commit 뒤 checkpoint 전 crash가 cancel intent/reason을 잃으면 실패한다."""

        await self.application.stop()
        waiting = self._persist_waiting_task("cancel-crash")
        reason = "운영자 중단 요청"
        self.store.put_runtime_command(
            RuntimeCommandDraft(
                command_id="command:cancel-crash",
                task_id=waiting.task_id,
                command_type=RuntimeCommandType.CANCEL,
                fingerprint="cancel:crash",
                payload={"expected_version": waiting.version, "reason": reason},
                created_at=NOW + timedelta(seconds=4),
            ),
            expected_task=waiting,
        )
        cancelled = waiting.cancel(at=NOW + timedelta(seconds=4))
        self.store.save_task(
            cancelled,
            expected_version=waiting.version,
            event=TaskEventDraft(
                "event:cancel-crash:cancelled",
                "TASK_CANCELLED",
                {"reason": reason},
                cancelled.updated_at,
            ),
        )
        orchestrator = _RecordingOrchestrator()
        self.application = RuntimeApplication(
            store=self.store,
            orchestrator=orchestrator,
            queue_capacity=1,
            worker_count=1,
            clock=lambda: NOW + timedelta(seconds=5),
            id_factory=lambda: "unused",
        )

        await self.application.start()
        await self.application.drain()

        self.assertEqual(orchestrator.cancellations, ["cancel-crash"])
        self.assertEqual(orchestrator.cancellation_reasons, [reason])
        self.assertEqual(self.store.list_pending_runtime_commands(), ())

    def _persist_waiting_task(self, task_id: str) -> Task:
        task = Task.receive(task_id=task_id, input="승인 대상", at=NOW)
        self.store.create_task(
            task,
            event=TaskEventDraft(f"event:{task_id}:1", "TASK_RECEIVED", {}, NOW),
        )
        running = task.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        self.store.save_task(
            running,
            expected_version=task.version,
            event=TaskEventDraft(
                f"event:{task_id}:2", "TASK_STARTED", {}, running.updated_at
            ),
        )
        planned = running.update_plan(
            f"sha256:{task_id}", at=NOW + timedelta(seconds=2)
        )
        self.store.save_task(
            planned,
            expected_version=running.version,
            event=TaskEventDraft(
                f"event:{task_id}:3", "TASK_PLAN_UPDATED", {}, planned.updated_at
            ),
        )
        waiting = planned.transition(
            Status.WAITING_APPROVAL,
            at=NOW + timedelta(seconds=3),
        )
        self.store.save_task(
            waiting,
            expected_version=planned.version,
            event=TaskEventDraft(
                f"event:{task_id}:4", "TASK_WAITING_APPROVAL", {}, waiting.updated_at
            ),
        )
        return waiting


class _BlockingOrchestrator(_RecordingOrchestrator):
    """Queue capacity와 shutdown을 관찰하기 위해 첫 실행을 막는다."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(
        self,
        request: object,
        *,
        thread_id: str,
        initial_task: Task,
    ) -> None:
        await super().start(request, thread_id=thread_id, initial_task=initial_task)
        self.entered.set()
        await self.release.wait()


class RuntimeFullStackTests(unittest.IsolatedAsyncioTestCase):
    """실제 FastAPI, compiled graph와 SQLite authority를 함께 검증한다."""

    async def test_read_only_task_completes_in_background_and_get_reads_sqlite(
        self,
    ) -> None:
        """Handler가 graph를 직접 실행하거나 final 상태만 메모리에 두면 실패한다."""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "full-stack.sqlite3"
            upgrade_database(database_path)
            store = SQLiteStore(database_path)
            checkpointer_context = store.open_checkpointer()
            checkpointer = checkpointer_context.__enter__()
            agent = FakeAgent(
                AgentMetadata(
                    agent_id="operations-agent",
                    name="Operations",
                    description="읽기 전용 조사",
                ),
                output="영향 범위 확인 완료",
            )
            registry = AgentRegistry()
            registry.register(agent)
            classifier = FakeRequestClassifier(
                {
                    RequestKind.USER_TASK: RoutingDecision(
                        request_kind=RequestKind.USER_TASK,
                        agent_id="operations-agent",
                        action=ActionKind.READ_ONLY,
                        reason="read-only inspection",
                    )
                }
            )
            clock = _StepClock()
            ids = iter(("task-full", "workflow-full", "agent-run-full"))
            service = OrchestratorService(
                classifier=classifier,
                governance=FakeGovernance(approved=True, reason="policy allowed"),
                approval_consumer=SQLiteApprovalConsumer(store),
                execution_coordinator=FakeExecutionCoordinator(),
                journal=SQLiteLifecycleJournal(store),
                registry=registry,
                max_agent_runs=2,
                checkpointer=checkpointer,
                clock=clock,
                id_factory=lambda: next(ids),
            )
            application = RuntimeApplication(
                store=store,
                orchestrator=service,
                queue_capacity=4,
                worker_count=1,
                clock=clock,
                id_factory=lambda: next(ids),
            )
            web = create_app(application)
            lifespan = web.router.lifespan_context(web)
            await lifespan.__aenter__()
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web),
                base_url="http://test",
            )
            try:
                accepted = await client.post(
                    "/v1/tasks",
                    json={"input": "장애 영향을 조사해 주세요."},
                )
                self.assertEqual(accepted.status_code, 202)
                self.assertEqual(accepted.json()["status"], "RECEIVED")

                await application.drain()
                response = await client.get("/v1/tasks/task-full")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "COMPLETED")
                self.assertEqual(
                    response.json()["result"],
                    {"output": "영향 범위 확인 완료", "agent_run_count": 1},
                )
                self.assertEqual(len(store.list_task_events("task-full")), 3)
                self.assertEqual(len(agent.received_requests), 1)
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)
                checkpointer_context.__exit__(None, None, None)
                store.close()

    async def test_approval_accept_and_waiting_cancel_are_durable(self) -> None:
        """승인·취소가 queue, SQLite와 checkpoint 중 하나만 바꾸면 실패한다."""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mutating.sqlite3"
            upgrade_database(database_path)
            store = SQLiteStore(database_path)
            checkpointer_context = store.open_checkpointer()
            checkpointer = checkpointer_context.__enter__()
            agent = FakeAgent(
                AgentMetadata("operations-agent", "Operations", "변경 실행"),
                output="변경 완료",
            )
            registry = AgentRegistry()
            registry.register(agent)
            plan = ActionPlan("서비스 재시작", ("상태 확인", "서비스 재시작"))
            classifier = FakeRequestClassifier(
                {
                    RequestKind.USER_TASK: RoutingDecision(
                        RequestKind.USER_TASK,
                        "operations-agent",
                        ActionKind.MUTATING,
                        "restart required",
                        plan,
                    )
                }
            )
            clock = _StepClock()
            service_ids = iter(
                ("workflow-approve", "agent-run-approve", "workflow-cancel")
            )
            service = OrchestratorService(
                classifier=classifier,
                governance=FakeGovernance(approved=True, reason="allowed"),
                approval_consumer=SQLiteApprovalConsumer(store),
                execution_coordinator=FakeExecutionCoordinator(),
                journal=SQLiteLifecycleJournal(store),
                registry=registry,
                max_agent_runs=2,
                checkpointer=checkpointer,
                clock=clock,
                id_factory=lambda: next(service_ids),
            )
            task_ids = iter(("task-approve", "task-cancel"))
            application = RuntimeApplication(
                store=store,
                orchestrator=service,
                queue_capacity=4,
                worker_count=1,
                clock=clock,
                id_factory=lambda: next(task_ids),
            )
            web = create_app(application)
            lifespan = web.router.lifespan_context(web)
            await lifespan.__aenter__()
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web),
                base_url="http://test",
            )
            try:
                await client.post("/v1/tasks", json={"input": "재시작해 주세요."})
                await application.drain()
                waiting = await client.get("/v1/tasks/task-approve")
                waiting_body = waiting.json()
                self.assertEqual(waiting_body["status"], "WAITING_APPROVAL")
                self.assertEqual(
                    waiting_body["approval"]["plan_summary"],
                    "서비스 재시작",
                )

                accepted = await client.post(
                    "/v1/tasks/task-approve/approval",
                    json={
                        "decision_id": "decision-approve",
                        "decision": "approve",
                        "task_version": waiting_body["version"],
                        "plan_hash": waiting_body["approval"]["plan_hash"],
                    },
                )
                self.assertEqual(accepted.status_code, 202)
                await application.drain()
                completed = await client.get("/v1/tasks/task-approve")
                self.assertEqual(completed.json()["status"], "COMPLETED")
                self.assertEqual(len(agent.received_requests), 1)

                await client.post("/v1/tasks", json={"input": "재시작해 주세요."})
                await application.drain()
                waiting_cancel = await client.get("/v1/tasks/task-cancel")
                cancelled = await client.post(
                    "/v1/tasks/task-cancel/cancel",
                    json={"expected_version": waiting_cancel.json()["version"]},
                )
                self.assertEqual(cancelled.status_code, 202)
                await application.drain()
                cancelled_view = await client.get("/v1/tasks/task-cancel")
                self.assertEqual(cancelled_view.json()["status"], "CANCELLED")
                recovered = await service.recover(thread_id="task-cancel")
                self.assertEqual(recovered.task.status, Status.CANCELLED)
                self.assertEqual(len(agent.received_requests), 1)
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)
                checkpointer_context.__exit__(None, None, None)
                store.close()

    async def test_builder_assembles_injected_fakes_and_closes_owned_resources(
        self,
    ) -> None:
        """Composition root가 provider 호출 또는 resource 누수를 만들면 실패한다."""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "builder.sqlite3"
            settings = RuntimeSettings(
                db_path=database_path,
                model_settings=ModelSettings("upstage", "solar", "not-called"),
                max_agent_runs=2,
                queue_capacity=4,
                worker_count=1,
            )
            registry = AgentRegistry()
            registry.register(
                FakeAgent(
                    AgentMetadata("operations-agent", "Operations", "조사"),
                    output="builder 완료",
                )
            )
            classifier = FakeRequestClassifier(
                {
                    RequestKind.USER_TASK: RoutingDecision(
                        RequestKind.USER_TASK,
                        "operations-agent",
                        ActionKind.READ_ONLY,
                        "inspection",
                    )
                }
            )
            ids = iter(("task-builder", "workflow-builder", "agent-run-builder"))
            notification_sender = FakeNotificationSender()
            application = build_runtime(
                settings,
                classifier=classifier,
                governance=FakeGovernance(approved=True, reason="allowed"),
                registry=registry,
                clock=_StepClock(),
                id_factory=lambda: next(ids),
                notification_sender=notification_sender,
            )

            await application.start()
            await application.submit(
                Submission(SubmissionKind.USER_TASK, {"input": "조사"})
            )
            await application.drain()
            await application.stop()
            await application.stop()

            with SQLiteStore(database_path) as reopened:
                task = reopened.get_task("task-builder")
                self.assertIsNotNone(task)
                assert task is not None
                self.assertEqual(task.status, Status.COMPLETED)
            self.assertEqual(
                [notification.status for notification in notification_sender.sent],
                ["COMPLETED"],
            )


class _StepClock:
    """호출마다 단조 증가하는 결정 가능한 UTC clock."""

    def __init__(self) -> None:
        self._next = NOW

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(seconds=1)
        return value


if __name__ == "__main__":
    unittest.main()
