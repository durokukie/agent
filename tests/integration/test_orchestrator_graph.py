"""실제 LangGraph checkpoint와 Command resume 동작을 검증한다."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

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
    FakeGovernance,
    FakeRequestClassifier,
    OrchestrationStartError,
    OrchestratorService,
    RequestKind,
    RoutingDecision,
    Status,
    UserTaskInput,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class ApprovalResumeTests(unittest.IsolatedAsyncioTestCase):
    """승인 interrupt가 같은 checkpoint thread에서만 재개된다."""

    def make_service(self) -> tuple[OrchestratorService, FakeAgent]:
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
        agent = FakeAgent(
            AgentMetadata("operator", "운영 Agent", "변경 실행"),
            output="재시작 완료",
        )
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=classifier,
            governance=FakeGovernance(approved=True, reason="허용"),
            registry=registry,
            max_agent_runs=2,
            checkpointer=InMemorySaver(),
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
            response=ApprovalResponse.reject(reason="지금은 변경하면 안 됩니다."),
        )

        self.assertEqual(result.task.status, Status.REJECTED)
        self.assertEqual(result.errors, (FailureCode.HUMAN_REJECTED,))
        self.assertIsNone(result.interrupt)
        self.assertEqual(agent.received_requests, [])

    async def test_bound_approval_resumes_and_executes_exactly_once(self) -> None:
        service, agent = self.make_service()
        waiting = await service.start(
            UserTaskInput(task_id="task-approval", input="서비스를 복구해 주세요."),
            thread_id="thread-approval",
        )
        assert waiting.interrupt is not None

        result = await service.resume(
            thread_id="thread-approval",
            response=ApprovalResponse.approve(waiting.interrupt, at=NOW),
        )

        self.assertEqual(result.task.status, Status.COMPLETED)
        self.assertEqual(result.output, "재시작 완료")
        self.assertEqual(result.workflow.budget.consumed, 1)
        self.assertEqual(len(result.agent_runs), 1)
        self.assertEqual(len(agent.received_requests), 1)

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
        response = ApprovalResponse.approve(waiting.interrupt, at=NOW)
        await service.resume(thread_id="thread-approval", response=response)

        with self.assertRaises(ApprovalResumeError):
            await service.resume(thread_id="thread-approval", response=response)

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
