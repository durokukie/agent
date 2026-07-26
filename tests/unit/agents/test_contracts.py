"""공통 Agent 계약과 registry의 관찰 가능한 동작을 검증한다."""

from __future__ import annotations

import unittest

from agent_system.agents import (
    Agent,
    AgentMetadata,
    AgentNotFoundError,
    AgentOutcome,
    AgentRegistry,
    AgentRequest,
    AgentResult,
    DuplicateAgentIdError,
    EchoAgent,
    FakeAgent,
)


REQUEST = AgentRequest(
    task_id="task-123",
    input="현재 상태를 분석해 주세요.",
    context={"priority": "high"},
)
METADATA = AgentMetadata(
    agent_id="analysis",
    name="분석 Agent",
    description="요청 내용을 분석합니다.",
)


class AgentContractTestMixin:
    """새 Agent 구현이 따라야 할 공통 관찰 가능 계약이다."""

    agent: Agent
    expected_result: AgentResult

    async def assert_agent_contract(self) -> None:
        self.assertIsInstance(self.agent, Agent)

        result = await self.agent.run(REQUEST)

        self.assertEqual(result, self.expected_result)
        self.assertEqual(result.agent_id, self.agent.metadata.agent_id)


class EchoAgentContractTests(AgentContractTestMixin, unittest.IsolatedAsyncioTestCase):
    """기본 구현도 공통 Agent 계약을 만족한다."""

    def setUp(self) -> None:
        self.agent = EchoAgent(METADATA)
        self.expected_result = AgentResult(
            agent_id="analysis",
            outcome=AgentOutcome.SUCCESS,
            output="현재 상태를 분석해 주세요.",
        )

    async def test_satisfies_the_common_agent_contract(self) -> None:
        await self.assert_agent_contract()


class FakeAgentContractTests(AgentContractTestMixin, unittest.IsolatedAsyncioTestCase):
    """결정 가능한 fake도 공통 Agent 계약을 만족한다."""

    def setUp(self) -> None:
        self.agent = FakeAgent(
            METADATA,
            outcome=AgentOutcome.FAILURE,
            output="도구 호출에 실패했습니다.",
        )
        self.expected_result = AgentResult(
            agent_id="analysis",
            outcome=AgentOutcome.FAILURE,
            output="도구 호출에 실패했습니다.",
        )

    async def test_satisfies_the_common_agent_contract(self) -> None:
        await self.assert_agent_contract()

    async def test_returns_the_same_configured_result_for_each_request(self) -> None:
        first_result = await self.agent.run(REQUEST)
        second_result = await self.agent.run(REQUEST)

        self.assertEqual(first_result, second_result)
        self.assertEqual(self.agent.received_requests, [REQUEST, REQUEST])


class AgentRegistryTests(unittest.TestCase):
    """Registry가 Agent 식별자 불변 조건을 지킨다."""

    def test_returns_the_registered_agent_for_its_identifier(self) -> None:
        agent = EchoAgent(METADATA)
        registry = AgentRegistry()

        registry.register(agent)

        self.assertIs(registry.get("analysis"), agent)

    def test_rejects_duplicate_agent_identifier(self) -> None:
        registry = AgentRegistry()
        registry.register(EchoAgent(METADATA))

        with self.assertRaises(DuplicateAgentIdError):
            registry.register(FakeAgent(METADATA))

    def test_rejects_an_unregistered_agent_identifier(self) -> None:
        registry = AgentRegistry()

        with self.assertRaises(AgentNotFoundError):
            registry.get("missing")
