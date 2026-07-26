"""Notification outbox protocol을 구현하는 SQLite async adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from agent_system.notifications import (
    Notification,
    NotificationChannel,
    NotificationClaim,
    NotificationLeaseLostError,
)

from ._store import SQLiteStore
from ._values import OptimisticConcurrencyError, OutboxMessage

_NOTIFICATION_TOPIC = "task.status_changed"
_INVALID_PAYLOAD_BACKOFF = timedelta(minutes=5)
_MAX_INVALID_PAYLOAD_SKIPS = 100
_NOTIFIED_STATUSES = frozenset(
    {
        "WAITING_APPROVAL",
        "COMPLETED",
        "REJECTED",
        "FAILED",
        "ESCALATED",
        "CANCELLED",
    }
)
_PAYLOAD_FIELDS = frozenset(
    {
        "notification_id",
        "task_id",
        "task_version",
        "status",
        "channel",
        "occurred_at",
        "metadata",
    }
)


class SQLiteNotificationOutbox:
    """SQLiteStore의 짧은 동기 transaction을 async lease interface로 감싼다."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def claim(
        self,
        *,
        now: datetime,
        lease_duration: timedelta,
        lease_token: str,
    ) -> NotificationClaim | None:
        for _invalid_count in range(_MAX_INVALID_PAYLOAD_SKIPS):
            message = await asyncio.to_thread(
                self._store.claim_outbox,
                now=now,
                lease_duration=lease_duration,
                lease_token=lease_token,
                topic=_NOTIFICATION_TOPIC,
            )
            if message is None:
                return None
            if message.lease_token is None or message.lease_expires_at is None:
                raise ValueError("Claim된 outbox에 lease 정보가 없습니다.")
            try:
                notification = Notification.from_payload(message.payload)
                self._validate_binding(message, notification)
            except (TypeError, ValueError):
                await asyncio.to_thread(
                    self._store.record_outbox_failure,
                    message.outbox_id,
                    lease_token=message.lease_token,
                    error_code="notification_payload_invalid",
                    at=now,
                    next_attempt_at=now + _INVALID_PAYLOAD_BACKOFF,
                )
                continue
            return NotificationClaim(
                notification=notification,
                lease_token=message.lease_token,
                attempt_count=message.attempt_count,
                lease_expires_at=message.lease_expires_at,
            )
        return None

    @staticmethod
    def _validate_binding(
        message: OutboxMessage,
        notification: Notification,
    ) -> None:
        expected_id = f"notification:{notification.task_id}:{notification.task_version}"
        if (
            set(message.payload) != _PAYLOAD_FIELDS
            or message.outbox_id != expected_id
            or notification.notification_id != expected_id
            or message.task_id != notification.task_id
            or message.task_version != notification.task_version
            or notification.status not in _NOTIFIED_STATUSES
            or notification.channel is not NotificationChannel.OPERATIONS
            or notification.occurred_at != message.created_at
        ):
            raise ValueError("notification binding이 올바르지 않습니다.")
        metadata = notification.metadata
        if notification.status == "WAITING_APPROVAL":
            if (
                set(metadata) != {"plan_hash"}
                or not isinstance(metadata["plan_hash"], str)
                or not metadata["plan_hash"].strip()
            ):
                raise ValueError("approval notification metadata가 올바르지 않습니다.")
        elif metadata:
            raise ValueError("terminal notification metadata는 비어 있어야 합니다.")

    async def renew(
        self,
        claim: NotificationClaim,
        *,
        at: datetime,
        lease_duration: timedelta,
    ) -> NotificationClaim:
        try:
            renewed = await asyncio.to_thread(
                self._store.renew_outbox_lease,
                claim.notification.notification_id,
                lease_token=claim.lease_token,
                at=at,
                lease_duration=lease_duration,
            )
        except OptimisticConcurrencyError:
            raise NotificationLeaseLostError(
                "notification lease를 잃었습니다."
            ) from None
        if renewed.lease_expires_at is None:
            raise NotificationLeaseLostError("notification lease 만료 시각이 없습니다.")
        return NotificationClaim(
            notification=claim.notification,
            lease_token=claim.lease_token,
            attempt_count=claim.attempt_count,
            lease_expires_at=renewed.lease_expires_at,
        )

    async def mark_delivered(
        self,
        claim: NotificationClaim,
        *,
        at: datetime,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._store.mark_outbox_delivered,
                claim.notification.notification_id,
                lease_token=claim.lease_token,
                at=at,
            )
        except OptimisticConcurrencyError:
            raise NotificationLeaseLostError(
                "notification lease를 잃었습니다."
            ) from None

    async def mark_failed(
        self,
        claim: NotificationClaim,
        *,
        error_code: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._store.record_outbox_failure,
                claim.notification.notification_id,
                lease_token=claim.lease_token,
                error_code=error_code,
                at=at,
                next_attempt_at=next_attempt_at,
            )
        except OptimisticConcurrencyError:
            raise NotificationLeaseLostError(
                "notification lease를 잃었습니다."
            ) from None


__all__ = ["SQLiteNotificationOutbox"]
