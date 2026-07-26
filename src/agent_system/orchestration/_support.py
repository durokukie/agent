"""Orchestration 생명주기 모델이 공유하는 오류와 경계 검증."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar


class LifecycleError(Exception):
    """생명주기 불변 조건 위반의 기본 오류다."""


class InvalidLifecycleValueError(LifecycleError, ValueError):
    """생명주기 snapshot의 값이 유효하지 않을 때 발생한다."""


class InvalidStatusTransitionError(LifecycleError):
    """허용되지 않은 Task 상태 전이를 요청했을 때 발생한다."""


class InvalidPhaseTransitionError(LifecycleError):
    """허용되지 않은 WorkflowRun phase 전이를 요청했을 때 발생한다."""


class ExecutionBudgetExhaustedError(LifecycleError):
    """WorkflowRun이 허용된 Agent 호출 횟수를 모두 소비했을 때 발생한다."""


class AgentRunError(LifecycleError):
    """AgentRun 완료 불변 조건 위반의 기본 오류다."""


class AgentRunAgentMismatchError(AgentRunError):
    """호출한 Agent와 완료 결과의 Agent가 다를 때 발생한다."""


class AgentRunAlreadyCompletedError(AgentRunError):
    """완료된 AgentRun을 다시 완료하려 할 때 발생한다."""


class AgentRunOwnershipError(AgentRunError):
    """AgentRun이 소유 WorkflowRun의 실행 권한과 맞지 않을 때 발생한다."""


class PlanRequiredError(LifecycleError):
    """승인 대기 전이에 필요한 plan이 없을 때 발생한다."""


class PlanUpdateNotAllowedError(LifecycleError):
    """현재 Task 상태에서 plan을 바꿀 수 없을 때 발생한다."""


class ApprovalError(LifecycleError):
    """Approval이 현재 Task snapshot과 호환되지 않을 때 발생한다."""


class ApprovalRequiredError(ApprovalError):
    """승인 없이 승인 대기 Task를 재개하려 할 때 발생한다."""


class ApprovalNotAllowedError(ApprovalError):
    """승인 대기 상태가 아닌 Task에 Approval을 만들 때 발생한다."""


class ApprovalTaskMismatchError(ApprovalError):
    """Approval의 Task 식별자가 현재 Task와 다를 때 발생한다."""


class PlanChangedError(ApprovalError):
    """Approval 이후 Task plan이 변경되었을 때 발생한다."""


class StaleApprovalError(ApprovalError):
    """Approval의 Task version이 현재 snapshot보다 오래되었을 때 발생한다."""


def require_aware(value: datetime, *, field_name: str) -> None:
    """값이 timezone-aware datetime인지 검증한다."""

    if not isinstance(value, datetime):
        raise InvalidLifecycleValueError(f"{field_name}은 datetime이어야 합니다.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidLifecycleValueError(
            f"{field_name}에는 timezone-aware datetime이 필요합니다."
        )


def require_not_before(value: datetime, earliest: datetime, *, field_name: str) -> None:
    """값이 비교 기준보다 이른 UTC instant가 아닌지 검증한다."""

    require_aware(value, field_name=field_name)
    require_aware(earliest, field_name="비교 기준 시각")
    if value.astimezone(UTC) < earliest.astimezone(UTC):
        raise InvalidLifecycleValueError(
            f"{field_name}은 이전 snapshot 시각보다 빠를 수 없습니다."
        )


_EnumType = TypeVar("_EnumType", bound=StrEnum)


def snapshot_value(snapshot: Mapping[str, object], field_name: str) -> object:
    """필수 snapshot 필드를 읽고 누락 오류를 정규화한다."""

    if not isinstance(snapshot, Mapping):
        raise InvalidLifecycleValueError("snapshot은 mapping이어야 합니다.")
    try:
        return snapshot[field_name]
    except KeyError as error:
        raise InvalidLifecycleValueError(
            f"snapshot 필드가 없습니다: {field_name}"
        ) from error


def snapshot_string(snapshot: Mapping[str, object], field_name: str) -> str:
    """필수 문자열 필드를 읽는다."""

    value = snapshot_value(snapshot, field_name)
    if not isinstance(value, str):
        raise InvalidLifecycleValueError(f"{field_name}은 문자열이어야 합니다.")
    return value


def snapshot_optional_string(
    snapshot: Mapping[str, object], field_name: str
) -> str | None:
    """nullable 문자열 필드를 읽는다."""

    value = snapshot_value(snapshot, field_name)
    if value is not None and not isinstance(value, str):
        raise InvalidLifecycleValueError(
            f"{field_name}은 문자열 또는 null이어야 합니다."
        )
    return value


def snapshot_integer(snapshot: Mapping[str, object], field_name: str) -> int:
    """bool을 제외한 정수 필드를 읽는다."""

    value = snapshot_value(snapshot, field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidLifecycleValueError(f"{field_name}은 정수여야 합니다.")
    return value


def snapshot_datetime(
    snapshot: Mapping[str, object],
    field_name: str,
    *,
    optional: bool = False,
) -> datetime | None:
    """ISO 8601 datetime 필드를 읽는다."""

    value = snapshot_value(snapshot, field_name)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise InvalidLifecycleValueError(
            f"{field_name}은 ISO 8601 문자열이어야 합니다."
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidLifecycleValueError(
            f"{field_name}의 ISO 8601 형식이 올바르지 않습니다."
        ) from error


def snapshot_enum(
    snapshot: Mapping[str, object],
    field_name: str,
    enum_type: type[_EnumType],
) -> _EnumType:
    """문자열 필드를 지정된 StrEnum으로 변환한다."""

    value = snapshot_string(snapshot, field_name)
    try:
        return enum_type(value)
    except ValueError as error:
        raise InvalidLifecycleValueError(
            f"{field_name}이 알려진 enum 값이 아닙니다."
        ) from error


def snapshot_mapping(
    snapshot: Mapping[str, object], field_name: str
) -> Mapping[str, object]:
    """중첩 mapping 필드를 읽는다."""

    value = snapshot_value(snapshot, field_name)
    if not isinstance(value, Mapping):
        raise InvalidLifecycleValueError(f"{field_name}은 mapping이어야 합니다.")
    return value


def snapshot_list(snapshot: Mapping[str, object], field_name: str) -> list[object]:
    """JSON array 필드를 읽는다."""

    value = snapshot_value(snapshot, field_name)
    if not isinstance(value, list):
        raise InvalidLifecycleValueError(f"{field_name}은 list여야 합니다.")
    return value


__all__ = [
    "AgentRunAgentMismatchError",
    "AgentRunAlreadyCompletedError",
    "AgentRunError",
    "AgentRunOwnershipError",
    "ApprovalError",
    "ApprovalNotAllowedError",
    "ApprovalRequiredError",
    "ApprovalTaskMismatchError",
    "ExecutionBudgetExhaustedError",
    "InvalidLifecycleValueError",
    "InvalidPhaseTransitionError",
    "InvalidStatusTransitionError",
    "LifecycleError",
    "PlanChangedError",
    "PlanRequiredError",
    "PlanUpdateNotAllowedError",
    "StaleApprovalError",
    "require_aware",
    "require_not_before",
    "snapshot_datetime",
    "snapshot_enum",
    "snapshot_integer",
    "snapshot_list",
    "snapshot_mapping",
    "snapshot_optional_string",
    "snapshot_string",
]
