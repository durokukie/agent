"""Runtime이 사용할 검증된 애플리케이션 설정 인터페이스."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_system.models import ModelSettings


class RuntimeConfigurationError(ValueError):
    """Runtime 설정이 실행 불변 조건을 만족하지 않을 때 발생한다."""


_POSITIVE_INTEGER_PATTERN = re.compile(r"[1-9][0-9]*\Z")
_MAX_AGENT_RUNS = 1_000
_MAX_QUEUE_CAPACITY = 10_000
_MAX_WORKER_COUNT = 128
_MAX_NOTIFICATION_TIMING_SECONDS = 3_600


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Runtime composition root에 주입할 불변 실행 설정이다."""

    db_path: Path
    model_settings: ModelSettings
    max_agent_runs: int = 3
    queue_capacity: int = 100
    worker_count: int = 1
    notification_lease_seconds: int = 30
    notification_send_timeout_seconds: int = 20
    notification_heartbeat_seconds: int = 5

    def __post_init__(self) -> None:
        """직접 생성도 환경 입력과 동일한 불변 조건으로 제한한다."""

        if not isinstance(self.db_path, Path):
            raise RuntimeConfigurationError(
                "AGENT_SYSTEM_DB_PATH는 파일 경로여야 합니다."
            )
        _validate_db_path(self.db_path)
        if not isinstance(self.model_settings, ModelSettings):
            raise RuntimeConfigurationError(
                "model_settings는 ModelSettings여야 합니다."
            )
        _validate_positive_integer(
            "AGENT_MAX_RUNS", self.max_agent_runs, _MAX_AGENT_RUNS
        )
        _validate_positive_integer(
            "AGENT_QUEUE_CAPACITY", self.queue_capacity, _MAX_QUEUE_CAPACITY
        )
        _validate_positive_integer(
            "AGENT_WORKER_COUNT", self.worker_count, _MAX_WORKER_COUNT
        )
        _validate_positive_integer(
            "AGENT_NOTIFICATION_LEASE_SECONDS",
            self.notification_lease_seconds,
            _MAX_NOTIFICATION_TIMING_SECONDS,
        )
        _validate_positive_integer(
            "AGENT_NOTIFICATION_SEND_TIMEOUT_SECONDS",
            self.notification_send_timeout_seconds,
            _MAX_NOTIFICATION_TIMING_SECONDS,
        )
        _validate_positive_integer(
            "AGENT_NOTIFICATION_HEARTBEAT_SECONDS",
            self.notification_heartbeat_seconds,
            _MAX_NOTIFICATION_TIMING_SECONDS,
        )
        if self.notification_send_timeout_seconds >= self.notification_lease_seconds:
            raise RuntimeConfigurationError(
                "AGENT_NOTIFICATION_SEND_TIMEOUT_SECONDS는 lease보다 작아야 합니다."
            )
        if self.notification_heartbeat_seconds * 2 >= self.notification_lease_seconds:
            raise RuntimeConfigurationError(
                "AGENT_NOTIFICATION_HEARTBEAT_SECONDS는 lease 절반보다 작아야 합니다."
            )

    @classmethod
    def from_env(cls, mapping: Mapping[str, str] | None = None) -> RuntimeSettings:
        """같은 환경 mapping에서 runtime 및 model 설정을 함께 생성한다."""

        values = os.environ if mapping is None else mapping
        db_path = _parse_db_path(values.get("AGENT_SYSTEM_DB_PATH"))
        return cls(
            db_path=db_path,
            model_settings=ModelSettings.from_env(values),
            max_agent_runs=_parse_positive_integer(
                values.get("AGENT_MAX_RUNS", "3"),
                "AGENT_MAX_RUNS",
                _MAX_AGENT_RUNS,
            ),
            queue_capacity=_parse_positive_integer(
                values.get("AGENT_QUEUE_CAPACITY", "100"),
                "AGENT_QUEUE_CAPACITY",
                _MAX_QUEUE_CAPACITY,
            ),
            worker_count=_parse_positive_integer(
                values.get("AGENT_WORKER_COUNT", "1"),
                "AGENT_WORKER_COUNT",
                _MAX_WORKER_COUNT,
            ),
            notification_lease_seconds=_parse_positive_integer(
                values.get("AGENT_NOTIFICATION_LEASE_SECONDS", "30"),
                "AGENT_NOTIFICATION_LEASE_SECONDS",
                _MAX_NOTIFICATION_TIMING_SECONDS,
            ),
            notification_send_timeout_seconds=_parse_positive_integer(
                values.get("AGENT_NOTIFICATION_SEND_TIMEOUT_SECONDS", "20"),
                "AGENT_NOTIFICATION_SEND_TIMEOUT_SECONDS",
                _MAX_NOTIFICATION_TIMING_SECONDS,
            ),
            notification_heartbeat_seconds=_parse_positive_integer(
                values.get("AGENT_NOTIFICATION_HEARTBEAT_SECONDS", "5"),
                "AGENT_NOTIFICATION_HEARTBEAT_SECONDS",
                _MAX_NOTIFICATION_TIMING_SECONDS,
            ),
        )


def _parse_db_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigurationError("AGENT_SYSTEM_DB_PATH는 비어 있을 수 없습니다.")
    try:
        db_path = Path(value)
    except (TypeError, ValueError) as error:
        raise RuntimeConfigurationError(
            "AGENT_SYSTEM_DB_PATH는 유효한 파일 경로여야 합니다."
        ) from error
    _validate_db_path(db_path)
    return db_path


def _validate_db_path(db_path: Path) -> None:
    try:
        if db_path.exists() and not db_path.is_file():
            raise RuntimeConfigurationError(
                "AGENT_SYSTEM_DB_PATH는 디렉터리가 아닌 SQLite 파일 경로여야 합니다."
            )
        if not db_path.parent.is_dir():
            raise RuntimeConfigurationError(
                "AGENT_SYSTEM_DB_PATH의 부모 디렉터리가 존재해야 합니다."
            )
    except OSError as error:
        raise RuntimeConfigurationError(
            "AGENT_SYSTEM_DB_PATH를 확인할 수 없습니다."
        ) from error


def _parse_positive_integer(value: object, setting_name: str, upper_bound: int) -> int:
    if not isinstance(value, str) or not _POSITIVE_INTEGER_PATTERN.fullmatch(value):
        raise RuntimeConfigurationError(f"{setting_name}는 양의 정수여야 합니다.")
    return _validate_positive_integer(setting_name, int(value), upper_bound)


def _validate_positive_integer(
    setting_name: str,
    value: object,
    upper_bound: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeConfigurationError(f"{setting_name}는 양의 정수여야 합니다.")
    if value > upper_bound:
        raise RuntimeConfigurationError(
            f"{setting_name}는 {upper_bound} 이하여야 합니다."
        )
    return value


__all__ = ["RuntimeConfigurationError", "RuntimeSettings"]
