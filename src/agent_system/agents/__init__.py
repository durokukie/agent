"""오케스트레이터가 Agent 구현을 호출하기 위한 공통 계약과 registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol, runtime_checkable


class AgentOutcome(StrEnum):
    """Agent 실행의 최종 결과를 나타낸다."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class AgentMetadata:
    """Agent를 식별하고 사용자에게 설명하는 변경 불가능한 정보다."""

    agent_id: str
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """오케스트레이터가 Agent에 전달하는 표준 요청이다."""

    task_id: str
    input: str
    context: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Agent 실행 뒤 오케스트레이터가 소비하는 표준 결과다."""

    agent_id: str
    outcome: AgentOutcome
    output: str


@runtime_checkable
class Agent(Protocol):
    """설정형, 전용, 원격 Agent가 공통으로 제공해야 하는 구조적 인터페이스다."""

    @property
    def metadata(self) -> AgentMetadata:
        """Agent의 안정적인 식별 정보다."""

    async def run(self, request: AgentRequest) -> AgentResult:
        """표준 요청을 실행하고 표준 결과를 반환한다."""


class AgentRegistryError(Exception):
    """Agent registry 불변 조건이 깨졌을 때 발생하는 기본 오류다."""


class DuplicateAgentIdError(AgentRegistryError):
    """이미 등록된 Agent 식별자를 다시 등록하려 할 때 발생한다."""


class AgentNotFoundError(AgentRegistryError):
    """등록되지 않은 Agent 식별자를 조회할 때 발생한다."""


class AgentRegistry:
    """안정적인 Agent 식별자로 공통 인터페이스 구현을 조회한다."""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        """새 Agent를 등록하고, 같은 식별자의 중복 등록을 거부한다."""

        agent_id = agent.metadata.agent_id
        if agent_id in self._agents:
            raise DuplicateAgentIdError(f"이미 등록된 Agent ID입니다: {agent_id}")
        self._agents[agent_id] = agent

    def get(self, agent_id: str) -> Agent:
        """등록된 Agent를 반환하고 미등록 식별자는 명시적으로 거부한다."""

        try:
            return self._agents[agent_id]
        except KeyError as error:
            raise AgentNotFoundError(f"등록되지 않은 Agent ID입니다: {agent_id}") from error


class EchoAgent:
    """입력 문자열을 그대로 반환하는 외부 I/O 없는 최소 Agent 구현이다."""

    def __init__(self, metadata: AgentMetadata) -> None:
        self._metadata = metadata

    @property
    def metadata(self) -> AgentMetadata:
        """Agent 식별 정보를 반환한다."""

        return self._metadata

    async def run(self, request: AgentRequest) -> AgentResult:
        """요청의 입력 문자열을 성공 결과로 반환한다."""

        return AgentResult(
            agent_id=self.metadata.agent_id,
            outcome=AgentOutcome.SUCCESS,
            output=request.input,
        )


class FakeAgent:
    """고정된 결과를 반환하고 요청을 기록하는 결정 가능한 test adapter다."""

    def __init__(
        self,
        metadata: AgentMetadata,
        *,
        outcome: AgentOutcome = AgentOutcome.SUCCESS,
        output: str = "",
    ) -> None:
        self._metadata = metadata
        self._outcome = outcome
        self._output = output
        self.received_requests: list[AgentRequest] = []

    @property
    def metadata(self) -> AgentMetadata:
        """Agent 식별 정보를 반환한다."""

        return self._metadata

    async def run(self, request: AgentRequest) -> AgentResult:
        """요청을 기록하고 설정된 결과를 반환한다."""

        self.received_requests.append(request)
        return AgentResult(
            agent_id=self.metadata.agent_id,
            outcome=self._outcome,
            output=self._output,
        )


__all__ = [
    "Agent",
    "AgentMetadata",
    "AgentNotFoundError",
    "AgentOutcome",
    "AgentRegistry",
    "AgentRegistryError",
    "AgentRequest",
    "AgentResult",
    "DuplicateAgentIdError",
    "EchoAgent",
    "FakeAgent",
]
