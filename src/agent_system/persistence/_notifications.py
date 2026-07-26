"""Notification outbox protocol을 구현하는 SQLite async adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from agent_system.notifications import (
    Notification,
    NotificationClaim,
    NotificationLeaseLostError,
)

from ._store import SQLiteStore
from ._values import OptimisticConcurrencyError

_NOTIFICATION_TOPIC = "task.status_changed"
_INVALID_PAYLOAD_BACKOFF = timedelta(minutes=5)


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
        except (TypeError, ValueError):
            await asyncio.to_thread(
                self._store.record_outbox_failure,
                message.outbox_id,
                lease_token=message.lease_token,
                error_code="notification_payload_invalid",
                at=now,
                next_attempt_at=now + _INVALID_PAYLOAD_BACKOFF,
            )
            return None
        return NotificationClaim(
            notification=notification,
            lease_token=message.lease_token,
            attempt_count=message.attempt_count,
            lease_expires_at=message.lease_expires_at,
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
