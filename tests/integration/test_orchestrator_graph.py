"""실제 LangGraph checkpoint와 Command resume 동작을 검증한다."""

from __future__ import annotations

import asyncio
import threading
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
    ApprovalConsumer,
    ApprovalConsumeResult,
    ApprovalConsumeStatus,
    ApprovalResponse,
    ApprovalResumeError,
    ExecutionClaimResult,
    ExecutionClaimStatus,
    FailureCode,
    FakeApprovalConsumer,
    FakeExecutionCoordinator,
    FakeGovernance,
    FakeRequestClassifier,
    OrchestrationDependencyError,
    OrchestrationRecoveryError,
    OrchestrationStartError,
    OrchestrationStateError,
    OrchestratorService,
    RequestKind,
    RoutingDecision,
    Status,
    Task,
    UserTaskInput,
)
from agent_system.persistence import SQLiteStore, upgrade_database

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class _FailAfterConsumeApprovalConsumer:
    """첫 소비를 저장한 직후 process failure를 재현한다."""

    def __init__(self, inner: ApprovalConsumer) -> None:
        self._inner = inner
        self._failed = False

    async def consume(self, **kwargs: object) -> object:
        result = await self._inner.consume(**kwargs)  # type: ignore[arg-type]
        if not self._failed:
            self._failed = True
            raise RuntimeError("consume 이후 crash")
        return result


class _RaisingApprovalConsumer:
    async def consume(self, **kwargs: object) -> object:
        raise RuntimeError("approval-secret")


class _CancelledApprovalConsumer:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def consume(self, **kwargs: object) -> object:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("취소 뒤에는 도달할 수 없습니다.")


class _MalformedApprovalConsumer:
    def __init__(self, mutation: str) -> None:
        self._mutation = mutation

    async def consume(
        self,
        *,
        task: Task,
        response: ApprovalResponse,
        at: datetime,
    ) -> ApprovalConsumeResult:
        assert response.approval is not None
        successor = task.transition(
            Status.RUNNING,
            approval=response.approval,
            at=at,
        )
        if self._mutation == "forged_status":
            result = object.__new__(ApprovalConsumeResult)
            object.__setattr__(result, "status", "applied")
            object.__setattr__(result, "task", successor)
            object.__setattr__(result, "failure", None)
            return result
        if self._mutation == "status":
            return ApprovalConsumeResult(
                "applied",  # type: ignore[arg-type]
                successor,
            )
        if self._mutation == "task":
            return ApprovalConsumeResult(
                ApprovalConsumeStatus.APPLIED,
                True,  # type: ignore[arg-type]
            )
        if self._mutation == "failure":
            return ApprovalConsumeResult(
                ApprovalConsumeStatus.APPLIED,
                successor,
                True,  # type: ignore[arg-type]
            )
        return ApprovalConsumeResult(
            ApprovalConsumeStatus.APPLIED,
            successor,
            FailureCode.HUMAN_REJECTED,
        )


class _RaisingExecutionCoordinator:
    async def claim(self, **kwargs: object) -> object:
        raise RuntimeError("claim-secret")

    async def release(self, **kwargs: object) -> None:
        raise AssertionError("claim 실패 뒤 release하면 안 됩니다.")


