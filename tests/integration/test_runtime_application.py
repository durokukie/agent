"""Runtime application과 bounded background runner의 통합 계약."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from agent_system.agents import AgentMetadata, AgentRegistry, FakeAgent
from agent_system.config import RuntimeSettings
from agent_system.http import create_app
from agent_system.models import ModelSettings
from agent_system.orchestration import (
    ActionKind,
    ActionPlan,
    AlertInput,
    Approval,
    ApprovalConsumeStatus,
    ApprovalResponse,
    FakeExecutionCoordinator,
    FakeGovernance,
    FakeRequestClassifier,
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
    SQLiteStore,
    TaskEventDraft,
    upgrade_database,
)
from agent_system.runtime import (
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

    async def cancel(self, *, thread_id: str) -> None:
        self.cancellations.append(thread_id)


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

    async def test_retries_exact_approval_after_cas_before_graph_command(
        self,
    ) -> None:
        """Approval CAS 뒤 Command 전 crash가 HTTP retry를 영구 차단하면 실패한다."""

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

        replay = await self.application.approve(
            waiting.task_id,
            ApprovalCommand(
                decision_id=response.decision_id,
                decision=ApprovalDecision.APPROVE,
                task_version=waiting.version,
                plan_hash=waiting.plan_hash or "",
            ),
        )
        await self.application.drain()

        self.assertTrue(replay.replayed)
        self.assertEqual(replay.status, Status.RUNNING.value)
        self.assertEqual(self.orchestrator.resumes[-1][1], response)

        rejected = self._persist_waiting_task("rejection-crash")
        rejection = ApprovalResponse.reject(
            decision_id="decision-rejection-crash",
            reason="변경을 승인하지 않습니다.",
        )
        await SQLiteApprovalConsumer(self.store).consume(
            task=rejected,
            response=rejection,
            at=NOW + timedelta(seconds=4),
        )
        rejected_replay = await self.application.approve(
            rejected.task_id,
            ApprovalCommand(
                decision_id=rejection.decision_id,
                decision=ApprovalDecision.REJECT,
                task_version=rejected.version,
                plan_hash=rejected.plan_hash or "",
                reason=rejection.reason,
            ),
        )
        await self.application.drain()

        self.assertTrue(rejected_replay.replayed)
        self.assertEqual(rejected_replay.status, Status.REJECTED.value)
        self.assertEqual(self.orchestrator.resumes[-1][1], rejection)

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

        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual(self.orchestrator.cancellations, [])
        self.assertEqual(self.store.get_task("cancel-1").status, Status.CANCELLED)
        with self.assertRaises(ApplicationNotFoundError):
            await self.application.cancel("missing", CancelCommand(1))

        with self.assertRaises(ApplicationConflictError):
            await self.application.cancel("cancel-1", CancelCommand(2))

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
            application = build_runtime(
                settings,
                classifier=classifier,
                governance=FakeGovernance(approved=True, reason="allowed"),
                registry=registry,
                clock=_StepClock(),
                id_factory=lambda: next(ids),
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
