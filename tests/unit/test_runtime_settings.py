"""Runtime 설정이 안전하고 유효한 실행 입력만 만들도록 검증한다."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_system.config import RuntimeConfigurationError, RuntimeSettings
from agent_system.models import ModelSettings


class RuntimeSettingsTests(unittest.TestCase):
    """환경 입력을 runtime의 불변 composition 설정으로 변환한다."""

    def test_reads_runtime_values_and_composes_model_settings_from_same_mapping(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "agent-system.sqlite3"
            values = self._valid_environment(db_path) | {
                "AGENT_MAX_RUNS": "7",
                "AGENT_QUEUE_CAPACITY": "250",
                "AGENT_WORKER_COUNT": "4",
                "MODEL_TIMEOUT_SECONDS": "12.5",
            }

            settings = RuntimeSettings.from_env(values)

        self.assertEqual(settings.db_path, db_path)
        self.assertEqual(settings.max_agent_runs, 7)
        self.assertEqual(settings.queue_capacity, 250)
        self.assertEqual(settings.worker_count, 4)
        self.assertEqual(
            settings.model_settings,
            ModelSettings(
                provider="upstage",
                model_name="solar-pro2",
                api_key="test-api-key",
                timeout_seconds=12.5,
            ),
        )

    def test_uses_documented_runtime_defaults(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = RuntimeSettings.from_env(
                self._valid_environment(Path(temporary_directory) / "agent.sqlite3")
            )

        self.assertEqual(settings.max_agent_runs, 3)
        self.assertEqual(settings.queue_capacity, 100)
        self.assertEqual(settings.worker_count, 1)

    def test_is_immutable_after_configuration_is_loaded(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            settings = RuntimeSettings.from_env(
                self._valid_environment(Path(temporary_directory) / "agent.sqlite3")
            )

        with self.assertRaises(AttributeError):
            settings.max_agent_runs = 8  # type: ignore[misc]

    def test_rejects_missing_or_blank_database_path(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            valid_values = self._valid_environment(
                Path(temporary_directory) / "agent.sqlite3"
            )
            for db_path in (None, "", "   "):
                values = dict(valid_values)
                if db_path is None:
                    values.pop("AGENT_SYSTEM_DB_PATH")
                else:
                    values["AGENT_SYSTEM_DB_PATH"] = db_path

                with (
                    self.subTest(db_path=db_path),
                    self.assertRaises(RuntimeConfigurationError),
                ):
                    RuntimeSettings.from_env(values)

    def test_rejects_database_path_that_is_a_directory_or_has_missing_parent(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            for db_path in (parent, parent / "missing" / "agent.sqlite3"):
                with (
                    self.subTest(db_path=db_path),
                    self.assertRaises(RuntimeConfigurationError),
                ):
                    RuntimeSettings.from_env(self._valid_environment(db_path))

    def test_rejects_non_positive_or_malformed_runtime_numeric_values(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            valid_values = self._valid_environment(
                Path(temporary_directory) / "agent.sqlite3"
            )
            invalid_values = ("", "0", "-1", "1.5", "true", " 2", "+2")
            for setting_name in (
                "AGENT_MAX_RUNS",
                "AGENT_QUEUE_CAPACITY",
                "AGENT_WORKER_COUNT",
            ):
                for value in invalid_values:
                    with (
                        self.subTest(setting_name=setting_name, value=value),
                        self.assertRaises(RuntimeConfigurationError),
                    ):
                        RuntimeSettings.from_env(valid_values | {setting_name: value})

    def test_rejects_non_string_runtime_environment_values(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            valid_values = self._valid_environment(
                Path(temporary_directory) / "agent.sqlite3"
            )
            for setting_name, value in (
                ("AGENT_SYSTEM_DB_PATH", True),
                ("AGENT_MAX_RUNS", True),
                ("AGENT_QUEUE_CAPACITY", 100),
                ("AGENT_WORKER_COUNT", False),
            ):
                with (
                    self.subTest(setting_name=setting_name, value=value),
                    self.assertRaises(RuntimeConfigurationError),
                ):
                    RuntimeSettings.from_env(valid_values | {setting_name: value})

    def test_rejects_values_over_runtime_safety_limits(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            valid_values = self._valid_environment(
                Path(temporary_directory) / "agent.sqlite3"
            )
            for setting_name, value in (
                ("AGENT_MAX_RUNS", "1001"),
                ("AGENT_QUEUE_CAPACITY", "10001"),
                ("AGENT_WORKER_COUNT", "129"),
            ):
                with (
                    self.subTest(setting_name=setting_name),
                    self.assertRaises(RuntimeConfigurationError),
                ):
                    RuntimeSettings.from_env(valid_values | {setting_name: value})

    def test_never_exposes_api_key_in_repr_or_configuration_error(self) -> None:
        api_key = "api-key-that-must-not-be-disclosed"
        with TemporaryDirectory() as temporary_directory:
            settings = RuntimeSettings.from_env(
                self._valid_environment(
                    Path(temporary_directory) / "agent.sqlite3",
                    api_key=api_key,
                )
            )
            invalid_values = self._valid_environment(
                Path(temporary_directory) / "agent.sqlite3",
                api_key=api_key,
            ) | {"AGENT_MAX_RUNS": "0"}

            with self.assertRaises(RuntimeConfigurationError) as context:
                RuntimeSettings.from_env(invalid_values)

        self.assertNotIn(api_key, repr(settings))
        self.assertNotIn(api_key, str(context.exception))

    @staticmethod
    def _valid_environment(
        db_path: Path, *, api_key: str = "test-api-key"
    ) -> dict[str, str]:
        return {
            "AGENT_SYSTEM_DB_PATH": str(db_path),
            "MODEL_PROVIDER": "upstage",
            "MODEL_NAME": "solar-pro2",
            "UPSTAGE_API_KEY": api_key,
        }