class _CancelledExecutionCoordinator:
    async def claim(self, **kwargs: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError("취소 뒤에는 도달할 수 없습니다.")

    async def release(self, **kwargs: object) -> None:
        raise AssertionError("claim 취소 뒤 release하면 안 됩니다.")


class _ForgedExecutionCoordinator:
    async def claim(self, **kwargs: object) -> ExecutionClaimResult:
        result = object.__new__(ExecutionClaimResult)
        object.__setattr__(result, "status", "claimed")
        return result

    async def release(self, **kwargs: object) -> None:
        raise AssertionError("잘못된 claim 결과는 release하면 안 됩니다.")


class _RaisingReleaseCoordinator(FakeExecutionCoordinator):
    async def release(self, **kwargs: object) -> None:
        raise RuntimeError("release-secret")


class _RecordingExecutionCoordinator(FakeExecutionCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.claimed: list[tuple[str, str]] = []

    async def claim(self, *, thread_id: str, claim_key: str) -> object:
        self.claimed.append((thread_id, claim_key))
        return await super().claim(thread_id=thread_id, claim_key=claim_key)


class _StartRaceExecutionCoordinator(_RecordingExecutionCoordinator):
    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    async def claim(self, *, thread_id: str, claim_key: str) -> object:
        result = await super().claim(thread_id=thread_id, claim_key=claim_key)
        if result.status is ExecutionClaimStatus.BUSY:
            self._barrier.abort()
        return result


class _MutatingExecutionCoordinator(FakeExecutionCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.service: OrchestratorService | None = None

    async def claim(self, **kwargs: object) -> object:
        assert self.service is not None
        config = {"configurable": {"thread_id": "thread-open"}}
        snapshot = await self.service._graph.aget_state(config)
        request = dict(snapshot.values["request"])
        request["kind"] = 1
        await self.service._graph.aupdate_state(
            config,
            {"request": request},
            as_node="issue_agent_run",
        )
        return await super().claim(**kwargs)  # type: ignore[arg-type]


class ApprovalResumeTests(unittest.IsolatedAsyncioTestCase):
    """승인 interrupt가 같은 checkpoint thread에서만 재개된다."""

    def make_service(
        self,
        *,
        checkpointer: InMemorySaver | None = None,
        approval_consumer: FakeApprovalConsumer | None = None,
        execution_coordinator: FakeExecutionCoordinator | None = None,
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
            execution_coordinator=(execution_coordinator or FakeExecutionCoordinator()),
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

    async def test_retry_heals_crash_after_approval_consume_before_command(
        self,
    ) -> None:
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                checkpointer = InMemorySaver()
                consumer = _FailAfterConsumeApprovalConsumer(FakeApprovalConsumer())
                coordinator = FakeExecutionCoordinator()
                first_service, agent = self.make_service(
                    checkpointer=checkpointer,
                    approval_consumer=consumer,  # type: ignore[arg-type]
                    execution_coordinator=coordinator,
                )
                waiting = await first_service.start(
                    UserTaskInput("task-heal", "서비스를 복구해 주세요."),
                    thread_id="thread-heal",
                )
                assert waiting.interrupt is not None
                response = (
                    ApprovalResponse.approve(
                        waiting.interrupt,
                        decision_id="decision-heal",
                        at=NOW,
                    )
                    if accepted
                    else ApprovalResponse.reject(
                        decision_id="decision-heal",
                        reason="거절",
                    )
                )
                with self.assertRaises(OrchestrationDependencyError):
                    await first_service.resume(
                        thread_id="thread-heal", response=response
                    )
                second_service, _ = self.make_service(
                    checkpointer=checkpointer,
                    approval_consumer=consumer,  # type: ignore[arg-type]
                    execution_coordinator=coordinator,
                    agent=agent,
                )

                result = await second_service.resume(
                    thread_id="thread-heal", response=response
                )

                self.assertEqual(
                    result.task.status,
                    Status.COMPLETED if accepted else Status.REJECTED,
                )
                self.assertEqual(len(agent.received_requests), int(accepted))

    async def test_approval_consumer_error_is_stable_and_cause_free(self) -> None:
        service, _ = self.make_service(
            approval_consumer=_RaisingApprovalConsumer(),  # type: ignore[arg-type]
        )
        waiting = await service.start(
            UserTaskInput("task-consumer-error", "복구"),
            thread_id="thread-consumer-error",
        )
        assert waiting.interrupt is not None

        with self.assertRaises(OrchestrationDependencyError) as raised:
            await service.resume(
                thread_id="thread-consumer-error",
                response=ApprovalResponse.approve(
                    waiting.interrupt,
                    decision_id="decision-consumer-error",
                    at=NOW,
                ),
            )

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("approval-secret", str(raised.exception))

    async def test_changed_decision_cannot_heal_consumed_approval_binding(self) -> None:
        checkpointer = InMemorySaver()
        consumer = _FailAfterConsumeApprovalConsumer(FakeApprovalConsumer())
        service, agent = self.make_service(
            checkpointer=checkpointer,
            approval_consumer=consumer,  # type: ignore[arg-type]
        )
        waiting = await service.start(
            UserTaskInput("task-decision-conflict", "복구"),
            thread_id="thread-decision-conflict",
        )
        assert waiting.interrupt is not None
        original = ApprovalResponse.approve(
            waiting.interrupt,
            decision_id="decision-original",
            at=NOW,
        )
        with self.assertRaises(OrchestrationDependencyError):
            await service.resume(
                thread_id="thread-decision-conflict", response=original
            )
        changed = ApprovalResponse.approve(
            waiting.interrupt,
            decision_id="decision-changed",
            at=NOW,
        )

        with self.assertRaises(ApprovalResumeError):
            await service.resume(thread_id="thread-decision-conflict", response=changed)

        self.assertEqual(agent.received_requests, [])

    async def test_approval_consumer_cancellation_propagates(self) -> None:
        checkpointer = InMemorySaver()
        coordinator = _RecordingExecutionCoordinator()
        consumer = _CancelledApprovalConsumer()
        service, agent = self.make_service(
            checkpointer=checkpointer,
            approval_consumer=consumer,  # type: ignore[arg-type]
            execution_coordinator=coordinator,
        )
        waiting = await service.start(
            UserTaskInput("task-consumer-cancel", "복구"),
            thread_id="thread-consumer-cancel",
        )
        assert waiting.interrupt is not None
        invocation = asyncio.create_task(
            service.resume(
                thread_id="thread-consumer-cancel",
                response=ApprovalResponse.approve(
                    waiting.interrupt,
                    decision_id="decision-consumer-cancel",
                    at=NOW,
                ),
            )
        )
        await consumer.started.wait()
        invocation.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await invocation

        retry_service, _ = self.make_service(
            checkpointer=checkpointer,
            approval_consumer=FakeApprovalConsumer(),
            execution_coordinator=coordinator,
            agent=agent,
        )
        result = await retry_service.resume(
            thread_id="thread-consumer-cancel",
            response=ApprovalResponse.approve(
                waiting.interrupt,
                decision_id="decision-consumer-cancel",
                at=NOW,
            ),
        )

        self.assertEqual(result.task.status, Status.COMPLETED)
        self.assertEqual(len(agent.received_requests), 1)
        self.assertEqual(
            coordinator.claimed,
            [
                ("thread-consumer-cancel", "thread:thread-consumer-cancel"),
                ("thread-consumer-cancel", "thread:thread-consumer-cancel"),
                ("thread-consumer-cancel", "thread:thread-consumer-cancel"),
            ],
        )

    async def test_malformed_approval_consumer_result_is_dependency_error(self) -> None:
        for mutation in (
            "forged_status",
            "status",
            "task",
            "failure",
            "combination",
        ):
            with self.subTest(mutation=mutation):
                service, agent = self.make_service(
                    approval_consumer=_MalformedApprovalConsumer(mutation),  # type: ignore[arg-type]
                )
                waiting = await service.start(
                    UserTaskInput("task-malformed-consumer", "복구"),
                    thread_id="thread-malformed-consumer",
                )
                assert waiting.interrupt is not None

                with self.assertRaises(OrchestrationDependencyError) as raised:
                    await service.resume(
                        thread_id="thread-malformed-consumer",
                        response=ApprovalResponse.approve(
                            waiting.interrupt,
                            decision_id="decision-malformed-consumer",
                            at=NOW,
                        ),
                    )

                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(agent.received_requests, [])

    async def test_recover_keeps_approval_waiting_without_agent_execution(self) -> None:
        coordinator = _RecordingExecutionCoordinator()
        service, agent = self.make_service(execution_coordinator=coordinator)
        waiting = await service.start(
            UserTaskInput(task_id="task-waiting", input="서비스를 복구해 주세요."),
            thread_id="thread-waiting",
        )
        claims_before_recovery = list(coordinator.claimed)

        recovered = await service.recover(thread_id="thread-waiting")

        self.assertEqual(recovered, waiting)
        self.assertEqual(recovered.task.status, Status.WAITING_APPROVAL)
        self.assertEqual(agent.received_requests, [])
        self.assertEqual(
            claims_before_recovery,
            [("thread-waiting", "thread:thread-waiting")],
        )
        self.assertEqual(coordinator.claimed, claims_before_recovery)

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
        coordinator = FakeExecutionCoordinator()
        first_service, agent = self.make_service(
            checkpointer=checkpointer,
            approval_consumer=consumer,
            execution_coordinator=coordinator,
        )
        second_service, _ = self.make_service(
            checkpointer=checkpointer,
            approval_consumer=consumer,
            execution_coordinator=coordinator,
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
        self.agent_run_ids.append(request.idempotency_key)
        self.started.set()
        await self.release.wait()
        self.effects += 1
        return AgentResult(self.metadata.agent_id, AgentOutcome.SUCCESS, "복구 완료")


class _FirstReadBarrierCheckpointer:
    """두 SQLite service가 최초 thread 소유권 조회를 함께 통과하게 한다."""

    def __init__(self, inner: object, barrier: threading.Barrier) -> None:
        self._inner = inner
        self._barrier = barrier
        self._first_read = True

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def get_tuple(self, config: object) -> object:
        result = self._inner.get_tuple(config)  # type: ignore[attr-defined]
        if self._first_read:
            self._first_read = False
            try:
                self._barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
        return result


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
            execution_coordinator=FakeExecutionCoordinator(),
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


class RecoveryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    """복구 claim의 오류와 해제 정책을 facade에서 검증한다."""

    async def make_open_checkpoint(
        self,
    ) -> tuple[
        InMemorySaver,
        AgentRegistry,
        FakeRequestClassifier,
        _RecoverableAgent,
    ]:
        saver = InMemorySaver()
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
        starter = OrchestratorService(
            classifier=classifier,
            governance=FakeGovernance(approved=True, reason="허용"),
            approval_consumer=FakeApprovalConsumer(),
            execution_coordinator=FakeExecutionCoordinator(),
            registry=registry,
            max_agent_runs=2,
            checkpointer=saver,
            clock=lambda: NOW,
            id_factory=iter(("workflow-open", "agent-run-open")).__next__,
        )
        invocation = asyncio.create_task(
            starter.start(
                UserTaskInput("task-open", "복구"),
                thread_id="thread-open",
            )
        )
        await agent.started.wait()
        invocation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await invocation
        agent.started = asyncio.Event()
        agent.release = asyncio.Event()
        agent.agent_run_ids.clear()
        return saver, registry, classifier, agent

    def make_recovery_service(
        self,
        *,
        saver: InMemorySaver,
        registry: AgentRegistry,
        classifier: FakeRequestClassifier,
        coordinator: object,
    ) -> OrchestratorService:
        return OrchestratorService(
            classifier=classifier,
            governance=FakeGovernance(approved=True, reason="허용"),
            approval_consumer=FakeApprovalConsumer(),
            execution_coordinator=coordinator,  # type: ignore[arg-type]
            registry=registry,
            max_agent_runs=2,
            checkpointer=saver,
            clock=lambda: NOW,
            id_factory=iter(("unused",)).__next__,
        )

    async def test_claim_error_is_stable_and_cause_free(self) -> None:
        saver, registry, classifier, _ = await self.make_open_checkpoint()
        service = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=_RaisingExecutionCoordinator(),
        )

        with self.assertRaises(OrchestrationDependencyError) as raised:
            await service.recover(thread_id="thread-open")

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("claim-secret", str(raised.exception))

    async def test_recovery_uses_thread_execution_claim_key(self) -> None:
        saver, registry, classifier, agent = await self.make_open_checkpoint()
        coordinator = _RecordingExecutionCoordinator()
        agent.release.set()
        service = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=coordinator,
        )

        await service.recover(thread_id="thread-open")

        self.assertEqual(
            coordinator.claimed,
            [("thread-open", "thread:thread-open")],
        )

    async def test_claim_cancellation_propagates(self) -> None:
        saver, registry, classifier, _ = await self.make_open_checkpoint()
        service = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=_CancelledExecutionCoordinator(),
        )
        invocation = asyncio.create_task(service.recover(thread_id="thread-open"))
        await asyncio.sleep(0)
        invocation.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await invocation

    async def test_forged_claim_result_is_dependency_error(self) -> None:
        saver, registry, classifier, _ = await self.make_open_checkpoint()
        service = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=_ForgedExecutionCoordinator(),
        )

        with self.assertRaises(OrchestrationDependencyError) as raised:
            await service.recover(thread_id="thread-open")

        self.assertIsNone(raised.exception.__cause__)

    async def test_release_error_is_stable_and_cause_free(self) -> None:
        saver, registry, classifier, agent = await self.make_open_checkpoint()
        agent.release.set()
        service = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=_RaisingReleaseCoordinator(),
        )

        with self.assertRaises(OrchestrationDependencyError) as raised:
            await service.recover(thread_id="thread-open")

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("release-secret", str(raised.exception))

    async def test_cancelled_recovery_releases_claim_for_retry(self) -> None:
        saver, registry, classifier, agent = await self.make_open_checkpoint()
        coordinator = FakeExecutionCoordinator()
        first = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=coordinator,
        )
        invocation = asyncio.create_task(first.recover(thread_id="thread-open"))
        await agent.started.wait()
        invocation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await invocation
        agent.started = asyncio.Event()
        agent.release = asyncio.Event()
        second = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=coordinator,
        )
        retry = asyncio.create_task(second.recover(thread_id="thread-open"))
        await agent.started.wait()
        agent.release.set()

        result = await retry

        self.assertEqual(result.task.status, Status.COMPLETED)

    async def test_malformed_open_checkpoint_is_normalized(self) -> None:
        mutation_names = ("request", "routing", "workflow", "agent_run")
        for mutation_name in mutation_names:
            with self.subTest(mutation_name=mutation_name):
                saver, registry, classifier, _ = await self.make_open_checkpoint()
                service = self.make_recovery_service(
                    saver=saver,
                    registry=registry,
                    classifier=classifier,
                    coordinator=FakeExecutionCoordinator(),
                )
                config = {"configurable": {"thread_id": "thread-open"}}
                snapshot = await service._graph.aget_state(config)
                values = dict(snapshot.values)
                if mutation_name == "request":
                    malformed = dict(values["request"])
                    malformed["kind"] = 1
                    update = {"request": malformed}
                elif mutation_name == "routing":
                    malformed = dict(values["routing"])
                    malformed["agent_id"] = 1
                    update = {"routing": malformed}
                elif mutation_name == "workflow":
                    malformed = dict(values["workflow"])
                    malformed["task_version"] = "2"
                    update = {"workflow": malformed}
                elif mutation_name == "agent_run":
                    malformed = dict(values["agent_runs"][-1])
                    malformed["budget_sequence"] = "1"
                    update = {"agent_runs": [malformed]}
                await service._graph.aupdate_state(
                    config,
                    update,
                    as_node="issue_agent_run",
                )

                with self.assertRaises(OrchestrationStateError) as raised:
                    await service.recover(thread_id="thread-open")

                self.assertIsNone(raised.exception.__cause__)

    async def test_ainvoke_malformed_state_is_normalized(self) -> None:
        saver, registry, classifier, _ = await self.make_open_checkpoint()
        coordinator = _MutatingExecutionCoordinator()
        service = self.make_recovery_service(
            saver=saver,
            registry=registry,
            classifier=classifier,
            coordinator=coordinator,
        )
        coordinator.service = service

        with self.assertRaises(OrchestrationStateError) as raised:
            await service.recover(thread_id="thread-open")

        self.assertIsNone(raised.exception.__cause__)


class SQLiteCheckpointerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """Persistence 공개 checkpointer를 async supervisor에 그대로 주입한다."""

    async def test_two_services_claim_thread_before_sqlite_start_ownership(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "start-claim-race.db"
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
                        "시작 경합",
                    )
                }
            )
            barrier = threading.Barrier(2)
            coordinator = _StartRaceExecutionCoordinator(barrier)
            with (
                SQLiteStore(database_path) as store,
                store.open_checkpointer() as first_saver,
                store.open_checkpointer() as second_saver,
            ):
                first = OrchestratorService(
                    classifier=classifier,
                    governance=FakeGovernance(approved=True, reason="허용"),
                    approval_consumer=FakeApprovalConsumer(),
                    execution_coordinator=coordinator,
                    registry=registry,
                    max_agent_runs=2,
                    checkpointer=_FirstReadBarrierCheckpointer(first_saver, barrier),  # type: ignore[arg-type]
                    clock=lambda: NOW,
                    id_factory=iter(
                        ("workflow-start-first", "agent-run-start-first")
                    ).__next__,
                )
                second = OrchestratorService(
                    classifier=classifier,
                    governance=FakeGovernance(approved=True, reason="허용"),
                    approval_consumer=FakeApprovalConsumer(),
                    execution_coordinator=coordinator,
                    registry=registry,
                    max_agent_runs=2,
                    checkpointer=_FirstReadBarrierCheckpointer(second_saver, barrier),  # type: ignore[arg-type]
                    clock=lambda: NOW,
                    id_factory=iter(
                        ("workflow-start-second", "agent-run-start-second")
                    ).__next__,
                )
                invocations = (
                    asyncio.create_task(
                        first.start(
                            UserTaskInput("task-start-first", "복구"),
                            thread_id="thread-start-claim",
                        )
                    ),
                    asyncio.create_task(
                        second.start(
                            UserTaskInput("task-start-second", "복구"),
                            thread_id="thread-start-claim",
                        )
                    ),
                )
                await agent.started.wait()
                agent.release.set()
                results = await asyncio.gather(
                    *invocations,
                    return_exceptions=True,
                )

            winners = [
                result for result in results if not isinstance(result, BaseException)
            ]
            self.assertEqual(len(winners), 1)
            self.assertEqual(
                sum(isinstance(result, OrchestrationStartError) for result in results),
                1,
            )
            winner = winners[0]
            self.assertEqual(winner.task.status, Status.COMPLETED)
            self.assertEqual(winner.workflow.budget.consumed, 1)
            self.assertEqual(len(winner.agent_runs), 1)
            self.assertEqual(len(agent.agent_run_ids), 1)
            self.assertEqual(agent.agent_run_ids[0], winner.agent_runs[0].agent_run_id)
            self.assertEqual(agent.effects, 1)
            self.assertEqual(
                coordinator.claimed,
                [
                    ("thread-start-claim", "thread:thread-start-claim"),
                    ("thread-start-claim", "thread:thread-start-claim"),
                ],
            )

    async def test_recover_loses_to_blocked_sqlite_approval_resume(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "resume-recovery-race.db"
            upgrade_database(database_path)
            plan = ActionPlan("서비스 재시작", ("재시작",))
            classifier = FakeRequestClassifier(
                {
                    RequestKind.USER_TASK: RoutingDecision(
                        RequestKind.USER_TASK,
                        "recoverable",
                        ActionKind.MUTATING,
                        "승인 실행 경합",
                        plan,
                    )
                }
            )
            agent = _RecoverableAgent()
            registry = AgentRegistry()
            registry.register(agent)
            consumer = FakeApprovalConsumer()
            coordinator = _RecordingExecutionCoordinator()
            with SQLiteStore(database_path) as store:
                with store.open_checkpointer() as saver:
                    starter = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=consumer,
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=saver,
                        clock=lambda: NOW,
                        id_factory=iter(("workflow-resume-race",)).__next__,
                    )
                    waiting = await starter.start(
                        UserTaskInput("task-resume-race", "복구"),
                        thread_id="thread-resume-race",
                    )
                    assert waiting.interrupt is not None
                with (
                    store.open_checkpointer() as resume_saver,
                    store.open_checkpointer() as recovery_saver,
                ):
                    resumer = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=consumer,
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=resume_saver,
                        clock=lambda: NOW,
                        id_factory=iter(("agent-run-resume-race",)).__next__,
                    )
                    recoverer = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=consumer,
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=recovery_saver,
                        clock=lambda: NOW,
                        id_factory=iter(("unused-recovery-id",)).__next__,
                    )
                    resuming = asyncio.create_task(
                        resumer.resume(
                            thread_id="thread-resume-race",
                            response=ApprovalResponse.approve(
                                waiting.interrupt,
                                decision_id="decision-resume-race",
                                at=NOW,
                            ),
                        )
                    )
                    await agent.started.wait()
                    for _ in range(1000):
                        observed = await recoverer.get_result(
                            thread_id="thread-resume-race"
                        )
                        if (
                            observed.agent_runs
                            and not observed.agent_runs[-1].is_completed
                        ):
                            break
                        await asyncio.sleep(0)
                    else:
                        self.fail(
                            "recovery service가 열린 AgentRun을 관찰하지 못했습니다."
                        )
                    recovering = asyncio.create_task(
                        recoverer.recover(thread_id="thread-resume-race")
                    )
                    for _ in range(1000):
                        if recovering.done() or len(agent.agent_run_ids) > 1:
                            break
                        await asyncio.sleep(0)
                    agent.release.set()
                    resumed, recovered = await asyncio.gather(
                        resuming,
                        recovering,
                        return_exceptions=True,
                    )

            self.assertNotIsInstance(resumed, BaseException)
            assert not isinstance(resumed, BaseException)
            self.assertEqual(resumed.task.status, Status.COMPLETED)
            self.assertIsInstance(recovered, OrchestrationRecoveryError)
            self.assertEqual(resumed.workflow.budget.consumed, 1)
            self.assertEqual(len(resumed.agent_runs), 1)
            self.assertEqual(agent.agent_run_ids, ["agent-run-resume-race"])
            self.assertEqual(agent.agent_run_ids[0], resumed.agent_runs[0].agent_run_id)
            self.assertEqual(agent.effects, 1)
            self.assertEqual(
                coordinator.claimed,
                [
                    ("thread-resume-race", "thread:thread-resume-race"),
                    ("thread-resume-race", "thread:thread-resume-race"),
                    ("thread-resume-race", "thread:thread-resume-race"),
                ],
            )

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
            coordinator = _RecordingExecutionCoordinator()
            with SQLiteStore(database_path) as store:
                with store.open_checkpointer() as saver:
                    service = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
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
                        execution_coordinator=coordinator,
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
                    self.assertEqual(
                        coordinator.claimed,
                        [("thread-sqlite", "thread:thread-sqlite")],
                    )

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
            coordinator = _RecordingExecutionCoordinator()
            with SQLiteStore(database_path) as store:
                with store.open_checkpointer() as saver:
                    service = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
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
                        execution_coordinator=coordinator,
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
            self.assertEqual(
                coordinator.claimed,
                [
                    ("thread-recover", "thread:thread-recover"),
                    ("thread-recover", "thread:thread-recover"),
                ],
            )

    async def test_two_services_claim_one_open_sqlite_agent_run(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "claim-race.db"
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
                        "복구 경합",
                    )
                }
            )
            coordinator = FakeExecutionCoordinator()
            with SQLiteStore(database_path) as store:
                with store.open_checkpointer() as saver:
                    starter = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=saver,
                        clock=lambda: NOW,
                        id_factory=iter(("workflow-claim", "agent-run-claim")).__next__,
                    )
                    interrupted = asyncio.create_task(
                        starter.start(
                            UserTaskInput("task-claim", "복구"),
                            thread_id="thread-claim",
                        )
                    )
                    await agent.started.wait()
                    interrupted.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await interrupted
                agent.started = asyncio.Event()
                agent.release = asyncio.Event()
                agent.agent_run_ids.clear()
                with (
                    store.open_checkpointer() as first_saver,
                    store.open_checkpointer() as second_saver,
                ):
                    first = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=first_saver,
                        clock=lambda: NOW,
                        id_factory=iter(("unused-first",)).__next__,
                    )
                    second = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=second_saver,
                        clock=lambda: NOW,
                        id_factory=iter(("unused-second",)).__next__,
                    )
                    winner = asyncio.create_task(
                        first.recover(thread_id="thread-claim")
                    )
                    await agent.started.wait()
                    with self.assertRaises(OrchestrationRecoveryError) as raised:
                        await second.recover(thread_id="thread-claim")
                    self.assertIsNone(raised.exception.__cause__)
                    agent.release.set()
                    result = await winner

            self.assertEqual(result.task.status, Status.COMPLETED)
            self.assertEqual(result.workflow.budget.consumed, 1)
            self.assertEqual(agent.agent_run_ids, ["agent-run-claim"])
            self.assertEqual(agent.effects, 1)

    async def test_two_services_claim_before_sqlite_agent_run_issuance(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "issue-claim-race.db"
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
                        "issuance 복구 경합",
                    )
                }
            )
            coordinator = FakeExecutionCoordinator()
            config = {"configurable": {"thread_id": "thread-issue-claim"}}
            with SQLiteStore(database_path) as store:
                with store.open_checkpointer() as saver:
                    starter = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=saver,
                        clock=lambda: NOW,
                        id_factory=iter(
                            ("workflow-issue-claim", "initial-agent-run")
                        ).__next__,
                    )
                    interrupted = asyncio.create_task(
                        starter.start(
                            UserTaskInput("task-issue-claim", "복구"),
                            thread_id="thread-issue-claim",
                        )
                    )
                    await agent.started.wait()
                    interrupted.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await interrupted
                    history = [
                        state
                        async for state in starter._graph.aget_state_history(config)
                    ]
                    before_issuance = next(
                        state for state in history if state.next == ("issue_agent_run",)
                    )
                    await starter._graph.aupdate_state(
                        config,
                        dict(before_issuance.values),
                        as_node="prepare_read_only",
                    )
                agent.started = asyncio.Event()
                agent.release = asyncio.Event()
                agent.agent_run_ids.clear()
                with (
                    store.open_checkpointer() as first_saver,
                    store.open_checkpointer() as second_saver,
                ):
                    first = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=first_saver,
                        clock=lambda: NOW,
                        id_factory=iter(("agent-run-race-first",)).__next__,
                    )
                    second = OrchestratorService(
                        classifier=classifier,
                        governance=FakeGovernance(approved=True, reason="허용"),
                        approval_consumer=FakeApprovalConsumer(),
                        execution_coordinator=coordinator,
                        registry=registry,
                        max_agent_runs=2,
                        checkpointer=second_saver,
                        clock=lambda: NOW,
                        id_factory=iter(("agent-run-race-second",)).__next__,
                    )
                    winner = asyncio.create_task(
                        first.recover(thread_id="thread-issue-claim")
                    )
                    await agent.started.wait()
                    with self.assertRaises(OrchestrationRecoveryError):
                        await asyncio.wait_for(
                            second.recover(thread_id="thread-issue-claim"),
                            timeout=0.1,
                        )
                    agent.release.set()
                    result = await winner

            self.assertEqual(result.task.status, Status.COMPLETED)
            self.assertEqual(result.workflow.budget.consumed, 1)
            self.assertEqual(result.agent_runs[0].agent_run_id, "agent-run-race-first")
            self.assertEqual(agent.agent_run_ids, ["agent-run-race-first"])
            self.assertEqual(agent.effects, 1)
