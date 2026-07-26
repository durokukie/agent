"""Supervisor orchestration 공개 계약을 외부 I/O 없이 검증한다."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agent_system.agents import (
    AgentMetadata,
    AgentOutcome,
    AgentRegistry,
    AgentRequest,
    AgentResult,
    FakeAgent,
)
from agent_system.models import FakeChatModel
from agent_system.orchestration import (
    ActionKind,
    ActionPlan,
    AlertInput,
    ChatModelRequestClassifier,
    ClassificationError,
    FailureCode,
    FakeGovernance,
    FakeRequestClassifier,
    OrchestratorService,
    RequestKind,
    RoutingDecision,
    Status,
    TicketInput,
    UserTaskInput,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class _RaisingAgent:
    metadata = AgentMetadata("broken", "예외 Agent", "예외를 발생시킴")

    async def run(self, request: AgentRequest) -> AgentResult:
        raise RuntimeError("외부에 노출되면 안 되는 secret")


class _MismatchedAgent:
    metadata = AgentMetadata("mismatch", "불일치 Agent", "잘못된 ID를 반환")

    async def run(self, request: AgentRequest) -> AgentResult:
        return AgentResult(
            agent_id="another-agent",
            outcome=AgentOutcome.SUCCESS,
            output="잘못된 결과",
        )


class _RecoveringAgent:
    metadata = AgentMetadata("recovering", "복구 Agent", "두 번째 호출에 성공")

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: AgentRequest) -> AgentResult:
        self.calls += 1
        return AgentResult(
            agent_id=self.metadata.agent_id,
            outcome=(AgentOutcome.FAILURE if self.calls == 1 else AgentOutcome.SUCCESS),
            output="일시 실패" if self.calls == 1 else "복구 완료",
        )


class _RaisingClassifier:
    async def classify(self, request: UserTaskInput) -> RoutingDecision:
        raise RuntimeError("분류 provider secret")


class _RaisingGovernance:
    async def evaluate(
        self,
        request: UserTaskInput,
        routing: RoutingDecision,
    ) -> object:
        raise RuntimeError("governance provider secret")


class RequestClassifierTests(unittest.IsolatedAsyncioTestCase):
    """분류 결과가 안정적인 route와 근거를 구조화해 남긴다."""

    async def test_fake_classifies_a_ticket_with_a_traceable_route(self) -> None:
        decision = RoutingDecision(
            request_kind=RequestKind.TICKET,
            agent_id="ticket-reader",
            action=ActionKind.READ_ONLY,
            reason="티켓 조회 요청",
        )
        classifier = FakeRequestClassifier({RequestKind.TICKET: decision})
        request = TicketInput(
            task_id="task-ticket",
            ticket_id="INC-42",
            subject="배포 상태 문의",
            description="현재 배포 상태를 확인해 주세요.",
        )

        actual = await classifier.classify(request)

        self.assertEqual(actual, decision)
        self.assertEqual(classifier.received_requests, [request])

    async def test_fake_classifies_alert_and_user_task_by_their_input_kind(
        self,
    ) -> None:
        alert_decision = RoutingDecision(
            request_kind=RequestKind.ALERT,
            agent_id="incident-reader",
            action=ActionKind.READ_ONLY,
            reason="운영 경보 분석",
        )
        user_decision = RoutingDecision(
            request_kind=RequestKind.USER_TASK,
            agent_id="task-agent",
            action=ActionKind.READ_ONLY,
            reason="사용자 작업",
        )
        classifier = FakeRequestClassifier(
            {
                RequestKind.ALERT: alert_decision,
                RequestKind.USER_TASK: user_decision,
            }
        )

        alert = AlertInput(
            task_id="task-alert",
            alert_id="alert-7",
            severity="critical",
            message="오류율이 임계치를 넘었습니다.",
        )
        user_task = UserTaskInput(
            task_id="task-user",
            input="배포 상태를 요약해 주세요.",
        )

        self.assertEqual(await classifier.classify(alert), alert_decision)
        self.assertEqual(await classifier.classify(user_task), user_decision)

    async def test_chat_model_classifier_parses_validated_provider_neutral_json(
        self,
    ) -> None:
        model = FakeChatModel(
            response=(
                '{"request_kind":"alert","agent_id":"incident-reader",'
                '"action":"read_only","reason":"운영 경보 분석","plan":null}'
            )
        )
        classifier = ChatModelRequestClassifier(model)
        request = AlertInput(
            task_id="task-alert",
            alert_id="alert-8",
            severity="high",
            message="오류율 증가",
        )

        result = await classifier.classify(request)

        self.assertEqual(
            result,
            RoutingDecision(
                request_kind=RequestKind.ALERT,
                agent_id="incident-reader",
                action=ActionKind.READ_ONLY,
                reason="운영 경보 분석",
            ),
        )
        self.assertEqual(len(model.received_messages), 1)

    async def test_chat_model_classifier_rejects_unvalidated_output(self) -> None:
        classifier = ChatModelRequestClassifier(
            FakeChatModel(response='{"request_kind":"alert"}')
        )

        with self.assertRaises(ClassificationError):
            await classifier.classify(
                AlertInput(
                    task_id="task-alert",
                    alert_id="alert-9",
                    severity="high",
                    message="오류율 증가",
                )
            )


class OrchestratorServiceTests(unittest.IsolatedAsyncioTestCase):
    """Facade가 graph와 domain snapshot을 감춘 채 실행 결과를 반환한다."""

    async def test_read_only_route_completes_through_the_registered_agent(self) -> None:
        decision = RoutingDecision(
            request_kind=RequestKind.USER_TASK,
            agent_id="reader",
            action=ActionKind.READ_ONLY,
            reason="조회 전용 요청",
        )
        classifier = FakeRequestClassifier({RequestKind.USER_TASK: decision})
        governance = FakeGovernance(approved=True, reason="허용")
        agent = FakeAgent(
            AgentMetadata("reader", "조회 Agent", "읽기 전용 조회"),
            output="현재 상태는 정상입니다.",
        )
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=classifier,
            governance=governance,
            registry=registry,
            max_agent_runs=2,
            clock=lambda: NOW,
            id_factory=iter(("workflow-1", "agent-run-1")).__next__,
        )

        result = await service.start(
            UserTaskInput(task_id="task-1", input="현재 상태를 알려 주세요."),
            thread_id="thread-task-1",
        )

        self.assertEqual(result.task.status, Status.COMPLETED)
        self.assertEqual(result.output, "현재 상태는 정상입니다.")
        self.assertEqual(result.routing, decision)
        self.assertEqual(result.workflow.budget.consumed, 1)
        self.assertEqual(len(result.agent_runs), 1)
        self.assertEqual(agent.received_requests[0].task_id, "task-1")
        self.assertEqual(governance.received_requests, [])

    async def test_classifier_exception_fails_without_agent_budget_or_leakage(
        self,
    ) -> None:
        service = OrchestratorService(
            classifier=_RaisingClassifier(),
            governance=FakeGovernance(approved=True, reason="허용"),
            registry=AgentRegistry(),
            max_agent_runs=2,
            clock=lambda: NOW,
            id_factory=iter(("workflow-classifier-failure",)).__next__,
        )

        result = await service.start(
            UserTaskInput(task_id="task-classifier", input="상태를 알려 주세요."),
            thread_id="thread-classifier",
        )

        self.assertEqual(result.task.status, Status.FAILED)
        self.assertIsNone(result.routing)
        self.assertEqual(result.workflow.budget.consumed, 0)
        self.assertEqual(result.errors, (FailureCode.CLASSIFICATION_FAILED,))
        self.assertNotIn("secret", str(result))

    async def test_mutating_plan_waits_for_bound_human_approval(self) -> None:
        plan = ActionPlan(
            summary="서비스를 재시작합니다.",
            steps=("현재 상태 확인", "서비스 재시작"),
        )
        decision = RoutingDecision(
            request_kind=RequestKind.ALERT,
            agent_id="operator",
            action=ActionKind.MUTATING,
            reason="복구 작업 필요",
            plan=plan,
        )
        classifier = FakeRequestClassifier({RequestKind.ALERT: decision})
        governance = FakeGovernance(approved=True, reason="정책상 허용")
        agent = FakeAgent(
            AgentMetadata("operator", "운영 Agent", "복구 작업 실행"),
            output="재시작 완료",
        )
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=classifier,
            governance=governance,
            registry=registry,
            max_agent_runs=2,
            clock=lambda: NOW,
            id_factory=iter(("workflow-mutating",)).__next__,
        )

        result = await service.start(
            AlertInput(
                task_id="task-alert",
                alert_id="alert-1",
                severity="critical",
                message="서비스 응답이 없습니다.",
            ),
            thread_id="thread-task-alert",
        )

        self.assertEqual(result.task.status, Status.WAITING_APPROVAL)
        self.assertEqual(result.task.plan_hash, plan.plan_hash)
        self.assertIsNotNone(result.interrupt)
        assert result.interrupt is not None
        self.assertEqual(
            result.interrupt.binding,
            ("task-alert", result.task.version, plan.plan_hash),
        )
        self.assertEqual(len(governance.received_requests), 1)
        self.assertEqual(agent.received_requests, [])

    async def test_governance_rejection_terminates_without_human_or_agent(self) -> None:
        plan = ActionPlan(summary="설정을 변경합니다.", steps=("설정 변경",))
        decision = RoutingDecision(
            request_kind=RequestKind.USER_TASK,
            agent_id="operator",
            action=ActionKind.MUTATING,
            reason="설정 변경 요청",
            plan=plan,
        )
        classifier = FakeRequestClassifier({RequestKind.USER_TASK: decision})
        governance = FakeGovernance(approved=False, reason="허용되지 않은 변경")
        agent = FakeAgent(AgentMetadata("operator", "운영 Agent", "변경 실행"))
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=classifier,
            governance=governance,
            registry=registry,
            max_agent_runs=2,
            clock=lambda: NOW,
            id_factory=iter(("workflow-rejected",)).__next__,
        )

        result = await service.start(
            UserTaskInput(task_id="task-rejected", input="보안 설정을 꺼 주세요."),
            thread_id="thread-rejected",
        )

        self.assertEqual(result.task.status, Status.REJECTED)
        self.assertEqual(result.errors, (FailureCode.GOVERNANCE_REJECTED,))
        self.assertIsNone(result.interrupt)
        self.assertEqual(agent.received_requests, [])

    async def test_governance_exception_escalates_without_agent_execution(self) -> None:
        plan = ActionPlan(summary="설정을 변경합니다.", steps=("설정 변경",))
        decision = RoutingDecision(
            request_kind=RequestKind.USER_TASK,
            agent_id="operator",
            action=ActionKind.MUTATING,
            reason="설정 변경 요청",
            plan=plan,
        )
        service = OrchestratorService(
            classifier=FakeRequestClassifier({RequestKind.USER_TASK: decision}),
            governance=_RaisingGovernance(),
            registry=AgentRegistry(),
            max_agent_runs=2,
            clock=lambda: NOW,
            id_factory=iter(("workflow-governance-failure",)).__next__,
        )

        result = await service.start(
            UserTaskInput(task_id="task-governance", input="설정을 바꿔 주세요."),
            thread_id="thread-governance",
        )

        self.assertEqual(result.task.status, Status.ESCALATED)
        self.assertEqual(result.workflow.budget.consumed, 0)
        self.assertEqual(result.errors, (FailureCode.GOVERNANCE_FAILED,))
        self.assertNotIn("secret", str(result))

    async def test_agent_failure_consumes_retry_budget_then_escalates(self) -> None:
        decision = RoutingDecision(
            request_kind=RequestKind.USER_TASK,
            agent_id="unstable-reader",
            action=ActionKind.READ_ONLY,
            reason="조회 요청",
        )
        classifier = FakeRequestClassifier({RequestKind.USER_TASK: decision})
        agent = FakeAgent(
            AgentMetadata("unstable-reader", "불안정 Agent", "항상 실패"),
            outcome=AgentOutcome.FAILURE,
            output="내부 상세 오류",
        )
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=classifier,
            governance=FakeGovernance(approved=True, reason="허용"),
            registry=registry,
            max_agent_runs=2,
            clock=lambda: NOW,
            id_factory=iter(
                ("workflow-failure", "agent-run-failure-1", "agent-run-failure-2")
            ).__next__,
        )

        result = await service.start(
            UserTaskInput(task_id="task-failure", input="상태를 조회해 주세요."),
            thread_id="thread-failure",
        )

        self.assertEqual(result.task.status, Status.ESCALATED)
        self.assertEqual(result.workflow.budget.consumed, 2)
        self.assertEqual(len(result.agent_runs), 2)
        self.assertEqual(len(agent.received_requests), 2)
        self.assertEqual(
            result.errors,
            (
                FailureCode.AGENT_FAILURE,
                FailureCode.AGENT_FAILURE,
                FailureCode.RETRY_EXHAUSTED,
            ),
        )
        self.assertNotIn("내부 상세 오류", str(result.agent_runs))

    async def test_exception_mismatch_and_missing_route_escalate_without_leakage(
        self,
    ) -> None:
        cases = (
            ("broken", _RaisingAgent(), FailureCode.AGENT_EXCEPTION),
            ("mismatch", _MismatchedAgent(), FailureCode.AGENT_RESULT_MISMATCH),
            ("missing", None, FailureCode.ROUTE_NOT_FOUND),
        )
        for index, (agent_id, agent, expected_error) in enumerate(cases):
            with self.subTest(expected_error=expected_error):
                decision = RoutingDecision(
                    request_kind=RequestKind.USER_TASK,
                    agent_id=agent_id,
                    action=ActionKind.READ_ONLY,
                    reason="오류 정책 검증",
                )
                registry = AgentRegistry()
                if agent is not None:
                    registry.register(agent)
                service = OrchestratorService(
                    classifier=FakeRequestClassifier({RequestKind.USER_TASK: decision}),
                    governance=FakeGovernance(approved=True, reason="허용"),
                    registry=registry,
                    max_agent_runs=1,
                    clock=lambda: NOW,
                    id_factory=iter(
                        (f"workflow-error-{index}", f"agent-run-error-{index}")
                    ).__next__,
                )

                result = await service.start(
                    UserTaskInput(
                        task_id=f"task-error-{index}", input="상태를 조회합니다."
                    ),
                    thread_id=f"thread-error-{index}",
                )

                self.assertEqual(result.task.status, Status.ESCALATED)
                self.assertEqual(result.workflow.budget.consumed, 1)
                self.assertEqual(
                    result.errors,
                    (expected_error, FailureCode.RETRY_EXHAUSTED),
                )
                self.assertEqual(result.agent_runs[0].output, expected_error.value)
                self.assertNotIn("secret", str(result))

    async def test_successful_retry_uses_the_next_budget_slot_and_completes(
        self,
    ) -> None:
        decision = RoutingDecision(
            request_kind=RequestKind.USER_TASK,
            agent_id="recovering",
            action=ActionKind.READ_ONLY,
            reason="재시도 검증",
        )
        agent = _RecoveringAgent()
        registry = AgentRegistry()
        registry.register(agent)
        service = OrchestratorService(
            classifier=FakeRequestClassifier({RequestKind.USER_TASK: decision}),
            governance=FakeGovernance(approved=True, reason="허용"),
            registry=registry,
            max_agent_runs=2,
            clock=lambda: NOW,
            id_factory=iter(
                ("workflow-retry", "agent-run-retry-1", "agent-run-retry-2")
            ).__next__,
        )

        result = await service.start(
            UserTaskInput(task_id="task-retry", input="복구 상태를 확인해 주세요."),
            thread_id="thread-retry",
        )

        self.assertEqual(result.task.status, Status.COMPLETED)
        self.assertEqual(result.output, "복구 완료")
        self.assertEqual(result.workflow.budget.consumed, 2)
        self.assertEqual([run.budget_sequence for run in result.agent_runs], [1, 2])
        self.assertEqual(result.errors, (FailureCode.AGENT_FAILURE,))
