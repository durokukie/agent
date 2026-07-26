"""Notification outbox protocol을 구현하는 SQLite async adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from agent_system.notifications import Notification, NotificationClaim

from ._store import SQLiteStore


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
        )
        if message is None:
            return None
        notification = Notification.from_payload(message.payload)
        if message.lease_token is None or message.lease_expires_at is None:
            raise ValueError("Claim된 outbox에 lease 정보가 없습니다.")
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
        await asyncio.to_thread(
            self._store.mark_outbox_delivered,
            claim.notification.notification_id,
            lease_token=claim.lease_token,
            at=at,
        )

    async def mark_failed(
        self,
        claim: NotificationClaim,
        *,
        error_code: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._store.record_outbox_failure,
            claim.notification.notification_id,
            lease_token=claim.lease_token,
            error_code=error_code,
            at=at,
            next_attempt_at=next_attempt_at,
        )


__all__ = ["SQLiteNotificationOutbox"]
