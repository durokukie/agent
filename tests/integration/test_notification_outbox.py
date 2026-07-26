"""Task 전이와 notification outbox의 SQLite 통합 계약."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_system.notifications import (
    FakeNotificationSender,
    Notification,
    NotificationClaim,
    NotificationDispatcher,
    NotificationLeaseLostError,
)
from agent_system.orchestration import ApprovalResponse, Status, Task
from agent_system.persistence import (
    InvalidPersistenceValueError,
    OptimisticConcurrencyError,
    OutboxDraft,
    OutboxStatus,
    PersistenceConflictError,
    SQLiteNotificationOutbox,
    SQLiteStore,
    TaskEventDraft,
    upgrade_database,
)

NOW = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
NOTIFIED = frozenset(
    {
        Status.WAITING_APPROVAL,
        Status.COMPLETED,
        Status.REJECTED,
        Status.FAILED,
        Status.ESCALATED,
        Status.CANCELLED,
    }
)


class _BlockingNotificationSender:
    """Timeout 전에는 반환하지 않고 cancellation을 기록하는 실제 async sender fake."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.attempt_count = 0

    async def send(self, _notification: Notification) -> None:
        self.attempt_count += 1
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _CancellationResistantSender:
    """Timeout cancellation 뒤에도 외부 호출이 끝날 때까지 active인 sender fake."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.attempt_count = 0
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.attempt_count += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        self.sent.append(notification)


class _StaleFinalizationOutbox:
    """Sender 성공 직후 lease 소유권을 잃는 경합을 재현한다."""

    def __init__(self, notification: Notification) -> None:
        self._claim = NotificationClaim(
            notification=notification,
            lease_token="stale-lease",
            attempt_count=1,
            lease_expires_at=notification.occurred_at + timedelta(seconds=30),
        )

    async def claim(self, **_kwargs: object) -> NotificationClaim | None:
        claim, self._claim = self._claim, None
        return claim

    async def mark_delivered(self, *_args: object, **_kwargs: object) -> None:
        raise NotificationLeaseLostError("stale")

    async def mark_failed(self, *_args: object, **_kwargs: object) -> None:
        raise NotificationLeaseLostError("stale")


class _LeaseLosingOutbox(_StaleFinalizationOutbox):
    """첫 heartbeat에서 lease 소유권을 잃는 outbox fake."""

    def __init__(self, notification: Notification) -> None:
        super().__init__(notification)
        self.finalized = False

    async def renew(self, *_args: object, **_kwargs: object) -> NotificationClaim:
        raise NotificationLeaseLostError("lost-during-send")

    async def mark_delivered(self, *_args: object, **_kwargs: object) -> None:
        self.finalized = True

    async def mark_failed(self, *_args: object, **_kwargs: object) -> None:
        self.finalized = True


class _RenewalFailingOutbox:
    """실제 SQLite claim/finalize를 유지하고 heartbeat dependency만 실패시킨다."""

    def __init__(self, delegate: SQLiteNotificationOutbox) -> None:
        self._delegate = delegate
        self.renew_failed = asyncio.Event()

    async def claim(self, **kwargs: object) -> NotificationClaim | None:
        return await self._delegate.claim(**kwargs)  # type: ignore[arg-type]

    async def renew(self, *_args: object, **_kwargs: object) -> NotificationClaim:
        self.renew_failed.set()
        raise RuntimeError("heartbeat-database-unavailable")

    async def mark_delivered(self, *args: object, **kwargs: object) -> None:
        await self._delegate.mark_delivered(*args, **kwargs)  # type: ignore[arg-type]

    async def mark_failed(self, *args: object, **kwargs: object) -> None:
        await self._delegate.mark_failed(*args, **kwargs)  # type: ignore[arg-type]


class _CleanupTrackingSender:
    """Cancellation 시 정리가 끝났는지 관찰하는 sender fake."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.active_count = 0

    async def send(self, _notification: Notification) -> None:
        self.active_count += 1
        try:
            await self.release.wait()
        finally:
            self.active_count -= 1
            self.cleaned.set()


