"""Provider 중립 model 설정과 factory 계약을 검증한다."""

from __future__ import annotations

import unittest

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from agent_system.models import (
    FakeChatModel,
    ModelCapability,
    ModelConfigurationError,
    ModelSettings,
    UnsupportedModelCapabilityError,
    create_chat_model,
)


class ModelSettingsTests(unittest.TestCase):
    """환경 설정을 명시적인 model 생성 입력으로 변환한다."""

    def test_reads_upstage_settings_from_environment(self) -> None:
        settings = ModelSettings.from_env(
            {
                "MODEL_PROVIDER": "upstage",
                "MODEL_NAME": "solar-pro2",
                "UPSTAGE_API_KEY": "secret-value",
                "MODEL_TIMEOUT_SECONDS": "12.5",
                "MODEL_MAX_RETRIES": "4",
            }
        )

        self.assertEqual(settings.provider, "upstage")
        self.assertEqual(settings.model_name, "solar-pro2")
        self.assertEqual(settings.api_key, "secret-value")
        self.assertEqual(settings.timeout_seconds, 12.5)
        self.assertEqual(settings.max_retries, 4)

    def test_rejects_non_positive_timeout_at_our_configuration_boundary(self) -> None:
        for timeout_seconds in (0, -0.1):
            with (
                self.subTest(timeout_seconds=timeout_seconds),
                self.assertRaises(ModelConfigurationError),
            ):
                ModelSettings(
                    provider="upstage",
                    model_name="solar-pro2",
                    api_key="secret-value",
                    timeout_seconds=timeout_seconds,
                )

    def test_rejects_negative_retry_count_at_our_configuration_boundary(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            ModelSettings(
                provider="upstage",
                model_name="solar-pro2",
                api_key="secret-value",
                max_retries=-1,
            )

    def test_rejects_missing_required_model_settings(self) -> None:
        valid_settings = {
            "provider": "upstage",
            "model_name": "solar-pro2",
            "api_key": "secret-value",
        }
        for missing_field in valid_settings:
            values = valid_settings | {missing_field: ""}
            with (
                self.subTest(missing_field=missing_field),
                self.assertRaises(ModelConfigurationError),
            ):
                ModelSettings(**values)

    def test_reports_malformed_numeric_environment_values_as_configuration_errors(
        self,
    ) -> None:
        required_env = {
            "MODEL_PROVIDER": "upstage",
            "MODEL_NAME": "solar-pro2",
            "UPSTAGE_API_KEY": "secret-value",
        }
        invalid_values = {
            "MODEL_TIMEOUT_SECONDS": "not-a-number",
            "MODEL_MAX_RETRIES": "1.5",
        }
        for setting_name, invalid_value in invalid_values.items():
            with (
                self.subTest(setting_name=setting_name),
                self.assertRaises(ModelConfigurationError),
            ):
                ModelSettings.from_env(required_env | {setting_name: invalid_value})


class ModelFactoryTests(unittest.TestCase):
    """Provider별 SDK를 LangChain ChatModel seam 뒤에서 생성한다."""

    def test_creates_an_upstage_adapter_as_a_langchain_chat_model(self) -> None:
        model = create_chat_model(
            ModelSettings(
                provider="upstage",
                model_name="solar-pro2",
                api_key="test-api-key",
                timeout_seconds=7.5,
                max_retries=3,
            )
        )

        self.assertIsInstance(model, BaseChatModel)
        self.assertEqual(model._llm_type, "upstage-chat")

    def test_accepts_capabilities_used_by_agents_and_classifier(self) -> None:
        model = create_chat_model(
            ModelSettings(
                provider="upstage",
                model_name="solar-pro2",
                api_key="test-api-key",
            ),
            required_capabilities={
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.TOOL_CALLING,
            },
        )

        self.assertIsInstance(model, BaseChatModel)

    def test_rejects_capability_not_supported_by_the_upstage_adapter(self) -> None:
        with self.assertRaises(UnsupportedModelCapabilityError):
            create_chat_model(
                ModelSettings(
                    provider="upstage",
                    model_name="solar-pro2",
                    api_key="test-api-key",
                ),
                required_capabilities={ModelCapability.IMAGE_INPUT},
            )


class FakeChatModelTests(unittest.TestCase):
    """외부 호출 없이 결정 가능한 ChatModel adapter를 제공한다."""

    def test_returns_the_configured_response_for_every_invocation(self) -> None:
        model = FakeChatModel(response="결정 가능한 응답")

        first = model.invoke("첫 번째 요청")
        second = model.invoke("두 번째 요청")

        self.assertEqual(first.content, "결정 가능한 응답")
        self.assertEqual(second.content, "결정 가능한 응답")

    def test_records_each_invocation_for_caller_contract_assertions(self) -> None:
        model = FakeChatModel(response="응답")
        first_messages = [HumanMessage(content="첫 번째 요청")]
        second_messages = [HumanMessage(content="두 번째 요청")]

        model.invoke(first_messages)
        model.invoke(second_messages)

        self.assertEqual(model.received_messages, [first_messages, second_messages])
