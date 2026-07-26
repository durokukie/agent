"""Durable orchestration journal과 취소 facade의 실제 graph 동작을 검증한다."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from inspect import Parameter, signature

from langgraph.checkpoint.memory import InMemorySaver

from agent_system import orchestration
from agent_system.agents import (
    AgentMetadata,
    AgentOutcome,
    AgentRegistry,
    AgentRequest,
    AgentResult,
    FakeAgent,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class _AuthoritativeIssuanceJournal:
    """Checkpoint 전 crash 뒤 이미 저장된 issuance replay를 재현한다."""

    def __init__(self) -> None:
        self.entries: list[orchestration.OrchestrationJournalEntry] = []
        self._executing: orchestration.WorkflowRun | None = None

    async def record(
        self, entry: orchestration.OrchestrationJournalEntry
    ) -> orchestration.OrchestrationJournalEntry:
        if (
            entry.workflow is not None
            and entry.workflow.phase is orchestration.Phase.EXECUTING
            and entry.agent_run is None
        ):
            self._executing = entry.workflow
        if entry.agent_run is not None and not entry.agent_run.is_completed:
            assert self._executing is not None
            workflow, agent_run = self._executing.begin_agent_run(
                agent_run_id="persisted-agent-run",
                agent_id=entry.agent_run.agent_id,
                at=entry.agent_run.started_at,
            )
            entry = orchestration.OrchestrationJournalEntry(
                task=entry.task,
                workflow=workflow,
                agent_run=agent_run,
            )
        self.entries.append(entry)
        return entry


class _AuthoritativePhaseJournal(orchestration.RecordingOrchestrationJournal):
    """동일 phase callback의 먼저 저장된 시각을 authoritative하게 반환한다."""

    async def record(
        self, entry: orchestration.OrchestrationJournalEntry
    ) -> orchestration.OrchestrationJournalEntry:
        if (
            entry.workflow is not None
            and entry.workflow.phase is orchestration.Phase.ANALYZING
        ):
            entry = orchestration.OrchestrationJournalEntry(
                task=entry.task,
                workflow=replace(
                    entry.workflow,
                    updated_at=entry.workflow.started_at,
                ),
            )
        return await super().record(entry)


class _AuthoritativeCompletionJournal(orchestration.RecordingOrchestrationJournal):
    """AgentRun completion callback의 먼저 저장된 완료 시각을 반환한다."""

    async def record(
        self, entry: orchestration.OrchestrationJournalEntry
    ) -> orchestration.OrchestrationJournalEntry:
        if entry.agent_run is not None and entry.agent_run.is_completed:
            assert entry.workflow is not None
            snapshot = entry.agent_run.to_snapshot()
            snapshot["completed_at"] = entry.agent_run.started_at.isoformat()
            entry = orchestration.OrchestrationJournalEntry(
                task=entry.task,
                workflow=entry.workflow,
                agent_run=orchestration.AgentRun.from_snapshot(
                    snapshot,
                    workflow=entry.workflow,
                ),
            )
        return await super().record(entry)


class _RecordingCoordinator(orchestration.FakeExecutionCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.claims: list[tuple[str, str]] = []
        self.releases: list[tuple[str, str]] = []

    async def claim(
        self, *, thread_id: str, claim_key: str
    ) -> orchestration.ExecutionClaimResult:
        self.claims.append((thread_id, claim_key))
        return await super().claim(thread_id=thread_id, claim_key=claim_key)

    async def release(self, *, thread_id: str, claim_key: str) -> None:
        self.releases.append((thread_id, claim_key))
        await super().release(thread_id=thread_id, claim_key=claim_key)


class _RaisingClassifier:
    async def classify(
        self, request: orchestration.OrchestratorInput
    ) -> orchestration.RoutingDecision:
        raise RuntimeError("classifier-secret")


class _BlockingAgent:
    metadata = AgentMetadata("blocking", "대기 Agent", "취소 경합 검증")

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def run(self, request: AgentRequest) -> AgentResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return AgentResult(
            self.metadata.agent_id,
            AgentOutcome.SUCCESS,
            "완료",
        )


class _RaisingJournal:
    async def record(
        self, entry: orchestration.OrchestrationJournalEntry
    ) -> orchestration.OrchestrationJournalEntry:
        raise RuntimeError("journal-secret")


class _CancelledJournal:
    async def record(
        self, entry: orchestration.OrchestrationJournalEntry
    ) -> orchestration.OrchestrationJournalEntry:
        raise asyncio.CancelledError


class OrchestrationJournalContractTests(unittest.IsolatedAsyncioTestCase):
    """Journal 공개 값과 fake가 immutable aggregate entry를 보존한다."""

    async def test_recording_journal_returns_and_records_the_exact_entry(self) -> None:
        self.assertTrue(
            hasattr(orchestration, "OrchestrationJournalEntry"),
            "orchestration public interface에 journal entry가 필요합니다.",
        )
        entry_type = orchestration.OrchestrationJournalEntry
        journal_type = orchestration.RecordingOrchestrationJournal
        task = orchestration.Task.receive(
            task_id="task-journal-contract",
            input="상태 확인",
            at=NOW,
        )
        entry = entry_type(
            task=task,
            workflow=None,
            task_event_type="TASK_RECEIVED",
            task_event_payload={"source": "test"},
        )
        journal = journal_type()

        returned = await journal.record(entry)

        self.assertIs(returned, entry)
        self.assertEqual(journal.entries, [entry])
        with self.assertRaises(TypeError):
            entry.task_event_payload["source"] = "changed"

    def test_service_requires_an_explicit_journal_dependency(self) -> None:
        parameters = signature(orchestration.OrchestratorService).parameters

        self.assertIn("journal", parameters)
        self.assertIs(parameters["journal"].default, Parameter.empty)

    def test_start_accepts_an_optional_pre_persisted_received_task(self) -> None:
        parameters = signature(orchestration.OrchestratorService.start).parameters

        self.assertIn("initial_task", parameters)
        self.assertIsNone(parameters["initial_task"].default)

    def test_service_exposes_public_checkpoint_cancellation(self) -> None:
        self.assertTrue(hasattr(orchestration, "OrchestrationCancellationError"))
        self.assertTrue(hasattr(orchestration.OrchestratorService, "cancel"))


class OrchestrationJournalGraphTests(unittest.IsolatedAsyncioTestCase):
    """실제 compiled graph가 모든 aggregate 변경을 순서대로 기록한다."""

    async def test_read_only_run_records_every_task_version_phase_and_agent_run(
        self,
    ) -> None:
        journal = orchestration.RecordingOrchestrationJournal()
        registry = AgentRegistry()
        registry.register(
            FakeAgent(
                AgentMetadata("reader", "조회 Agent", "상태 조회"),
                output="정상",
            )
        )
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "reader",
                        orchestration.ActionKind.READ_ONLY,
                        "상태 조회",
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-journal", "agent-run-journal")).__next__,
        )

        result = await service.start(
            orchestration.UserTaskInput("task-journal", "상태 확인"),
            thread_id="thread-journal",
        )

        self.assertEqual(result.task.status, orchestration.Status.COMPLETED)
        self.assertEqual(
            [
                (entry.task_event_type, entry.task.version)
                for entry in journal.entries
                if entry.task_event_type is not None
            ],
            [
                ("TASK_RECEIVED", 1),
                ("TASK_STARTED", 2),
                ("TASK_COMPLETED", 3),
            ],
        )
        self.assertEqual(
            [
                entry.workflow.phase
                for entry in journal.entries
                if entry.workflow is not None
            ],
            [
                orchestration.Phase.CLASSIFYING,
                orchestration.Phase.ANALYZING,
                orchestration.Phase.PLANNING,
                orchestration.Phase.GOVERNING,
                orchestration.Phase.EXECUTING,
                orchestration.Phase.EXECUTING,
                orchestration.Phase.VERIFYING,
                orchestration.Phase.VERIFYING,
            ],
        )
        self.assertEqual(
            [
                entry.agent_run.is_completed
                for entry in journal.entries
                if entry.agent_run
            ],
            [False, True],
        )

    async def test_start_uses_the_exact_pre_persisted_received_task(self) -> None:
        initial_task = orchestration.Task.receive(
            task_id="task-initial",
            input="상태 확인",
            at=NOW - timedelta(minutes=5),
        )
        journal = orchestration.RecordingOrchestrationJournal()
        registry = AgentRegistry()
        registry.register(FakeAgent(AgentMetadata("reader", "조회", "상태 조회")))
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "reader",
                        orchestration.ActionKind.READ_ONLY,
                        "상태 조회",
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-initial", "agent-run-initial")).__next__,
        )

        result = await service.start(
            orchestration.UserTaskInput("task-initial", "상태 확인"),
            thread_id="thread-initial",
            initial_task=initial_task,
        )

        self.assertEqual(journal.entries[0].task, initial_task)
        self.assertEqual(
            journal.entries[0].task_event_payload,
            {
                "request": {
                    "kind": "user_task",
                    "task_id": "task-initial",
                    "input": "상태 확인",
                }
            },
        )
        self.assertEqual(result.task.created_at, initial_task.created_at)

    async def test_mutating_run_records_plan_wait_approval_and_terminal_versions(
        self,
    ) -> None:
        plan = orchestration.ActionPlan("서비스 재시작", ("재시작",))
        journal = orchestration.RecordingOrchestrationJournal()
        registry = AgentRegistry()
        registry.register(
            FakeAgent(AgentMetadata("operator", "운영 Agent", "변경 실행"))
        )
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "operator",
                        orchestration.ActionKind.MUTATING,
                        "복구 필요",
                        plan,
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-mutating", "agent-run-mutating")).__next__,
        )
        waiting = await service.start(
            orchestration.UserTaskInput("task-mutating", "서비스 복구"),
            thread_id="thread-mutating",
        )
        assert waiting.interrupt is not None
        waiting_entry = next(
            entry
            for entry in journal.entries
            if entry.task_event_type == "TASK_WAITING_APPROVAL"
        )
        self.assertEqual(
            waiting_entry.task_event_payload,
            {"approval_request": waiting.interrupt.to_snapshot()},
        )

        completed = await service.resume(
            thread_id="thread-mutating",
            response=orchestration.ApprovalResponse.approve(
                waiting.interrupt,
                decision_id="decision-mutating",
                at=NOW,
            ),
        )

        self.assertEqual(completed.task.status, orchestration.Status.COMPLETED)
        self.assertEqual(
            [
                (entry.task_event_type, entry.task.version)
                for entry in journal.entries
                if entry.task_event_type is not None
            ],
            [
                ("TASK_RECEIVED", 1),
                ("TASK_STARTED", 2),
                ("TASK_PLAN_UPDATED", 3),
                ("TASK_WAITING_APPROVAL", 4),
                ("TASK_APPROVED", 5),
                ("TASK_COMPLETED", 6),
            ],
        )
        approved_entry = next(
            entry
            for entry in journal.entries
            if entry.task_event_type == "TASK_APPROVED"
        )
        self.assertEqual(
            approved_entry.task_event_payload,
            {"decision_id": "decision-mutating", "accepted": True, "errors": []},
        )
        completed_entry = journal.entries[-1]
        self.assertEqual(completed_entry.task_event_type, "TASK_COMPLETED")
        self.assertEqual(
            completed_entry.task_event_payload,
            {"output": completed.output, "errors": []},
        )

    async def test_issuance_replay_uses_the_authoritative_persisted_agent_run(
        self,
    ) -> None:
        journal = _AuthoritativeIssuanceJournal()
        agent = FakeAgent(AgentMetadata("reader", "조회", "상태 조회"))
        registry = AgentRegistry()
        registry.register(agent)
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "reader",
                        orchestration.ActionKind.READ_ONLY,
                        "상태 조회",
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-replay", "candidate-agent-run")).__next__,
        )

        try:
            result = await service.start(
                orchestration.UserTaskInput("task-replay", "상태 조회"),
                thread_id="thread-replay",
            )
        except orchestration.OrchestrationDependencyError as error:
            self.fail(f"authoritative issuance replay가 거부되었습니다: {error}")

        self.assertEqual(result.agent_runs[0].agent_run_id, "persisted-agent-run")
        self.assertEqual(
            agent.received_requests[0].idempotency_key,
            "persisted-agent-run",
        )

    async def test_non_issuance_replay_uses_authoritative_persisted_clock(
        self,
    ) -> None:
        """Checkpoint 전 phase callback replay가 새 clock 때문에 실패하면 안 된다."""

        journal = _AuthoritativePhaseJournal()
        registry = AgentRegistry()
        registry.register(FakeAgent(AgentMetadata("reader", "조회", "상태 조회")))
        moments = iter(NOW + timedelta(seconds=index) for index in range(20))
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "reader",
                        orchestration.ActionKind.READ_ONLY,
                        "상태 조회",
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: next(moments),
            id_factory=iter(("workflow-clock", "agent-run-clock")).__next__,
        )

        result = await service.start(
            orchestration.UserTaskInput("task-clock", "상태 조회"),
            thread_id="thread-clock",
        )

        self.assertEqual(result.task.status, orchestration.Status.COMPLETED)
        analyzing = next(
            entry
            for entry in journal.entries
            if entry.workflow is not None
            and entry.workflow.phase is orchestration.Phase.ANALYZING
        )
        assert analyzing.workflow is not None
        self.assertEqual(
            analyzing.workflow.updated_at,
            analyzing.workflow.started_at,
        )

    async def test_agent_completion_replay_uses_authoritative_persisted_clock(
        self,
    ) -> None:
        """AgentRun completion 뒤 checkpoint 전 crash의 clock replay를 허용한다."""

        journal = _AuthoritativeCompletionJournal()
        registry = AgentRegistry()
        registry.register(FakeAgent(AgentMetadata("reader", "조회", "상태 조회")))
        moments = iter(NOW + timedelta(seconds=index) for index in range(20))
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "reader",
                        orchestration.ActionKind.READ_ONLY,
                        "상태 조회",
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: next(moments),
            id_factory=iter(("workflow-completion", "agent-run-completion")).__next__,
        )

        result = await service.start(
            orchestration.UserTaskInput("task-completion", "상태 조회"),
            thread_id="thread-completion",
        )

        self.assertEqual(result.task.status, orchestration.Status.COMPLETED)
        self.assertEqual(
            result.agent_runs[0].completed_at,
            result.agent_runs[0].started_at,
        )

    async def test_classifier_failure_records_terminal_task_and_stable_errors(
        self,
    ) -> None:
        journal = orchestration.RecordingOrchestrationJournal()
        service = orchestration.OrchestratorService(
            classifier=_RaisingClassifier(),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=AgentRegistry(),
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-classifier-error",)).__next__,
        )

        result = await service.start(
            orchestration.UserTaskInput("task-classifier-error", "상태 조회"),
            thread_id="thread-classifier-error",
        )

        self.assertEqual(result.task.status, orchestration.Status.FAILED)
        terminal = journal.entries[-1]
        self.assertEqual(terminal.task, result.task)
        self.assertEqual(terminal.task_event_type, "TASK_FAILED")
        self.assertEqual(
            terminal.task_event_payload,
            {"errors": [orchestration.FailureCode.CLASSIFICATION_FAILED.value]},
        )

    async def test_retry_exhaustion_records_escalated_task_with_all_errors(
        self,
    ) -> None:
        journal = orchestration.RecordingOrchestrationJournal()
        registry = AgentRegistry()
        registry.register(
            FakeAgent(
                AgentMetadata("reader", "조회", "상태 조회"),
                outcome=AgentOutcome.FAILURE,
            )
        )
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "reader",
                        orchestration.ActionKind.READ_ONLY,
                        "상태 조회",
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-escalate", "agent-run-escalate")).__next__,
        )

        result = await service.start(
            orchestration.UserTaskInput("task-escalate", "상태 조회"),
            thread_id="thread-escalate",
        )

        self.assertEqual(result.task.status, orchestration.Status.ESCALATED)
        terminal = journal.entries[-1]
        self.assertEqual(terminal.task, result.task)
        self.assertEqual(terminal.task_event_type, "TASK_ESCALATED")
        self.assertEqual(
            terminal.task_event_payload,
            {
                "errors": [
                    orchestration.FailureCode.AGENT_FAILURE.value,
                    orchestration.FailureCode.RETRY_EXHAUSTED.value,
                ]
            },
        )

    async def test_governance_rejection_records_stable_errors_payload(self) -> None:
        plan = orchestration.ActionPlan("설정 변경", ("변경",))
        journal = orchestration.RecordingOrchestrationJournal()
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "operator",
                        orchestration.ActionKind.MUTATING,
                        "설정 변경",
                        plan,
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=False, reason="거부"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=AgentRegistry(),
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-governance-reject",)).__next__,
        )

        result = await service.start(
            orchestration.UserTaskInput("task-governance-reject", "설정 변경"),
            thread_id="thread-governance-reject",
        )

        self.assertEqual(result.task.status, orchestration.Status.REJECTED)
        self.assertEqual(
            journal.entries[-1].task_event_payload,
            {"errors": [orchestration.FailureCode.GOVERNANCE_REJECTED.value]},
        )

    async def test_journal_exception_is_stable_cause_free_dependency_error(
        self,
    ) -> None:
        coordinator = _RecordingCoordinator()
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier({}),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=coordinator,
            journal=_RaisingJournal(),
            registry=AgentRegistry(),
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("unused",)).__next__,
        )

        with self.assertRaises(orchestration.OrchestrationDependencyError) as raised:
            await service.start(
                orchestration.UserTaskInput("task-journal-error", "상태 조회"),
                thread_id="thread-journal-error",
            )

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("journal-secret", str(raised.exception))
        self.assertEqual(coordinator.releases, coordinator.claims)

    async def test_journal_cancellation_propagates_and_releases_thread_claim(
        self,
    ) -> None:
        coordinator = _RecordingCoordinator()
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier({}),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=coordinator,
            journal=_CancelledJournal(),
            registry=AgentRegistry(),
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("unused",)).__next__,
        )

        with self.assertRaises(asyncio.CancelledError):
            await service.start(
                orchestration.UserTaskInput("task-journal-cancel", "상태 조회"),
                thread_id="thread-journal-cancel",
            )

        self.assertEqual(coordinator.releases, coordinator.claims)

    async def test_human_rejection_records_decision_and_stable_errors(self) -> None:
        plan = orchestration.ActionPlan("서비스 재시작", ("재시작",))
        journal = orchestration.RecordingOrchestrationJournal()
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "operator",
                        orchestration.ActionKind.MUTATING,
                        "복구",
                        plan,
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=orchestration.FakeExecutionCoordinator(),
            journal=journal,
            registry=AgentRegistry(),
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-human-reject",)).__next__,
        )
        await service.start(
            orchestration.UserTaskInput("task-human-reject", "서비스 복구"),
            thread_id="thread-human-reject",
        )

        result = await service.resume(
            thread_id="thread-human-reject",
            response=orchestration.ApprovalResponse.reject(
                decision_id="decision-human-reject",
                reason="지금은 실행하지 않음",
            ),
        )

        self.assertEqual(result.task.status, orchestration.Status.REJECTED)
        self.assertEqual(
            journal.entries[-1].task_event_payload,
            {
                "decision_id": "decision-human-reject",
                "accepted": False,
                "errors": [orchestration.FailureCode.HUMAN_REJECTED.value],
            },
        )


class OrchestrationCancellationTests(unittest.IsolatedAsyncioTestCase):
    """Facade 취소가 app journal과 LangGraph checkpoint를 함께 terminal로 만든다."""

    async def test_waiting_task_cancellation_is_journaled_and_terminal_checkpointed(
        self,
    ) -> None:
        plan = orchestration.ActionPlan("서비스 재시작", ("재시작",))
        journal = orchestration.RecordingOrchestrationJournal()
        coordinator = _RecordingCoordinator()
        saver = InMemorySaver()
        registry = AgentRegistry()
        registry.register(FakeAgent(AgentMetadata("operator", "운영", "변경")))
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "operator",
                        orchestration.ActionKind.MUTATING,
                        "복구",
                        plan,
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=coordinator,
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=saver,
            clock=lambda: NOW,
            id_factory=iter(("workflow-cancel",)).__next__,
        )
        waiting = await service.start(
            orchestration.UserTaskInput("task-cancel", "서비스 복구"),
            thread_id="thread-cancel",
        )
        entries_before_cancel = len(journal.entries)

        try:
            cancelled = await service.cancel(
                thread_id="thread-cancel",
                reason="운영자 중단 요청",
            )
        except NotImplementedError:
            self.fail("OrchestratorService.cancel이 구현되지 않았습니다.")

        self.assertEqual(cancelled.task.status, orchestration.Status.CANCELLED)
        self.assertEqual(cancelled.task.version, waiting.task.version + 1)
        self.assertEqual(len(journal.entries), entries_before_cancel + 1)
        self.assertEqual(journal.entries[-1].task_event_type, "TASK_CANCELLED")
        self.assertEqual(journal.entries[-1].task, cancelled.task)
        self.assertEqual(
            journal.entries[-1].task_event_payload,
            {"reason": "운영자 중단 요청", "errors": []},
        )
        snapshot = await service._graph.aget_state(
            {"configurable": {"thread_id": "thread-cancel"}}
        )
        self.assertEqual(snapshot.next, ())
        self.assertEqual(await service.get_result(thread_id="thread-cancel"), cancelled)

        recovered = await service.recover(thread_id="thread-cancel")

        self.assertEqual(recovered, cancelled)
        self.assertEqual(len(journal.entries), entries_before_cancel + 1)
        self.assertEqual(
            coordinator.claims,
            [
                ("thread-cancel", "thread:thread-cancel"),
                ("thread-cancel", "thread:thread-cancel"),
            ],
        )
        self.assertEqual(coordinator.releases, coordinator.claims)

    async def test_open_agent_run_can_be_cancelled_after_runner_cancellation(
        self,
    ) -> None:
        journal = orchestration.RecordingOrchestrationJournal()
        coordinator = _RecordingCoordinator()
        agent = _BlockingAgent()
        registry = AgentRegistry()
        registry.register(agent)
        service = orchestration.OrchestratorService(
            classifier=orchestration.FakeRequestClassifier(
                {
                    orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                        orchestration.RequestKind.USER_TASK,
                        "blocking",
                        orchestration.ActionKind.READ_ONLY,
                        "상태 조회",
                    )
                }
            ),
            governance=orchestration.FakeGovernance(approved=True, reason="허용"),
            approval_consumer=orchestration.FakeApprovalConsumer(),
            execution_coordinator=coordinator,
            journal=journal,
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-open-cancel", "agent-run-open-cancel")).__next__,
        )
        running = asyncio.create_task(
            service.start(
                orchestration.UserTaskInput("task-open-cancel", "상태 조회"),
                thread_id="thread-open-cancel",
            )
        )
        await agent.started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

        cancelled = await service.cancel(thread_id="thread-open-cancel")

        self.assertEqual(cancelled.task.status, orchestration.Status.CANCELLED)
        self.assertEqual(len(cancelled.agent_runs), 1)
        self.assertFalse(cancelled.agent_runs[0].is_completed)
        self.assertEqual(agent.calls, 1)
        self.assertEqual(
            await service.recover(thread_id="thread-open-cancel"),
            cancelled,
        )
        self.assertEqual(coordinator.releases, coordinator.claims)

    async def test_active_runner_and_cancellation_are_serialized_by_thread_claim(
        self,
    ) -> None:
        journal = orchestration.RecordingOrchestrationJournal()
        coordinator = _RecordingCoordinator()
        saver = InMemorySaver()
        agent = _BlockingAgent()
        registry = AgentRegistry()
        registry.register(agent)
        classifier = orchestration.FakeRequestClassifier(
            {
                orchestration.RequestKind.USER_TASK: orchestration.RoutingDecision(
                    orchestration.RequestKind.USER_TASK,
                    "blocking",
                    orchestration.ActionKind.READ_ONLY,
                    "상태 조회",
                )
            }
        )

        def make_service(ids: tuple[str, ...]) -> orchestration.OrchestratorService:
            return orchestration.OrchestratorService(
                classifier=classifier,
                governance=orchestration.FakeGovernance(approved=True, reason="허용"),
                approval_consumer=orchestration.FakeApprovalConsumer(),
                execution_coordinator=coordinator,
                journal=journal,
                registry=registry,
                max_agent_runs=1,
                checkpointer=saver,
                clock=lambda: NOW,
                id_factory=iter(ids).__next__,
            )

        starter = make_service(("workflow-race", "agent-run-race"))
        canceller = make_service(("unused",))
        running = asyncio.create_task(
            starter.start(
                orchestration.UserTaskInput("task-cancel-race", "상태 조회"),
                thread_id="thread-cancel-race",
            )
        )
        await agent.started.wait()

        with self.assertRaises(orchestration.OrchestrationCancellationError):
            await canceller.cancel(thread_id="thread-cancel-race")

        self.assertFalse(
            any(entry.task_event_type == "TASK_CANCELLED" for entry in journal.entries)
        )
        agent.release.set()
        completed = await running
        self.assertEqual(completed.task.status, orchestration.Status.COMPLETED)
        self.assertEqual(agent.calls, 1)


if __name__ == "__main__":
    unittest.main()
