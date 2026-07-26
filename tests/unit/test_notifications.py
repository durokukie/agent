"""Notification wire 값과 sender 공개 계약 테스트."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agent_system.notifications import (
    FakeNotificationSender,
    LoggingNotificationSender,
    Notification,
    NotificationChannel,
    NotificationDeliveryError,
)

NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)


def _notification() -> Notification:
    return Notification(
        notification_id="notification:task-1:4",
        task_id="task-1",
        task_version=4,
        status="WAITING_APPROVAL",
        channel=NotificationChannel.OPERATIONS,
        occurred_at=NOW,
        metadata={"plan_hash": "sha256:plan"},
    )


class NotificationValueTests(unittest.TestCase):
    """Wire payload의 안정성과 입력 격리를 검증한다."""

    def test_round_trips_a_copied_wire_payload(self) -> None:
        """필드 누락, enum 손실 또는 caller metadata aliasing을 잡는다."""

        source = {"plan_hash": "sha256:plan"}
        notification = Notification(
            notification_id="notification:task-1:4",
            task_id="task-1",
            task_version=4,
            status="WAITING_APPROVAL",
            channel=NotificationChannel.OPERATIONS,
            occurred_at=NOW,
            metadata=source,
        )
        source["secret"] = "should-not-leak"

        self.assertEqual(
            notification.to_payload(),
            {
                "notification_id": "notification:task-1:4",
                "task_id": "task-1",
                "task_version": 4,
                "status": "WAITING_APPROVAL",
                "channel": "operations",
                "occurred_at": "2026-07-27T01:00:00+00:00",
                "metadata": {"plan_hash": "sha256:plan"},
            },
        )
        self.assertEqual(
            Notification.from_payload(notification.to_payload()),
            notification,
        )

    def test_rejects_invalid_wire_values(self) -> None:
        """Naive 시각이나 bool version이 sender 경계까지 통과하는 버그를 잡는다."""

        with self.assertRaises(ValueError):
            Notification(
                notification_id="notification-1",
                task_id="task-1",
                task_version=True,
                status="COMPLETED",
                channel=NotificationChannel.OPERATIONS,
                occurred_at=NOW.replace(tzinfo=None),
            )


class NotificationSenderTests(unittest.IsolatedAsyncioTestCase):
    """실제 logging과 결정 가능한 fake sender 동작을 검증한다."""

    async def test_fake_sender_fails_a_bounded_number_then_records_success(
        self,
    ) -> None:
        """Fake가 실패 뒤에도 잘못 sent 처리하거나 무한 실패하는 버그를 잡는다."""

        sender = FakeNotificationSender(failures_before_success=1)
        notification = _notification()

        with self.assertRaises(NotificationDeliveryError):
            await sender.send(notification)
        await sender.send(notification)

        self.assertEqual(sender.attempts, (notification, notification))
        self.assertEqual(sender.sent, (notification,))

    async def test_logging_sender_emits_safe_identity_without_metadata(self) -> None:
        """원본 metadata나 비밀값이 log message에 노출되는 버그를 잡는다."""

        sender = LoggingNotificationSender(logger_name="agent_system.test.notification")

        with self.assertLogs("agent_system.test.notification", level="INFO") as logs:
            await sender.send(_notification())

        self.assertEqual(len(logs.output), 1)
        self.assertIn("notification:task-1:4", logs.output[0])
        self.assertIn("WAITING_APPROVAL", logs.output[0])
        self.assertNotIn("sha256:plan", logs.output[0])


if __name__ == "__main__":
    unittest.main()
