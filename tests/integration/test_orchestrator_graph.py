"""실제 LangGraph checkpoint와 Command resume 동작을 검증한다."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.checkpoint.memory import InMemorySaver

from agent_system.agents import (
    AgentMetadata,
    AgentOutcome,
    AgentRegistry,
    AgentRequest,
    AgentResult,
    FakeAgent,
)
from agent_system.orchestration import (
    ActionKind,
    ActionPlan,
    Approval,
    ApprovalResponse,
    ApprovalResumeError,
    FailureCode,
    FakeApprovalConsumer,
    FakeGovernance,
    FakeRequestClassifier,
    OrchestrationStartError,
    OrchestratorService,
    RequestKind,
    RoutingDecision,
    Status,
    UserTaskInput,
)
from agent_system.persistence import SQLiteStore, upgrade_database

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class ApprovalResumeTests(unittest.IsolatedAsyncioTestCase):
    """승인 interrupt가 같은 checkpoint thread에서만 재개된다."""

    def make_service(
        self,
        *,
        checkpointer: InMemorySaver | None = None,
        approval_consumer: FakeApprovalConsumer | None = None,
        agent: FakeAgent | None = None,
    ) -> tuple[OrchestratorService, FakeAgent]:
        plan = ActionPlan(summary="서비스를 재시작합니다.", steps=("재시작",))
        classifier = FakeRequestClassifier(
            {
                RequestKind.USER_TASK: RoutingDecision(
                    request_kind=RequestKind.USER_TASK,
                    agent_id="operator",
                    action=ActionKind.MUTATING,
                    reason="복구 필요",
                    plan=plan,
                )
            }
        )
        agent = agent or FakeAgent(
            AgentMetadata("operator", "운영 Agent", "변경 실행"),
            output="재시작 완료",
        )
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=classifier,
            governance=FakeGovernance(approved=True, reason="허용"),
            approval_consumer=approval_consumer or FakeApprovalConsumer(),
            registry=registry,
            max_agent_runs=2,
            checkpointer=checkpointer or InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-approval", "agent-run-1")).__next__,
        )
        return service, agent

    async def test_human_rejection_resumes_to_rejected_terminal_state(self) -> None:
        service, agent = self.make_service()
        waiting = await service.start(
            UserTaskInput(task_id="task-approval", input="서비스를 복구해 주세요."),
            thread_id="thread-approval",
        )
        self.assertIsNotNone(waiting.interrupt)

        result = await service.resume(
            thread_id="thread-approval",
            response=ApprovalResponse.reject(
                decision_id="decision-reject",
                reason="지금은 변경하면 안 됩니다.",
            ),
        )

        self.assertEqual(result.task.status, Status.REJECTED)
        self.assertEqual(result.errors, (FailureCode.HUMAN_REJECTED,))
        self.assertIsNone(result.interrupt)
        self.assertEqual(agent.received_requests, [])

    async def test_recover_keeps_approval_waiting_without_agent_execution(self) -> None:
        service, agent = self.make_service()
        waiting = await service.start(
            UserTaskInput(task_id="task-waiting", input="서비스를 복구해 주세요."),
            thread_id="thread-waiting",
        )

        recovered = await service.recover(thread_id="thread-waiting")

        self.assertEqual(recovered, waiting)
        self.assertEqual(recovered.task.status, Status.WAITING_APPROVAL)
        self.assertEqual(agent.received_requests, [])

    async def test_bound_approval_resumes_and_executes_exactly_once(self) -> None:
        service, agent = self.make_service()
        waiting = await service.start(
            UserTaskInput(task_id="task-approval", input="서비스를 복구해 주세요."),
            thread_id="thread-approval",
        )
        assert waiting.interrupt is not None
        self.assertEqual(waiting.interrupt.plan.steps, ("재시작",))
        self.assertEqual(waiting.interrupt.agent_id, "operator")
        self.assertEqual(waiting.interrupt.action, ActionKind.MUTATING)

        result = await service.resume(
            thread_id="thread-approval",
            response=ApprovalResponse.approve(
                waiting.interrupt, decision_id="decision-approve", at=NOW
            ),
        )

        self.assertEqual(result.task.status, Status.COMPLETED)
        self.assertEqual(result.output, "재시작 완료")
        self.assertEqual(result.workflow.budget.consumed, 1)
        self.assertEqual(len(result.agent_runs), 1)
        self.assertEqual(len(agent.received_requests), 1)
        execution_version = result.task.version - 1
        self.assertEqual(result.workflow.task_version, execution_version)
        self.assertEqual(result.agent_runs[0].task_version, execution_version)
        context = agent.received_requests[0].context
        self.assertEqual(context["approved_plan"], waiting.interrupt.plan.to_snapshot())
        self.assertEqual(context["plan_hash"], waiting.interrupt.plan_hash)
        self.assertEqual(context["approval"]["task_version"], waiting.task.version)
        self.assertEqual(context["routing_action"], ActionKind.MUTATING.value)
        self.assertEqual(context["routing_agent_id"], "operator")
        self.assertEqual(context["approved_task_version"], waiting.task.version)
        self.assertEqual(context["execution_task_version"], execution_version)

    async def test_wrong_stale_and_changed_plan_approvals_are_rejected(self) -> None:
        invalid_cases = (
            (
                FailureCode.APPROVAL_TASK_MISMATCH,
                lambda request: Approval(
                    task_id="another-task",
                    task_version=request.task_version,
                    plan_hash=request.plan_hash,
                    approved_at=NOW,
                ),
            ),
            (
                FailureCode.APPROVAL_STALE,
                lambda request: Approval(
                    task_id=request.task_id,
                    task_version=request.task_version - 1,
                    plan_hash=request.plan_hash,
                    approved_at=NOW,
                ),
            ),
            (
                FailureCode.APPROVAL_PLAN_CHANGED,
                lambda request: Approval(
                    task_id=request.task_id,
                    task_version=request.task_version,
                    plan_hash="sha256:changed-plan",
                    approved_at=NOW,
                ),
            ),
        )
        for index, (expected_error, make_approval) in enumerate(invalid_cases):
            with self.subTest(expected_error=expected_error):
                service, agent = self.make_service()
                thread_id = f"thread-invalid-{index}"
                waiting = await service.start(
                    UserTaskInput(
                        task_id="task-approval",
                        input="서비스를 복구해 주세요.",
                    ),
                    thread_id=thread_id,
                )
                assert waiting.interrupt is not None

                result = await service.resume(
                    thread_id=thread_id,
                    response=ApprovalResponse(
                        decision_id=f"decision-invalid-{index}",
                        accepted=True,
                        approval=make_approval(waiting.interrupt),
                    ),
                )

                self.assertEqual(result.task.status, Status.REJECTED)
                self.assertEqual(result.errors, (expected_error,))
                self.assertEqual(agent.received_requests, [])

    async def test_terminal_approval_cannot_be_replayed(self) -> None:
        service, agent = self.make_service()
        waiting = await service.start(
            UserTaskInput(task_id="task-approval", input="서비스를 복구해 주세요."),
            thread_id="thread-approval",
        )
        assert waiting.interrupt is not None
        response = ApprovalResponse.approve(
            waiting.interrupt, decision_id="decision-replay", at=NOW
        )
        await service.resume(thread_id="thread-approval", response=response)

        with self.assertRaises(ApprovalResumeError):
            await service.resume(thread_id="thread-approval", response=response)

        self.assertEqual(len(agent.received_requests), 1)

    async def test_concurrent_approval_resume_executes_the_agent_once(self) -> None:
        checkpointer = InMemorySaver()
        consumer = FakeApprovalConsumer()
        first_service, agent = self.make_service(
            checkpointer=checkpointer,
            approval_consumer=consumer,
        )
        second_service, _ = self.make_service(
            checkpointer=checkpointer,
            approval_consumer=consumer,
            agent=agent,
        )
        waiting = await first_service.start(
            UserTaskInput(task_id="task-approval", input="서비스를 복구해 주세요."),
            thread_id="thread-approval",
        )
        assert waiting.interrupt is not None
        response = ApprovalResponse.approve(
            waiting.interrupt, decision_id="decision-concurrent", at=NOW
        )

        results = await asyncio.gather(
            first_service.resume(thread_id="thread-approval", response=response),
            second_service.resume(thread_id="thread-approval", response=response),
            return_exceptions=True,
        )

        self.assertEqual(
            sum(
                not isinstance(result, BaseException)
                and result.task.status is Status.COMPLETED
                for result in results
            ),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, ApprovalResumeError) for result in results),
            1,
        )
        self.assertEqual(len(agent.received_requests), 1)

    async def test_existing_checkpoint_thread_cannot_start_another_task(self) -> None:
        service, _ = self.make_service()
        await service.start(
            UserTaskInput(task_id="task-approval", input="서비스를 복구해 주세요."),
            thread_id="thread-approval",
        )

        with self.assertRaises(OrchestrationStartError):
            await service.start(
                UserTaskInput(task_id="another-task", input="다른 작업입니다."),
                thread_id="thread-approval",
            )


class _BlockingAgent:
    metadata = AgentMetadata("blocking", "대기 Agent", "호출 중 checkpoint 검증")

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request: AgentRequest) -> AgentResult:
        self.started.set()
        await self.release.wait()
        return AgentResult(
            agent_id=self.metadata.agent_id,
            outcome=AgentOutcome.SUCCESS,
            output="완료",
        )


class _RecoverableAgent:
    metadata = AgentMetadata("recoverable", "복구 Agent", "idempotent 실행")

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.agent_run_ids: list[str] = []
        self.effects = 0

    async def run(self, request: AgentRequest) -> AgentResult:
        self.agent_run_ids.append(str(request.context["agent_run_id"]))
        self.started.set()
        await self.release.wait()
        self.effects += 1
        return AgentResult(self.metadata.agent_id, AgentOutcome.SUCCESS, "복구 완료")


class AgentIssuanceCheckpointTests(unittest.IsolatedAsyncioTestCase):
    """외부 Agent 호출보다 budget issuance checkpoint가 먼저 기록된다."""

    async def test_open_agent_run_is_checkpointed_before_async_agent_call(self) -> None:
        agent = _BlockingAgent()
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=FakeRequestClassifier(
                {
                    RequestKind.USER_TASK: RoutingDecision(
                        request_kind=RequestKind.USER_TASK,
                        agent_id="blocking",
                        action=ActionKind.READ_ONLY,
                        reason="checkpoint 검증",
                    )
                }
            ),
            governance=FakeGovernance(approved=True, reason="허용"),
            approval_consumer=FakeApprovalConsumer(),
            registry=registry,
            max_agent_runs=1,
            checkpointer=InMemorySaver(),
            clock=lambda: NOW,
            id_factory=iter(("workflow-checkpoint", "agent-run-checkpoint")).__next__,
        )

        running = asyncio.create_task(
            service.start(
                UserTaskInput(task_id="task-checkpoint", input="상태 확인"),
                thread_id="thread-checkpoint",
            )
        )
        await agent.started.wait()

        checkpointed = await service.get_result(thread_id="thread-checkpoint")

        self.assertEqual(checkpointed.workflow.budget.consumed, 1)
        self.assertEqual(len(checkpointed.agent_runs), 1)
        self.assertFalse(checkpointed.agent_runs[0].is_completed)
        agent.release.set()
        completed = await running
        self.assertEqual(completed.task.status, Status.COMPLETED)


class SQLiteCheckpointerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """Persistence 공개 checkpointer를 async supervisor에 그대로 주입한다."""

    async def test_async_service_runs_and_reads_after_sync_saver_reconnect(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "orchestrator.db"
            upgrade_database(database_path)
            registry = AgentRegistry()
            registry.register(
                FakeAgent(
                    AgentMetadata("reader", "조회 Agent", "상태 조회"),
                    output="정상",
                )
            )
            classifier = FakeRequestClassifier(
                {
                    RequestKind.USER_TASK: RoutingDecision(
                        request_kind=RequestKind.USER_TASK,
                        agent_id="reader",
                        action=ActionKind.READ_ONLY,
                        reason="상태 조회",
                    )
                }
            )
            with SQLiteStore(database_path) as store:
                with store.open_checkpointer() as saver:
                    service = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        registry=registry,
                        max_agent_runs=1,
                        checkpointer=saver,
                        clock=lambda: NOW,
                        id_factory=iter(
                            ("workflow-sqlite", "agent-run-sqlite")
                        ).__next__,
                    )
                    completed = await service.start(
                        UserTaskInput(task_id="task-sqlite", input="상태 확인"),
                        thread_id="thread-sqlite",
                    )
                    self.assertEqual(completed.task.status, Status.COMPLETED)

                with store.open_checkpointer() as reopened_saver:
                    recovered_service = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        registry=registry,
                        max_agent_runs=1,
                        checkpointer=reopened_saver,
                        clock=lambda: NOW,
                        id_factory=iter(("unused-recovery-id",)).__next__,
                    )
                    recovered = await recovered_service.recover(
                        thread_id="thread-sqlite"
                    )
                    self.assertEqual(recovered, completed)

    async def test_recovers_open_issuance_without_another_budget_slot(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "recovery.db"
            upgrade_database(database_path)
            agent = _RecoverableAgent()
            registry = AgentRegistry()
            registry.register(agent)
            classifier = FakeRequestClassifier(
                {
                    RequestKind.USER_TASK: RoutingDecision(
                        RequestKind.USER_TASK,
                        "recoverable",
                        ActionKind.READ_ONLY,
                        "복구",
                    )
                }
            )
            with SQLiteStore(database_path) as store:
                with store.open_checkpointer() as saver:
                    service = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=saver,
                        clock=lambda: NOW,
                        id_factory=iter(
                            ("workflow-recover", "agent-run-recover")
                        ).__next__,
                    )
                    running = asyncio.create_task(
                        service.start(
                            UserTaskInput("task-recover", "복구"),
                            thread_id="thread-recover",
                        )
                    )
                    await agent.started.wait()
                    running.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await running
                agent.release.set()
                with store.open_checkpointer() as reopened:
                    recovered_service = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=reopened,
                        clock=lambda: NOW,
                        id_factory=iter(("unused",)).__next__,
                    )
                    result = await recovered_service.recover(thread_id="thread-recover")

            self.assertEqual(result.task.status, Status.COMPLETED)
            self.assertEqual(result.workflow.budget.consumed, 1)
            self.assertEqual(len(result.agent_runs), 1)
            self.assertEqual(set(agent.agent_run_ids), {"agent-run-recover"})
            self.assertEqual(agent.effects, 1)
