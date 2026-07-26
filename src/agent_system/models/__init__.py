"""Provider 중립 chat model 설정과 생성 인터페이스."""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field as PydanticField


class ModelConfigurationError(ValueError):
    """Model 설정값이 adapter 경계의 불변 조건을 어겼을 때 발생한다."""


class ModelCapability(StrEnum):
    """호출자가 chat model에 요구할 수 있는 provider 중립 기능이다."""

    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"
    IMAGE_INPUT = "image_input"


class UnsupportedModelCapabilityError(ModelConfigurationError):
    """선택한 provider가 필수 capability를 제공하지 않을 때 발생한다."""


_UPSTAGE_CAPABILITIES = frozenset(
    {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}
)


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Chat model adapter를 생성하는 데 필요한 설정이다."""

    provider: str
    model_name: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        required_values = {
            "MODEL_PROVIDER": self.provider,
            "MODEL_NAME": self.model_name,
            "UPSTAGE_API_KEY": self.api_key,
        }
        for setting_name, value in required_values.items():
            if not value.strip():
                raise ModelConfigurationError(
                    f"{setting_name}는 비어 있을 수 없습니다."
                )
        if self.timeout_seconds <= 0:
            raise ModelConfigurationError("MODEL_TIMEOUT_SECONDS는 0보다 커야 합니다.")
        if self.max_retries < 0:
            raise ModelConfigurationError("MODEL_MAX_RETRIES는 0 이상이어야 합니다.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ModelSettings:
        """환경 변수 mapping을 명시적인 model 설정으로 변환한다."""

        values = os.environ if env is None else env
        try:
            timeout_seconds = float(values.get("MODEL_TIMEOUT_SECONDS", "30"))
            max_retries = int(values.get("MODEL_MAX_RETRIES", "2"))
        except ValueError as error:
            raise ModelConfigurationError(
                "Model timeout 또는 retry 설정이 올바른 숫자가 아닙니다."
            ) from error
        return cls(
            provider=values.get("MODEL_PROVIDER", ""),
            model_name=values.get("MODEL_NAME", ""),
            api_key=values.get("UPSTAGE_API_KEY", ""),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )


class FakeChatModel(BaseChatModel):
    """고정 응답을 반환하는 외부 I/O 없는 LangChain ChatModel adapter다."""

    response: str = ""
    received_messages: list[list[BaseMessage]] = PydanticField(
        default_factory=list,
        exclude=True,
    )

    @property
    def _llm_type(self) -> str:
        return "fake-chat-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received_messages.append(list(messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response))]
        )


def create_chat_model(
    settings: ModelSettings,
    *,
    required_capabilities: Collection[ModelCapability] = (),
) -> BaseChatModel:
    """설정된 provider adapter를 LangChain ChatModel로 생성한다."""

    if settings.provider == "upstage":
        unsupported = set(required_capabilities) - _UPSTAGE_CAPABILITIES
        if unsupported:
            capability_names = ", ".join(
                sorted(capability.value for capability in unsupported)
            )
            raise UnsupportedModelCapabilityError(
                f"upstage provider가 지원하지 않는 model capability입니다: {capability_names}"
            )

        from langchain_upstage import ChatUpstage

        return ChatUpstage(
            model=settings.model_name,
            api_key=settings.api_key,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )
    raise ModelConfigurationError(
        f"지원하지 않는 model provider입니다: {settings.provider}"
    )


__all__ = [
    "FakeChatModel",
    "ModelCapability",
    "ModelConfigurationError",
    "ModelSettings",
    "UnsupportedModelCapabilityError",
    "create_chat_model",
]
