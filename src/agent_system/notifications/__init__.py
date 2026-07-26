"""알림 wire 값과 전달 adapter의 공개 interface."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol


class NotificationChannel(StrEnum):
    """MVP에서 지원하는 논리적 알림 channel."""

    OPERATIONS = "operations"


class NotificationDeliveryError(RuntimeError):
    """Sender가 알림을 전달하지 못했음을 나타내는 안정적인 오류."""


class NotificationLeaseLostError(RuntimeError):
    """전달 중 outbox lease가 다른 dispatcher로 넘어갔음을 나타낸다."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}는 비어 있을 수 없습니다.")
    return value


def _freeze_json(value: object) -> object:
    """JSON 호환 metadata를 재귀 복사해 불변 값으로 바꾼다."""

    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("metadata key는 비어 있지 않은 문자열이어야 합니다.")
            copied[key] = _freeze_json(item)
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("metadata는 JSON 호환 값이어야 합니다.")


def _thaw_json(value: object) -> object:
    """불변 metadata를 alias 없는 JSON 호환 값으로 복사한다."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Notification:
    """Adapter가 domain 객체 없이 소비할 수 있는 불변 알림 값."""

    notification_id: str
    task_id: str
    task_version: int
    status: str
    channel: NotificationChannel
    occurred_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.notification_id, "notification_id")
        _require_text(self.task_id, "task_id")
        if (
            isinstance(self.task_version, bool)
            or not isinstance(self.task_version, int)
            or self.task_version <= 0
        ):
            raise ValueError("task_version은 양의 정수여야 합니다.")
        _require_text(self.status, "status")
        if type(self.channel) is not NotificationChannel:
            raise ValueError("channel은 NotificationChannel이어야 합니다.")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("occurred_at에는 timezone-aware datetime이 필요합니다.")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata는 mapping이어야 합니다.")
        frozen = _freeze_json(self.metadata)
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "metadata", frozen)

    def to_payload(self) -> dict[str, object]:
        """Persistence와 sender 사이의 안정적인 JSON object를 반환한다."""

        return {
            "notification_id": self.notification_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "status": self.status,
            "channel": self.channel.value,
            "occurred_at": self.occurred_at.isoformat(),
            "metadata": _thaw_json(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Notification:
        """저장된 JSON object를 검증해 알림 값으로 복원한다."""

        if not isinstance(payload, Mapping):
            raise TypeError("notification payload는 mapping이어야 합니다.")
        try:
            occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
            channel = NotificationChannel(payload["channel"])
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise TypeError
            return cls(
                notification_id=payload["notification_id"],  # type: ignore[arg-type]
                task_id=payload["task_id"],  # type: ignore[arg-type]
                task_version=payload["task_version"],  # type: ignore[arg-type]
                status=payload["status"],  # type: ignore[arg-type]
                channel=channel,
                occurred_at=occurred_at,
                metadata=metadata,
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("notification payload가 올바르지 않습니다.") from None


class NotificationSender(Protocol):
    """외부 알림 전달 구현이 만족할 작은 async interface."""

    async def send(self, notification: Notification) -> None:
        """알림 한 건을 전달한다.

        실제 channel adapter는 crash 후 재전달에도 외부 effect가 중복되지 않도록
        ``notification.notification_id``를 idempotency key로 사용해야 한다.
        """


@dataclass(frozen=True, slots=True)
class NotificationClaim:
    """Outbox adapter가 한 dispatcher에게 부여한 전달 lease."""

    notification: Notification
    lease_token: str
    attempt_count: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationClaimBatchProgress:
    """Bounded poison skip batch가 실제 row를 처리했음을 나타낸다."""

    processed_count: int


class NotificationOutbox(Protocol):
    """Dispatcher가 persistence 구현에서 요구하는 lease/CAS interface."""

    async def claim(
        self,
        *,
        now: datetime,
        lease_duration: timedelta,
        lease_token: str,
    ) -> NotificationClaim | NotificationClaimBatchProgress | None:
        """현재 eligible한 알림 한 건의 lease를 얻는다."""

    async def mark_delivered(
        self,
        claim: NotificationClaim,
        *,
        at: datetime,
    ) -> None:
        """현재 lease의 성공을 확정한다."""

    async def renew(
        self,
        claim: NotificationClaim,
        *,
        at: datetime,
        lease_duration: timedelta,
    ) -> NotificationClaim:
        """현재 owner의 active lease를 연장한다."""

    async def mark_failed(
        self,
        claim: NotificationClaim,
        *,
        error_code: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> None:
        """실패와 다음 eligibility를 기록한다."""


class LoggingNotificationSender:
    """비밀 metadata 없이 알림 identity만 표준 log로 전달한다."""

    def __init__(self, *, logger_name: str = "agent_system.notifications") -> None:
        self._logger = logging.getLogger(logger_name)

    async def send(self, notification: Notification) -> None:
        self._logger.info(
            "notification delivered id=%s task_id=%s version=%d status=%s channel=%s",
            notification.notification_id,
            notification.task_id,
            notification.task_version,
            notification.status,
            notification.channel.value,
        )


class FakeNotificationSender:
    """지정한 횟수만 실패한 뒤 전달 기록을 보존하는 결정 가능한 fake."""

    def __init__(self, *, failures_before_success: int = 0) -> None:
        if (
            isinstance(failures_before_success, bool)
            or not isinstance(failures_before_success, int)
            or failures_before_success < 0
        ):
            raise ValueError("failures_before_success는 0 이상의 정수여야 합니다.")
        self._remaining_failures = failures_before_success
        self._attempts: list[Notification] = []
        self._sent: list[Notification] = []

    @property
    def attempts(self) -> tuple[Notification, ...]:
        return tuple(self._attempts)

    @property
    def sent(self) -> tuple[Notification, ...]:
        return tuple(self._sent)

    async def send(self, notification: Notification) -> None:
        self._attempts.append(notification)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise NotificationDeliveryError("fake_delivery_failed")
        self._sent.append(notification)


class NotificationDispatcher:
    """Lease claim과 bounded retry를 조정하는 단일 background dispatcher."""

    def __init__(
        self,
        *,
        outbox: NotificationOutbox,
        sender: NotificationSender,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        lease_duration: timedelta = timedelta(seconds=30),
        send_timeout: timedelta = timedelta(seconds=20),
        heartbeat_interval: timedelta = timedelta(seconds=5),
        base_retry_delay: timedelta = timedelta(seconds=30),
        max_retry_delay: timedelta = timedelta(minutes=5),
        poll_interval: float = 1.0,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration은 양수여야 합니다.")
        if send_timeout <= timedelta(0) or send_timeout >= lease_duration:
            raise ValueError(
                "send_timeout은 0보다 크고 lease_duration보다 작아야 합니다."
            )
        if (
            heartbeat_interval <= timedelta(0)
            or heartbeat_interval * 2 >= lease_duration
        ):
            raise ValueError(
                "heartbeat_interval은 양수이고 lease_duration의 절반보다 작아야 합니다."
            )
        if base_retry_delay <= timedelta(0):
            raise ValueError("base_retry_delay는 양수여야 합니다.")
        if max_retry_delay < base_retry_delay:
            raise ValueError("max_retry_delay는 base_retry_delay 이상이어야 합니다.")
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("poll_interval은 양수여야 합니다.")
        self._outbox = outbox
        self._sender = sender
        self._clock = clock
        self._id_factory = id_factory
        self._lease_duration = lease_duration
        self._send_timeout = send_timeout
        self._heartbeat_interval = heartbeat_interval
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay
        self._poll_interval = float(poll_interval)
        self._stop_event = asyncio.Event()
        self._dispatch_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def start(self) -> None:
        """Idempotent하게 polling worker를 시작한다."""

        if self.is_running:
            return
        self._stop_event.clear()
        self._worker = asyncio.create_task(
            self._run(),
            name="notification-dispatcher",
        )

    async def stop(self) -> None:
        """새 poll을 중단하고 진행 중인 전달이 끝날 때까지 기다린다."""

        worker = self._worker
        if worker is None:
            return
        self._stop_event.set()
        await worker
        self._worker = None

    async def drain(self) -> None:
        """현재 시각에 eligible한 모든 알림을 처리한다."""

        while await self.dispatch_once():
            pass

    async def dispatch_once(self) -> bool:
        """Eligible 알림 한 건을 claim해 성공 또는 retry 상태로 확정한다."""

        async with self._dispatch_lock:
            claimed_at = self._clock()
            claim = await self._outbox.claim(
                now=claimed_at,
                lease_duration=self._lease_duration,
                lease_token=self._id_factory(),
            )
            if claim is None:
                return False
            if isinstance(claim, NotificationClaimBatchProgress):
                return True
            delivered = await self._send_with_heartbeat(claim)
            if delivered is None:
                return True
            if not delivered:
                failed_at = self._clock()
                try:
                    await self._outbox.mark_failed(
                        claim,
                        error_code="notification_delivery_failed",
                        at=failed_at,
                        next_attempt_at=(
                            failed_at + self._retry_delay(claim.attempt_count)
                        ),
                    )
                except NotificationLeaseLostError:
                    pass
            else:
                try:
                    await self._outbox.mark_delivered(claim, at=self._clock())
                except NotificationLeaseLostError:
                    pass
            return True

    async def _send_with_heartbeat(self, claim: NotificationClaim) -> bool | None:
        """Active sender가 끝날 때까지 lease를 갱신하고 전달 결과를 반환한다."""

        sender_task = asyncio.create_task(self._sender.send(claim.notification))
        loop = asyncio.get_running_loop()
        timeout_at = loop.time() + self._send_timeout.total_seconds()
        heartbeat_at = loop.time() + self._heartbeat_interval.total_seconds()
        timed_out = False
        try:
            while not sender_task.done():
                deadline = heartbeat_at if timed_out else min(timeout_at, heartbeat_at)
                await asyncio.wait(
                    {sender_task},
                    timeout=max(0.0, deadline - loop.time()),
                )
                if sender_task.done():
                    break
                current = loop.time()
                if current >= heartbeat_at:
                    try:
                        claim = await self._outbox.renew(
                            claim,
                            at=self._clock(),
                            lease_duration=self._lease_duration,
                        )
                    except NotificationLeaseLostError:
                        await self._cancel_sender_task(sender_task)
                        return None
                    except Exception:
                        await self._cancel_sender_task(sender_task)
                        raise
                    heartbeat_at = (
                        loop.time() + self._heartbeat_interval.total_seconds()
                    )
                if not timed_out and current >= timeout_at:
                    timed_out = True
                    sender_task.cancel()
            try:
                await sender_task
            except asyncio.CancelledError:
                if not timed_out:
                    raise
                return False
            except Exception:  # noqa: BLE001 - provider 오류 원문은 저장하지 않는다.
                return False
            return True
        except asyncio.CancelledError:
            await self._cancel_sender_task(sender_task)
            raise

    @staticmethod
    async def _cancel_sender_task(task: asyncio.Task[None]) -> None:
        """Sender task를 취소하고 종료 결과를 회수한다."""

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                dispatched = await self.dispatch_once()
            except Exception:  # noqa: BLE001 - 한 poison row가 polling을 종료하지 않는다.
                dispatched = False
            if dispatched:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                continue

    def _retry_delay(self, attempt_count: int) -> timedelta:
        exponent = min(max(attempt_count - 1, 0), 16)
        seconds = min(
            self._base_retry_delay.total_seconds() * (2**exponent),
            self._max_retry_delay.total_seconds(),
        )
        return timedelta(seconds=seconds)


__all__ = [
    "FakeNotificationSender",
    "LoggingNotificationSender",
    "Notification",
    "NotificationChannel",
    "NotificationClaim",
    "NotificationClaimBatchProgress",
    "NotificationDeliveryError",
    "NotificationDispatcher",
    "NotificationLeaseLostError",
    "NotificationOutbox",
    "NotificationSender",
]