class TransactionalNotificationOutboxTests(unittest.TestCase):
    """Task snapshot과 자동 알림 의도가 같은 transaction을 공유하는지 검증한다."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "notifications.sqlite3"
        upgrade_database(self.database_path)
        self.store = SQLiteStore(self.database_path)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_enqueues_only_configured_statuses_with_safe_wire_payload(self) -> None:
        """대상 누락, RUNNING 오발행, input 유출 또는 unstable payload를 잡는다."""

        for offset, status in enumerate(sorted(NOTIFIED, key=lambda item: item.value)):
            received = Task.receive(
                task_id=f"task-{status.value.lower()}",
                input="token=super-secret original webhook body",
                at=NOW + timedelta(minutes=offset),
            )
            running = received.transition(
                Status.RUNNING,
                at=received.updated_at + timedelta(seconds=1),
            )
            self.store.create_task(
                received,
                event=TaskEventDraft(
                    f"event:{received.task_id}:1",
                    "TASK_RECEIVED",
                    {},
                    received.updated_at,
                ),
            )
            self.store.save_task(
                running,
                expected_version=1,
                event=TaskEventDraft(
                    f"event:{received.task_id}:2",
                    "TASK_STARTED",
                    {},
                    running.updated_at,
                ),
            )
            if status is Status.WAITING_APPROVAL:
                planned = running.update_plan(
                    "sha256:plan",
                    at=running.updated_at + timedelta(seconds=1),
                )
                self.store.save_task(
                    planned,
                    expected_version=2,
                    event=TaskEventDraft(
                        f"event:{received.task_id}:3",
                        "TASK_PLAN_UPDATED",
                        {"plan_hash": "sha256:plan"},
                        planned.updated_at,
                    ),
                )
                target = planned.transition(
                    status,
                    at=planned.updated_at + timedelta(seconds=1),
                )
                expected_version = 3
            else:
                target = running.transition(
                    status,
                    at=running.updated_at + timedelta(seconds=1),
                )
                expected_version = 2
            self.store.save_task(
                target,
                expected_version=expected_version,
                event=TaskEventDraft(
                    f"event:{received.task_id}:{target.version}",
                    f"TASK_{status.value}",
                    {"errors": ["safe_code"]},
                    target.updated_at,
                ),
            )

        messages = self.store.list_outbox()

        self.assertEqual(len(messages), len(NOTIFIED))
        self.assertEqual(
            {message.status for message in messages}, {OutboxStatus.PENDING}
        )
        self.assertEqual(
            {message.topic for message in messages}, {"task.status_changed"}
        )
        for message in messages:
            notification = Notification.from_payload(message.payload)
            self.assertEqual(notification.notification_id, message.outbox_id)
            self.assertEqual(notification.task_id, message.task_id)
            self.assertIn(notification.status, {status.value for status in NOTIFIED})
            self.assertNotIn("secret", repr(message.payload))
            self.assertEqual(message.next_attempt_at, notification.occurred_at)

    def test_rolls_back_task_when_automatic_outbox_identity_conflicts(self) -> None:
        """Outbox insert 실패 뒤 Task snapshot만 전진하는 atomicity 파손을 잡는다."""

        received = Task.receive(task_id="task-atomic", input="점검", at=NOW)
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        completed = running.transition(Status.COMPLETED, at=NOW + timedelta(seconds=2))
        self.store.create_task(
            received,
            event=TaskEventDraft("event:atomic:1", "TASK_RECEIVED", {}, NOW),
            outbox=OutboxDraft(
                outbox_id="notification:task-atomic:3",
                topic="occupied",
                payload={"occupied": True},
                created_at=NOW,
            ),
        )
        self.store.save_task(
            running,
            expected_version=1,
            event=TaskEventDraft(
                "event:atomic:2",
                "TASK_STARTED",
                {},
                running.updated_at,
            ),
        )

        with self.assertRaises(PersistenceConflictError):
            self.store.save_task(
                completed,
                expected_version=2,
                event=TaskEventDraft(
                    "event:atomic:3",
                    "TASK_COMPLETED",
                    {},
                    completed.updated_at,
                ),
            )

        self.assertEqual(self.store.get_task(received.task_id), running)
        self.assertEqual(
            [
                event.task_version
                for event in self.store.list_task_events(received.task_id)
            ],
            [1, 2],
        )

    def test_target_notification_cannot_be_replaced_by_a_custom_outbox(self) -> None:
        """호출자가 대상 상태의 필수 notification을 임의 topic으로 우회하는 버그를 잡는다."""

        received = Task.receive(task_id="task-required", input="점검", at=NOW)
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        completed = running.transition(Status.COMPLETED, at=NOW + timedelta(seconds=2))
        self.store.create_task(
            received,
            event=TaskEventDraft("event:required:1", "TASK_RECEIVED", {}, NOW),
        )
        self.store.save_task(
            running,
            expected_version=1,
            event=TaskEventDraft(
                "event:required:2", "TASK_STARTED", {}, running.updated_at
            ),
        )

        with self.assertRaises(InvalidPersistenceValueError):
            self.store.save_task(
                completed,
                expected_version=2,
                event=TaskEventDraft(
                    "event:required:3", "TASK_COMPLETED", {}, completed.updated_at
                ),
                outbox=OutboxDraft(
                    "custom-outbox",
                    "custom.topic",
                    {"raw": "override"},
                    completed.updated_at,
                ),
            )

        self.assertEqual(self.store.get_task(received.task_id), running)

    def test_rejects_reserved_notification_topic_from_public_task_writes(self) -> None:
        """일반 outbox 입력이 trusted notification namespace를 위조하는 버그를 잡는다."""

        received = Task.receive(task_id="task-reserved-create", input="점검", at=NOW)
        forged = OutboxDraft(
            "forged-notification",
            "task.status_changed",
            {"secret": "must-not-reach-sender"},
            NOW,
        )

        with self.assertRaises(InvalidPersistenceValueError):
            self.store.create_task(
                received,
                event=TaskEventDraft("event:reserved:1", "TASK_RECEIVED", {}, NOW),
                outbox=forged,
            )

        persisted = Task.receive(task_id="task-reserved-save", input="점검", at=NOW)
        self.store.create_task(
            persisted,
            event=TaskEventDraft("event:reserved-save:1", "TASK_RECEIVED", {}, NOW),
        )
        running = persisted.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        with self.assertRaises(InvalidPersistenceValueError):
            self.store.save_task(
                running,
                expected_version=1,
                event=TaskEventDraft(
                    "event:reserved-save:2", "TASK_STARTED", {}, running.updated_at
                ),
                outbox=forged,
            )

        self.assertIsNone(self.store.get_task(received.task_id))
        self.assertEqual(self.store.get_task(persisted.task_id), persisted)

    def test_rejects_public_requeue_of_reserved_notification_topic(self) -> None:
        """일반 retry API가 forged notification row를 활성화하는 버그를 잡는다."""

        received = Task.receive(task_id="task-reserved-requeue", input="점검", at=NOW)
        self.store.create_task(
            received,
            event=TaskEventDraft("event:reserved-requeue:1", "TASK_RECEIVED", {}, NOW),
            outbox=OutboxDraft("legacy-requeue", "legacy.topic", {}, NOW),
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE outbox_events SET topic = ?, status = ? WHERE outbox_id = ?",
                ("task.status_changed", "FAILED", "legacy-requeue"),
            )

        with self.assertRaises(InvalidPersistenceValueError):
            self.store.transition_outbox(
                "legacy-requeue",
                expected_status=OutboxStatus.FAILED,
                target=OutboxStatus.PENDING,
                at=NOW + timedelta(seconds=1),
            )

        self.assertEqual(self.store.list_outbox()[0].status, OutboxStatus.FAILED)

    def test_claim_competition_has_one_owner_and_success_is_terminal(self) -> None:
        """동시 dispatcher가 같은 알림을 두 번 claim하거나 stale owner가 확정하는 버그를 잡는다."""

        task = self._persist_completed("task-race")

        def claim(token: str):
            with SQLiteStore(self.database_path) as competing_store:
                return competing_store.claim_outbox(
                    now=task.updated_at,
                    lease_duration=timedelta(seconds=30),
                    lease_token=token,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(claim, ("lease-a", "lease-b")))

        claims = tuple(result for result in results if result is not None)
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim.status, OutboxStatus.PROCESSING)
        self.assertEqual(claim.attempt_count, 1)
        delivered = self.store.mark_outbox_delivered(
            claim.outbox_id,
            lease_token=claim.lease_token,
            at=task.updated_at + timedelta(seconds=1),
        )
        self.assertEqual(delivered.status, OutboxStatus.DELIVERED)
        self.assertEqual(delivered.delivered_at, task.updated_at + timedelta(seconds=1))
        self.assertIsNone(
            self.store.claim_outbox(
                now=task.updated_at + timedelta(minutes=1),
                lease_duration=timedelta(seconds=30),
                lease_token="lease-after-delivery",
            )
        )

    def test_failure_backoff_and_expired_lease_are_recoverable(self) -> None:
        """실패 즉시 재시도, stale lease 영구 고착 또는 old-token commit을 잡는다."""

        task = self._persist_completed("task-retry")
        first = self.store.claim_outbox(
            now=task.updated_at,
            lease_duration=timedelta(seconds=20),
            lease_token="lease-first",
        )
        assert first is not None
        failed_at = task.updated_at + timedelta(seconds=1)
        eligible_at = failed_at + timedelta(seconds=30)
        failed = self.store.record_outbox_failure(
            first.outbox_id,
            lease_token="lease-first",
            error_code="delivery_failed",
            at=failed_at,
            next_attempt_at=eligible_at,
        )

        self.assertEqual(failed.status, OutboxStatus.FAILED)
        self.assertEqual(failed.attempt_count, 1)
        self.assertEqual(failed.last_error, "delivery_failed")
        self.assertIsNone(
            self.store.claim_outbox(
                now=eligible_at - timedelta(microseconds=1),
                lease_duration=timedelta(seconds=20),
                lease_token="lease-too-early",
            )
        )
        second = self.store.claim_outbox(
            now=eligible_at,
            lease_duration=timedelta(seconds=20),
            lease_token="lease-second",
        )
        assert second is not None
        self.assertEqual(second.attempt_count, 2)
        with self.assertRaises(OptimisticConcurrencyError):
            self.store.mark_outbox_delivered(
                second.outbox_id,
                lease_token="lease-first",
                at=eligible_at + timedelta(seconds=1),
            )

        # 두 번째 owner가 죽으면 lease 만료 전에는 claim할 수 없고 만료 시 복구한다.
        self.assertIsNone(
            self.store.claim_outbox(
                now=eligible_at + timedelta(seconds=19),
                lease_duration=timedelta(seconds=20),
                lease_token="lease-before-expiry",
            )
        )
        recovered = self.store.claim_outbox(
            now=eligible_at + timedelta(seconds=20),
            lease_duration=timedelta(seconds=20),
            lease_token="lease-recovered",
        )
        assert recovered is not None
        self.assertEqual(recovered.attempt_count, 3)
        self.assertEqual(recovered.lease_token, "lease-recovered")
        self.assertEqual(self.store.get_task(task.task_id), task)

    def test_renews_only_the_current_notification_lease_owner(self) -> None:
        """Heartbeat가 stale token을 연장하거나 기존 만료 시각을 유지하는 버그를 잡는다."""

        task = self._persist_completed("task-heartbeat-store")
        claim = self.store.claim_outbox(
            now=task.updated_at,
            lease_duration=timedelta(seconds=20),
            lease_token="heartbeat-owner",
        )
        assert claim is not None

        renewed = self.store.renew_outbox_lease(
            claim.outbox_id,
            lease_token="heartbeat-owner",
            at=task.updated_at + timedelta(seconds=10),
            lease_duration=timedelta(seconds=20),
        )
        with self.assertRaises(OptimisticConcurrencyError):
            self.store.renew_outbox_lease(
                claim.outbox_id,
                lease_token="stale-owner",
                at=task.updated_at + timedelta(seconds=11),
                lease_duration=timedelta(seconds=20),
            )

        self.assertEqual(
            renewed.lease_expires_at,
            task.updated_at + timedelta(seconds=30),
        )
        self.assertIsNone(
            self.store.claim_outbox(
                now=task.updated_at + timedelta(seconds=20),
                lease_duration=timedelta(seconds=20),
                lease_token="competing-owner",
            )
        )

    def test_rejected_approval_enqueues_once_across_exact_replay(self) -> None:
        """Approval 전용 transaction이 알림을 누락하거나 replay 때 중복하는 버그를 잡는다."""

        received = Task.receive(task_id="task-rejected", input="변경", at=NOW)
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        planned = running.update_plan(
            "sha256:reject-plan",
            at=NOW + timedelta(seconds=2),
        )
        waiting = planned.transition(
            Status.WAITING_APPROVAL,
            at=NOW + timedelta(seconds=3),
        )
        snapshots = (received, running, planned, waiting)
        for index, snapshot in enumerate(snapshots, start=1):
            event = TaskEventDraft(
                f"event:rejected:{index}",
                f"TASK_{snapshot.status.value}",
                {},
                snapshot.updated_at,
            )
            if index == 1:
                self.store.create_task(snapshot, event=event)
            else:
                self.store.save_task(snapshot, expected_version=index - 1, event=event)
        # WAITING_APPROVAL 알림을 제외하고 거절 전이만 관찰한다.
        waiting_message = self.store.claim_outbox(
            now=waiting.updated_at,
            lease_duration=timedelta(seconds=10),
            lease_token="waiting-lease",
        )
        assert waiting_message is not None
        self.store.mark_outbox_delivered(
            waiting_message.outbox_id,
            lease_token="waiting-lease",
            at=waiting.updated_at,
        )
        response = ApprovalResponse.reject(
            decision_id="decision-reject",
            reason="운영자가 거절함",
        )
        rejected_at = NOW + timedelta(seconds=4)
        first = self.store.consume_approval(
            task=waiting,
            response=response,
            at=rejected_at,
            event=TaskEventDraft(
                "event:rejected:5",
                "TASK_REJECTED",
                {"decision_id": response.decision_id},
                rejected_at,
            ),
        )
        replay = self.store.consume_approval(
            task=waiting,
            response=response,
            at=rejected_at + timedelta(seconds=1),
            event=TaskEventDraft(
                "event:rejected:replay",
                "TASK_REJECTED",
                {"decision_id": response.decision_id},
                rejected_at + timedelta(seconds=1),
            ),
        )

        pending = self.store.list_outbox(status=OutboxStatus.PENDING)
        self.assertEqual(first.task.status, Status.REJECTED)
        self.assertEqual(replay.task, first.task)
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            Notification.from_payload(pending[0].payload).status,
            "REJECTED",
        )

    def _persist_completed(self, task_id: str) -> Task:
        received = Task.receive(task_id=task_id, input="점검", at=NOW)
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        completed = running.transition(
            Status.COMPLETED,
            at=NOW + timedelta(seconds=2),
        )
        self.store.create_task(
            received,
            event=TaskEventDraft(f"event:{task_id}:1", "TASK_RECEIVED", {}, NOW),
        )
        self.store.save_task(
            running,
            expected_version=1,
            event=TaskEventDraft(
                f"event:{task_id}:2",
                "TASK_STARTED",
                {},
                running.updated_at,
            ),
        )
        self.store.save_task(
            completed,
            expected_version=2,
            event=TaskEventDraft(
                f"event:{task_id}:3",
                "TASK_COMPLETED",
                {},
                completed.updated_at,
            ),
        )
        return completed


class NotificationDispatcherTests(unittest.IsolatedAsyncioTestCase):
    """Dispatcher의 실제 SQLite 전달, 실패와 재시작 복구를 검증한다."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "dispatcher.sqlite3"
        upgrade_database(self.database_path)
        self.store = SQLiteStore(self.database_path)
        received = Task.receive(task_id="task-dispatch", input="점검", at=NOW)
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        self.completed = running.transition(
            Status.COMPLETED,
            at=NOW + timedelta(seconds=2),
        )
        self.store.create_task(
            received,
            event=TaskEventDraft("event:dispatch:1", "TASK_RECEIVED", {}, NOW),
        )
        self.store.save_task(
            running,
            expected_version=1,
            event=TaskEventDraft(
                "event:dispatch:2", "TASK_STARTED", {}, running.updated_at
            ),
        )
        self.store.save_task(
            self.completed,
            expected_version=2,
            event=TaskEventDraft(
                "event:dispatch:3",
                "TASK_COMPLETED",
                {},
                self.completed.updated_at,
            ),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    async def test_dispatch_once_delivers_and_marks_the_claim_terminal(self) -> None:
        """Sender 성공 뒤 outbox가 PROCESSING에 남거나 중복 전달되는 버그를 잡는다."""

        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender,
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "dispatcher-lease",
        )

        claimed = await dispatcher.dispatch_once()
        second = await dispatcher.dispatch_once()

        self.assertTrue(claimed)
        self.assertFalse(second)
        self.assertEqual(len(sender.sent), 1)
        messages = self.store.list_outbox()
        self.assertEqual(messages[0].status, OutboxStatus.DELIVERED)
        self.assertEqual(messages[0].attempt_count, 1)

    async def test_sender_failure_does_not_change_task_and_restarts_at_eligibility(
        self,
    ) -> None:
        """외부 실패가 Task를 되돌리거나 persisted backoff가 재시작에서 사라지는 버그를 잡는다."""

        clock = [self.completed.updated_at]
        failing_sender = FakeNotificationSender(failures_before_success=1)
        first_dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=failing_sender,
            clock=lambda: clock[0],
            id_factory=lambda: "first-lease",
            base_retry_delay=timedelta(seconds=30),
        )

        self.assertTrue(await first_dispatcher.dispatch_once())
        failed = self.store.list_outbox()[0]
        self.assertEqual(failed.status, OutboxStatus.FAILED)
        self.assertEqual(failed.last_error, "notification_delivery_failed")
        self.assertEqual(
            failed.next_attempt_at,
            self.completed.updated_at + timedelta(seconds=30),
        )
        self.assertEqual(self.store.get_task(self.completed.task_id), self.completed)
        self.store.close()

        # 새 process 역할의 store/dispatcher가 persisted eligibility부터 이어받는다.
        self.store = SQLiteStore(self.database_path)
        sender_after_restart = FakeNotificationSender()
        dispatcher_after_restart = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender_after_restart,
            clock=lambda: clock[0],
            id_factory=lambda: "restart-lease",
            base_retry_delay=timedelta(seconds=30),
        )
        self.assertFalse(await dispatcher_after_restart.dispatch_once())
        clock[0] += timedelta(seconds=30)
        self.assertTrue(await dispatcher_after_restart.dispatch_once())

        delivered = self.store.list_outbox()[0]
        self.assertEqual(delivered.status, OutboxStatus.DELIVERED)
        self.assertEqual(delivered.attempt_count, 2)
        self.assertEqual(len(sender_after_restart.sent), 1)

    async def test_start_drain_and_stop_own_the_polling_lifecycle(self) -> None:
        """Runtime lifecycle에서 dispatcher worker가 시작되지 않거나 stop 뒤 남는 버그를 잡는다."""

        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender,
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "lifecycle-lease",
            poll_interval=0.01,
        )

        await dispatcher.start()
        await dispatcher.drain()
        await dispatcher.stop()
        await dispatcher.stop()

        self.assertEqual(len(sender.sent), 1)
        self.assertFalse(dispatcher.is_running)

    async def test_ignores_legacy_topics_and_delivers_only_notification_topic(
        self,
    ) -> None:
        """Legacy outbox가 새 notification 앞에서 parse 오류로 worker를 막는 버그를 잡는다."""

        legacy = Task.receive(task_id="task-legacy", input="legacy", at=NOW)
        self.store.create_task(
            legacy,
            event=TaskEventDraft("event:legacy:1", "TASK_RECEIVED", {}, NOW),
            outbox=OutboxDraft(
                "legacy-outbox",
                "task.received",
                {"task_id": legacy.task_id},
                NOW,
            ),
        )
        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender,
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "dedicated-topic-lease",
        )

        self.assertTrue(await dispatcher.dispatch_once())
        self.assertFalse(await dispatcher.dispatch_once())

        by_id = {message.outbox_id: message for message in self.store.list_outbox()}
        self.assertEqual(sender.sent[0].status, "COMPLETED")
        self.assertEqual(by_id["legacy-outbox"].status, OutboxStatus.PENDING)
        self.assertEqual(by_id["legacy-outbox"].attempt_count, 0)

    async def test_malformed_notification_is_backed_off_without_killing_worker(
        self,
    ) -> None:
        """전용 topic의 malformed payload가 PROCESSING 고착이나 polling 종료를 만드는 버그를 잡는다."""

        # 정상 automatic row를 먼저 전달해 malformed row만 남긴다.
        first_sender = FakeNotificationSender()
        first_dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=first_sender,
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "normal-lease",
        )
        self.assertTrue(await first_dispatcher.dispatch_once())
        malformed_task = Task.receive(
            task_id="task-malformed",
            input="malformed",
            at=self.completed.updated_at,
        )
        self.store.create_task(
            malformed_task,
            event=TaskEventDraft(
                "event:malformed:1",
                "TASK_RECEIVED",
                {},
                malformed_task.updated_at,
            ),
            outbox=OutboxDraft(
                "malformed-notification",
                "legacy.notification",
                {"task_id": malformed_task.task_id},
                malformed_task.updated_at,
            ),
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE outbox_events SET topic = ?, task_version = ? WHERE outbox_id = ?",
                ("task.status_changed", 1, "malformed-notification"),
            )
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=FakeNotificationSender(),
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "malformed-lease",
            poll_interval=0.01,
        )

        await dispatcher.start()
        await asyncio.sleep(0.02)
        await dispatcher.stop()

        malformed = next(
            message
            for message in self.store.list_outbox()
            if message.outbox_id == "malformed-notification"
        )
        self.assertEqual(malformed.status, OutboxStatus.FAILED)
        self.assertEqual(malformed.last_error, "notification_payload_invalid")
        self.assertEqual(
            malformed.next_attempt_at,
            self.completed.updated_at + timedelta(minutes=5),
        )
        self.assertFalse(dispatcher.is_running)

    async def test_skips_secret_injected_notification_and_delivers_valid_row_behind_it(
        self,
    ) -> None:
        """Poison row가 같은 poll의 정상 알림을 막거나 metadata secret을 보내는 버그를 잡는다."""

        poison_id = f"notification:{self.completed.task_id}:{self.completed.version}"
        poison_payload = self.store.list_outbox()[0].payload
        poison_payload["metadata"] = {"secret": "must-not-reach-sender"}
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE outbox_events SET payload_json = ? WHERE outbox_id = ?",
                (json.dumps(poison_payload), poison_id),
            )

        received = Task.receive(
            task_id="task-valid-behind-poison",
            input="점검",
            at=NOW + timedelta(seconds=3),
        )
        running = received.transition(
            Status.RUNNING,
            at=received.updated_at + timedelta(seconds=1),
        )
        completed = running.transition(
            Status.COMPLETED,
            at=running.updated_at + timedelta(seconds=1),
        )
        self.store.create_task(
            received,
            event=TaskEventDraft(
                "event:valid-behind:1", "TASK_RECEIVED", {}, received.updated_at
            ),
        )
        self.store.save_task(
            running,
            expected_version=1,
            event=TaskEventDraft(
                "event:valid-behind:2", "TASK_STARTED", {}, running.updated_at
            ),
        )
        self.store.save_task(
            completed,
            expected_version=2,
            event=TaskEventDraft(
                "event:valid-behind:3", "TASK_COMPLETED", {}, completed.updated_at
            ),
        )
        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender,
            clock=lambda: completed.updated_at,
            id_factory=lambda: "poison-skip-lease",
        )

        self.assertTrue(await dispatcher.dispatch_once())

        by_id = {message.outbox_id: message for message in self.store.list_outbox()}
        self.assertEqual(
            tuple(item.task_id for item in sender.sent), (completed.task_id,)
        )
        self.assertNotIn("secret", repr(sender.sent))
        self.assertEqual(by_id[poison_id].status, OutboxStatus.FAILED)
        self.assertEqual(
            by_id[poison_id].last_error,
            "notification_payload_invalid",
        )

    async def test_drain_continues_after_poison_batch_cap_to_valid_row(self) -> None:
        """100건 skip cap을 no-work로 오인해 101번째 poison 뒤 valid를 남기는 버그를 잡는다."""

        for index in range(101):
            task = Task.receive(
                task_id=f"task-poison-batch-{index:03d}",
                input="poison",
                at=NOW,
            )
            self.store.create_task(
                task,
                event=TaskEventDraft(
                    f"event:poison-batch:{index:03d}",
                    "TASK_RECEIVED",
                    {},
                    NOW,
                ),
                outbox=OutboxDraft(
                    f"poison-batch-{index:03d}",
                    "legacy.notification",
                    {"invalid": True},
                    NOW,
                ),
            )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE outbox_events SET topic = ?, task_version = ? "
                "WHERE outbox_id LIKE ?",
                ("task.status_changed", 1, "poison-batch-%"),
            )
        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender,
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "poison-batch-lease",
        )

        await dispatcher.drain()

        messages = self.store.list_outbox()
        poison = [
            item for item in messages if item.outbox_id.startswith("poison-batch-")
        ]
        self.assertEqual(
            tuple(item.task_id for item in sender.sent), (self.completed.task_id,)
        )
        self.assertEqual(len(poison), 101)
        self.assertEqual({item.status for item in poison}, {OutboxStatus.FAILED})
        self.assertFalse(await dispatcher.dispatch_once())

    async def test_rejects_status_not_bound_to_authoritative_task_event(self) -> None:
        """Raw row가 허용된 다른 terminal status로 위조되어 전달되는 버그를 잡는다."""

        message = self.store.list_outbox()[0]
        payload = dict(message.payload)
        payload["status"] = "FAILED"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE outbox_events SET payload_json = ? WHERE outbox_id = ?",
                (json.dumps(payload), message.outbox_id),
            )
        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender,
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "forged-status-lease",
        )

        await dispatcher.dispatch_once()

        self.assertEqual(sender.sent, ())
        forged = self.store.list_outbox()[0]
        self.assertEqual(forged.status, OutboxStatus.FAILED)
        self.assertEqual(forged.last_error, "notification_payload_invalid")

    async def test_rejects_plan_hash_not_bound_to_authoritative_task(self) -> None:
        """Safe-shaped forged plan_hash가 approval notification으로 전달되는 버그를 잡는다."""

        initial = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=FakeNotificationSender(),
            clock=lambda: self.completed.updated_at,
            id_factory=lambda: "initial-delivery-lease",
        )
        self.assertTrue(await initial.dispatch_once())
        received = Task.receive(
            task_id="task-forged-plan-notification",
            input="변경",
            at=NOW + timedelta(seconds=3),
        )
        running = received.transition(
            Status.RUNNING,
            at=received.updated_at + timedelta(seconds=1),
        )
        planned = running.update_plan(
            "sha256:authoritative-plan",
            at=running.updated_at + timedelta(seconds=1),
        )
        waiting = planned.transition(
            Status.WAITING_APPROVAL,
            at=planned.updated_at + timedelta(seconds=1),
        )
        for index, snapshot in enumerate(
            (received, running, planned, waiting), start=1
        ):
            event = TaskEventDraft(
                f"event:forged-plan-notification:{index}",
                f"TASK_{snapshot.status.value}",
                {},
                snapshot.updated_at,
            )
            if index == 1:
                self.store.create_task(snapshot, event=event)
            else:
                self.store.save_task(snapshot, expected_version=index - 1, event=event)
        notification_id = f"notification:{waiting.task_id}:{waiting.version}"
        message = next(
            item
            for item in self.store.list_outbox()
            if item.outbox_id == notification_id
        )
        payload = dict(message.payload)
        payload["metadata"] = {"plan_hash": "sha256:forged-plan"}
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE outbox_events SET payload_json = ? WHERE outbox_id = ?",
                (json.dumps(payload), notification_id),
            )
        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=sender,
            clock=lambda: waiting.updated_at,
            id_factory=lambda: "forged-plan-lease",
        )

        await dispatcher.dispatch_once()

        self.assertEqual(sender.sent, ())
        forged = next(
            item
            for item in self.store.list_outbox()
            if item.outbox_id == notification_id
        )
        self.assertEqual(forged.status, OutboxStatus.FAILED)

    async def test_sender_timeout_prevents_slow_delivery_from_crossing_lease(
        self,
    ) -> None:
        """느린 sender 중 lease가 만료되어 두 dispatcher가 중복 전송하는 버그를 잡는다."""

        started = time.monotonic()
        clock = lambda: (
            self.completed.updated_at + timedelta(seconds=time.monotonic() - started)
        )
        slow_sender = _BlockingNotificationSender()
        competing_sender = FakeNotificationSender()
        competing_store = SQLiteStore(self.database_path)
        self.addAsyncCleanup(asyncio.to_thread, competing_store.close)
        first = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=slow_sender,
            clock=clock,
            id_factory=lambda: "slow-owner",
            lease_duration=timedelta(milliseconds=50),
            send_timeout=timedelta(milliseconds=20),
            heartbeat_interval=timedelta(milliseconds=10),
            base_retry_delay=timedelta(milliseconds=100),
            max_retry_delay=timedelta(milliseconds=100),
        )
        second = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(competing_store),
            sender=competing_sender,
            clock=clock,
            id_factory=lambda: "competing-owner",
            lease_duration=timedelta(milliseconds=50),
            send_timeout=timedelta(milliseconds=20),
            heartbeat_interval=timedelta(milliseconds=10),
            base_retry_delay=timedelta(milliseconds=100),
            max_retry_delay=timedelta(milliseconds=100),
        )

        self.assertTrue(await first.dispatch_once())
        await asyncio.sleep(0.06)
        self.assertFalse(await second.dispatch_once())

        self.assertEqual(slow_sender.attempt_count, 1)
        self.assertTrue(slow_sender.cancelled.is_set())
        self.assertEqual(competing_sender.sent, ())
        failed = self.store.list_outbox()[0]
        self.assertEqual(failed.status, OutboxStatus.FAILED)
        self.assertEqual(failed.last_error, "notification_delivery_failed")

    async def test_heartbeat_prevents_duplicate_for_cancellation_resistant_sender(
        self,
    ) -> None:
        """Timeout cancellation을 무시한 active send 중 lease 만료와 중복 전달을 잡는다."""

        started = time.monotonic()
        clock = lambda: (
            self.completed.updated_at + timedelta(seconds=time.monotonic() - started)
        )
        resistant_sender = _CancellationResistantSender()
        competing_sender = FakeNotificationSender()
        competing_store = SQLiteStore(self.database_path)
        self.addAsyncCleanup(asyncio.to_thread, competing_store.close)
        first = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(self.store),
            sender=resistant_sender,
            clock=clock,
            id_factory=lambda: "resistant-owner",
            lease_duration=timedelta(milliseconds=50),
            send_timeout=timedelta(milliseconds=20),
            heartbeat_interval=timedelta(milliseconds=10),
            base_retry_delay=timedelta(milliseconds=100),
            max_retry_delay=timedelta(milliseconds=100),
        )
        second = NotificationDispatcher(
            outbox=SQLiteNotificationOutbox(competing_store),
            sender=competing_sender,
            clock=clock,
            id_factory=lambda: "competing-owner-resistant",
            lease_duration=timedelta(milliseconds=50),
            send_timeout=timedelta(milliseconds=20),
            heartbeat_interval=timedelta(milliseconds=10),
            base_retry_delay=timedelta(milliseconds=100),
            max_retry_delay=timedelta(milliseconds=100),
        )

        first_delivery = asyncio.create_task(first.dispatch_once())
        await resistant_sender.entered.wait()
        await asyncio.sleep(0.08)
        self.assertFalse(await second.dispatch_once())
        resistant_sender.release.set()
        self.assertTrue(await first_delivery)

        self.assertTrue(resistant_sender.cancelled.is_set())
        self.assertEqual(resistant_sender.attempt_count, 1)
        self.assertEqual(len(resistant_sender.sent), 1)
        self.assertEqual(competing_sender.sent, ())
        self.assertEqual(self.store.list_outbox()[0].status, OutboxStatus.DELIVERED)

    async def test_stale_finalize_isolated_after_sender_success(self) -> None:
        """다른 owner가 lease를 회수한 뒤 finalize CAS 오류가 dispatcher를 죽이는 버그를 잡는다."""

        notification = Notification.from_payload(self.store.list_outbox()[0].payload)
        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=_StaleFinalizationOutbox(notification),
            sender=sender,
            clock=lambda: notification.occurred_at,
            id_factory=lambda: "unused",
        )

        self.assertTrue(await dispatcher.dispatch_once())
        self.assertEqual(sender.sent, (notification,))

    async def test_lost_heartbeat_lease_cancels_send_without_finalize(self) -> None:
        """Lease 상실 뒤 stale owner가 성공/실패를 확정하거나 worker를 죽이는 버그를 잡는다."""

        notification = Notification.from_payload(self.store.list_outbox()[0].payload)
        outbox = _LeaseLosingOutbox(notification)
        sender = _BlockingNotificationSender()
        dispatcher = NotificationDispatcher(
            outbox=outbox,
            sender=sender,
            clock=lambda: notification.occurred_at,
            id_factory=lambda: "lost-owner",
            lease_duration=timedelta(milliseconds=50),
            send_timeout=timedelta(milliseconds=40),
            heartbeat_interval=timedelta(milliseconds=10),
        )

        self.assertTrue(await dispatcher.dispatch_once())

        self.assertTrue(sender.cancelled.is_set())
        self.assertFalse(outbox.finalized)

    async def test_renewal_error_cleans_sender_and_keeps_lease_backoff_on_shutdown(
        self,
    ) -> None:
        """Heartbeat dependency 오류가 sender task를 orphan으로 남기는 버그를 잡는다."""

        outbox = _RenewalFailingOutbox(SQLiteNotificationOutbox(self.store))
        sender = _CleanupTrackingSender()
        self.addAsyncCleanup(sender.release.set)
        started = time.monotonic()
        dispatcher = NotificationDispatcher(
            outbox=outbox,
            sender=sender,
            clock=lambda: (
                self.completed.updated_at
                + timedelta(seconds=time.monotonic() - started)
            ),
            id_factory=lambda: "renewal-error-owner",
            lease_duration=timedelta(milliseconds=50),
            send_timeout=timedelta(milliseconds=40),
            heartbeat_interval=timedelta(milliseconds=10),
            poll_interval=0.05,
        )

        await dispatcher.start()
        await asyncio.wait_for(outbox.renew_failed.wait(), timeout=0.2)
        await dispatcher.stop()

        persisted = self.store.list_outbox()[0]
        self.assertTrue(sender.cleaned.is_set())
        self.assertEqual(sender.active_count, 0)
        self.assertEqual(persisted.status, OutboxStatus.PROCESSING)
        self.assertEqual(persisted.attempt_count, 1)
        self.assertIsNotNone(persisted.lease_expires_at)


if __name__ == "__main__":
    unittest.main()
