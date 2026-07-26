"""SQLite persistence 공개 seam의 통합 테스트."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from alembic import command as alembic_command
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent_system.orchestration import (
    AgentRun,
    AgentRunOwnershipError,
    Approval,
    ApprovalConsumeStatus,
    ApprovalResponse,
    FailureCode,
    Phase,
    PlanChangedError,
    Status,
    Task,
    WorkflowRun,
)
from agent_system.persistence import (
    ApprovalApplyStatus,
    ApprovalConflictError,
    IdempotencyConflictError,
    IdempotencyKey,
    InvalidOutboxTransitionError,
    InvalidPersistenceValueError,
    OptimisticConcurrencyError,
    OutboxDraft,
    OutboxStatus,
    PersistenceConflictError,
    RecoveryDisposition,
    RuntimeCommandDraft,
    RuntimeCommandStatus,
    RuntimeCommandType,
    SQLiteStore,
    TaskEventDraft,
    _alembic_config,
    upgrade_database,
)

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 26, 19, 0, tzinfo=KST)


class MigrationTests(unittest.TestCase):
    """Alembic이 app schema의 유일한 생성 경로임을 검증한다."""

    def test_upgrades_an_empty_database_and_repeated_upgrade_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"

            upgrade_database(database_path)
            upgrade_database(database_path)

            with sqlite3.connect(database_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                revision = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                event_foreign_keys = connection.execute(
                    "PRAGMA foreign_key_list(task_events)"
                ).fetchall()

        self.assertEqual(
            tables,
            {
                "agent_runs",
                "alembic_version",
                "approval_decisions",
                "approvals",
                "outbox_events",
                "request_idempotency",
                "runtime_commands",
                "task_events",
                "tasks",
                "workflow_runs",
            },
        )
        self.assertEqual(revision, ("0004_notification_outbox",))
        self.assertTrue(
            any(row[2] == "tasks" and row[3] == "task_id" for row in event_foreign_keys)
        )

    def test_upgrades_an_existing_0001_database_without_losing_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            alembic_command.downgrade(
                _alembic_config(database_path),
                "0001_initial",
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "task-existing",
                        "기존 요청",
                        "RECEIVED",
                        1,
                        None,
                        NOW.isoformat(),
                        NOW.isoformat(),
                    ),
                )
            upgrade_database(database_path)

            with sqlite3.connect(database_path) as connection:
                revision = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                existing = connection.execute(
                    "SELECT input FROM tasks WHERE task_id = ?",
                    ("task-existing",),
                ).fetchone()
                decision_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    ("approval_decisions",),
                ).fetchone()
                command_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    ("runtime_commands",),
                ).fetchone()

        self.assertEqual(revision, ("0004_notification_outbox",))
        self.assertEqual(existing, ("기존 요청",))
        self.assertEqual(decision_table, ("approval_decisions",))
        self.assertEqual(command_table, ("runtime_commands",))

    def test_upgrades_legacy_processing_outbox_to_recoverable_pending(self) -> None:
        """Lease column이 없던 PROCESSING row가 영구 고착되는 migration 버그를 잡는다."""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            alembic_command.upgrade(
                _alembic_config(database_path),
                "0003_runtime_commands",
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "task-legacy-processing",
                        "기존 요청",
                        "RECEIVED",
                        1,
                        None,
                        NOW.isoformat(),
                        NOW.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO outbox_events (
                        outbox_id, task_id, topic, payload_json, status,
                        attempt_count, created_at, updated_at, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-processing",
                        "task-legacy-processing",
                        "task.status_changed",
                        '{"task_id":"task-legacy-processing"}',
                        "PROCESSING",
                        2,
                        NOW.isoformat(),
                        NOW.isoformat(),
                        None,
                    ),
                )

            upgrade_database(database_path)

            with sqlite3.connect(database_path) as connection:
                migrated = connection.execute(
                    """
                    SELECT status, last_error, next_attempt_at, lease_token,
                           lease_expires_at
                    FROM outbox_events WHERE outbox_id = ?
                    """,
                    ("legacy-processing",),
                ).fetchone()

        self.assertEqual(
            migrated,
            (
                "PENDING",
                "notification_lease_recovered",
                NOW.isoformat(),
                None,
                None,
            ),
        )

    def test_upgrades_from_a_repo_layout_free_package_and_has_no_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            installed_root = temporary_root / "installed"
            source_package = (
                Path(__file__).resolve().parents[2] / "src" / "agent_system"
            )
            shutil.copytree(
                source_package,
                installed_root / "agent_system",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            database_path = temporary_root / "packaged.sqlite3"
            verification_script = """
import sqlite3
import sys
from pathlib import Path

from alembic import command
import agent_system.persistence as persistence

database_path = Path(sys.argv[1])
installed_root = Path(sys.argv[2]).resolve()
assert Path(persistence.__file__).resolve().is_relative_to(installed_root)

persistence.upgrade_database(database_path)
persistence.upgrade_database(database_path)
command.check(persistence._alembic_config(database_path))

with sqlite3.connect(database_path) as connection:
    revision = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()
assert revision == ("0004_notification_outbox",)
"""
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(installed_root)

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    verification_script,
                    str(database_path),
                    str(installed_root),
                ],
                cwd=temporary_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


class TaskStoreTests(unittest.TestCase):
    """Task 변경이 event와 전달 의도를 원자적으로 보존하는지 검증한다."""

    def test_persists_and_restores_task_event_and_transactional_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            event = TaskEventDraft(
                event_id="event-1",
                event_type="TASK_RECEIVED",
                payload={"source": "ticket", "priority": 2},
                occurred_at=NOW,
            )
            outbox = OutboxDraft(
                outbox_id="outbox-1",
                topic="task.received",
                payload={"task_id": "task-1"},
                created_at=NOW,
            )

            with SQLiteStore(database_path) as store:
                written = store.create_task(task, event=event, outbox=outbox)

            with SQLiteStore(database_path) as reopened:
                restored = reopened.get_task("task-1")
                events = reopened.list_task_events("task-1")
                outbox_messages = reopened.list_outbox(status=OutboxStatus.PENDING)

        self.assertEqual(written.task, task)
        self.assertFalse(written.replayed)
        self.assertEqual(restored, task)
        self.assertEqual(restored.created_at.isoformat(), "2026-07-26T19:00:00+09:00")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "event-1")
        self.assertEqual(events[0].payload, {"priority": 2, "source": "ticket"})
        self.assertEqual(len(outbox_messages), 1)
        self.assertEqual(outbox_messages[0].outbox_id, "outbox-1")
        self.assertEqual(outbox_messages[0].status, OutboxStatus.PENDING)

    def test_persists_task_and_start_command_atomically_and_replays_exactly(
        self,
    ) -> None:
        """Task commit과 start intent 사이 crash 또는 이종 command 덮어쓰기를 막는다."""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-command", input="상태 조회", at=NOW)
            start = RuntimeCommandDraft(
                command_id="command-start",
                task_id=task.task_id,
                command_type=RuntimeCommandType.START,
                fingerprint="sha256:start",
                payload={"request": {"kind": "user_task", "input": "상태 조회"}},
                created_at=NOW,
            )
            approval = RuntimeCommandDraft(
                command_id="command-approval",
                task_id=task.task_id,
                command_type=RuntimeCommandType.APPROVAL,
                fingerprint="sha256:approval",
                payload={"decision_id": "decision-1"},
                created_at=NOW + timedelta(seconds=1),
            )

            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-command", "TASK_RECEIVED", {}, NOW),
                    command=start,
                )
                pending = store.list_pending_runtime_commands()
                replay = store.put_runtime_command(start, expected_task=task)
                with self.assertRaises(PersistenceConflictError):
                    store.put_runtime_command(approval, expected_task=task)
                failed = store.record_runtime_command_failure(
                    start.command_id,
                    error_code="background_execution_failed",
                    at=NOW + timedelta(seconds=2),
                )
                completed = store.complete_runtime_command(
                    start.command_id,
                    at=NOW + timedelta(seconds=3),
                )

                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].fingerprint, "sha256:start")
                self.assertTrue(replay.replayed)
                self.assertEqual(failed.status, RuntimeCommandStatus.PENDING)
                self.assertEqual(failed.attempt_count, 1)
                self.assertEqual(failed.last_error, "background_execution_failed")
                self.assertEqual(completed.status, RuntimeCommandStatus.COMPLETED)
                self.assertEqual(store.list_pending_runtime_commands(), ())

    def test_uses_task_version_and_rolls_back_snapshot_when_event_insert_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            received = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
            stale_cancel = received.cancel(at=NOW + timedelta(seconds=2))

            with SQLiteStore(database_path) as store:
                store.create_task(
                    received,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                )
                store.save_task(
                    running,
                    expected_version=1,
                    event=TaskEventDraft(
                        "event-2", "TASK_RUNNING", {}, running.updated_at
                    ),
                )

                with self.assertRaises(OptimisticConcurrencyError):
                    store.save_task(
                        stale_cancel,
                        expected_version=1,
                        event=TaskEventDraft(
                            "event-stale",
                            "TASK_CANCELLED",
                            {},
                            stale_cancel.updated_at,
                        ),
                    )

                planned = running.update_plan(
                    "sha256:plan-v1",
                    at=NOW + timedelta(seconds=3),
                )
                with self.assertRaises(PersistenceConflictError):
                    store.save_task(
                        planned,
                        expected_version=2,
                        event=TaskEventDraft(
                            "event-2",
                            "PLAN_UPDATED",
                            {},
                            planned.updated_at,
                        ),
                    )

                current = store.get_task("task-1")
                events = store.list_task_events("task-1")

        self.assertEqual(current, running)
        self.assertEqual([event.task_version for event in events], [1, 2])

    def test_deduplicates_concurrent_task_creation_by_request_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            idempotency = IdempotencyKey(
                namespace="alerts",
                key="external-event-7",
                fingerprint="sha256:request-a",
                created_at=NOW,
            )

            def create(suffix: str):
                task = Task.receive(
                    task_id=f"task-{suffix}",
                    input="노드 점검",
                    at=NOW,
                )
                with SQLiteStore(database_path) as store:
                    return store.create_task(
                        task,
                        event=TaskEventDraft(
                            f"event-{suffix}",
                            "TASK_RECEIVED",
                            {},
                            NOW,
                        ),
                        idempotency=idempotency,
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(create, "a")
                second_future = executor.submit(create, "b")
                results = (first_future.result(), second_future.result())

            with SQLiteStore(database_path) as store:
                persisted = tuple(
                    task
                    for task_id in ("task-a", "task-b")
                    if (task := store.get_task(task_id)) is not None
                )

        self.assertEqual(len(persisted), 1)
        self.assertEqual(results[0].task, results[1].task)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])

    def test_rejects_reusing_request_key_for_a_different_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-a", input="노드 점검", at=NOW)

            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-a", "TASK_RECEIVED", {}, NOW),
                    idempotency=IdempotencyKey(
                        "alerts", "external-event-7", "sha256:request-a", NOW
                    ),
                )
                with self.assertRaises(IdempotencyConflictError):
                    store.create_task(
                        Task.receive(task_id="task-b", input="다른 요청", at=NOW),
                        event=TaskEventDraft("event-b", "TASK_RECEIVED", {}, NOW),
                        idempotency=IdempotencyKey(
                            "alerts",
                            "external-event-7",
                            "sha256:request-b",
                            NOW,
                        ),
                    )

                self.assertIsNone(store.get_task("task-b"))

    def test_enforces_append_only_task_events_in_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                )

            for statement in (
                "UPDATE task_events SET event_type = 'CHANGED'",
                "DELETE FROM task_events",
            ):
                with (
                    self.subTest(statement=statement),
                    sqlite3.connect(database_path) as connection,
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    connection.execute(statement)

    def test_rejects_public_processing_transition_without_a_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                    outbox=OutboxDraft(
                        "outbox-1", "task.received", {"task_id": "task-1"}, NOW
                    ),
                )
                with self.assertRaises(InvalidOutboxTransitionError):
                    store.transition_outbox(
                        "outbox-1",
                        expected_status=OutboxStatus.PENDING,
                        target=OutboxStatus.PROCESSING,
                        at=NOW + timedelta(seconds=3),
                    )
                persisted = store.list_outbox()[0]

        self.assertEqual(persisted.status, OutboxStatus.PENDING)
        self.assertEqual(persisted.attempt_count, 0)
        self.assertIsNone(persisted.lease_token)


class MixedOffsetOrderingTests(unittest.TestCase):
    """공개 시간순 목록이 offset 문자열이 아니라 UTC instant를 따르는지 검증한다."""

    def test_orders_approval_history_by_consumed_instant(self) -> None:
        base = datetime(2026, 7, 26, 9, 0, tzinfo=KST)
        first_approved_at = datetime(2026, 7, 26, 10, 0, tzinfo=KST)
        first_consumed_at = datetime(2026, 7, 26, 10, 5, tzinfo=KST)
        second_approved_at = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
        second_consumed_at = datetime(2026, 7, 26, 2, 5, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            received = Task.receive(task_id="task-1", input="변경 요청", at=base)
            running = received.transition(
                Status.RUNNING,
                at=base + timedelta(minutes=5),
            )
            planned = running.update_plan(
                "sha256:plan-v1",
                at=base + timedelta(minutes=10),
            )
            waiting = planned.transition(
                Status.WAITING_APPROVAL,
                at=base + timedelta(minutes=15),
            )

            with SQLiteStore(database_path) as store:
                self._persist_task_snapshots(
                    store, (received, running, planned, waiting)
                )
                first_approval = Approval.grant_for(
                    waiting,
                    at=first_approved_at,
                )
                first_result = store.apply_approval(
                    first_approval,
                    decision_id="decision-early",
                    resumed_at=first_consumed_at,
                    event=TaskEventDraft(
                        "event-first-approval",
                        "TASK_APPROVED",
                        {},
                        first_consumed_at,
                    ),
                )
                replanned = first_result.task.update_plan(
                    "sha256:plan-v2",
                    at=first_consumed_at + timedelta(minutes=5),
                )
                waiting_again = replanned.transition(
                    Status.WAITING_APPROVAL,
                    at=first_consumed_at + timedelta(minutes=10),
                )
                store.save_task(
                    replanned,
                    expected_version=first_result.task.version,
                    event=TaskEventDraft(
                        "event-plan-v2",
                        "PLAN_UPDATED",
                        {},
                        replanned.updated_at,
                    ),
                )
                store.save_task(
                    waiting_again,
                    expected_version=replanned.version,
                    event=TaskEventDraft(
                        "event-waiting-again",
                        "WAITING_APPROVAL",
                        {},
                        waiting_again.updated_at,
                    ),
                )
                second_approval = Approval.grant_for(
                    waiting_again,
                    at=second_approved_at,
                )
                store.apply_approval(
                    second_approval,
                    decision_id="decision-late",
                    resumed_at=second_consumed_at,
                    event=TaskEventDraft(
                        "event-second-approval",
                        "TASK_APPROVED",
                        {},
                        second_consumed_at,
                    ),
                )

                approvals = store.list_approvals("task-1")

        self.assertEqual(
            [record.decision_id for record in approvals],
            ["decision-early", "decision-late"],
        )

    def test_orders_recovery_candidates_by_created_instant(self) -> None:
        early = datetime(2026, 7, 26, 10, 0, tzinfo=KST)
        late = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            early_task = Task.receive(task_id="task-early", input="먼저", at=early)
            late_task = Task.receive(task_id="task-late", input="나중", at=late)
            with SQLiteStore(database_path) as store:
                for task in (early_task, late_task):
                    store.create_task(
                        task,
                        event=TaskEventDraft(
                            f"event-{task.task_id}",
                            "TASK_RECEIVED",
                            {},
                            task.created_at,
                        ),
                    )
                candidates = store.list_recovery_candidates()

        self.assertEqual(
            [candidate.task.task_id for candidate in candidates],
            ["task-early", "task-late"],
        )

    def test_orders_outbox_by_created_instant(self) -> None:
        early = datetime(2026, 7, 26, 10, 0, tzinfo=KST)
        late = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            with SQLiteStore(database_path) as store:
                for suffix, created_at in (("early", early), ("late", late)):
                    task = Task.receive(
                        task_id=f"task-{suffix}",
                        input=suffix,
                        at=created_at,
                    )
                    store.create_task(
                        task,
                        event=TaskEventDraft(
                            f"event-{suffix}",
                            "TASK_RECEIVED",
                            {},
                            created_at,
                        ),
                        outbox=OutboxDraft(
                            f"outbox-{suffix}",
                            "task.received",
                            {"task_id": task.task_id},
                            created_at,
                        ),
                    )
                outbox = store.list_outbox()

        self.assertEqual(
            [message.outbox_id for message in outbox],
            ["outbox-early", "outbox-late"],
        )

    @staticmethod
    def _persist_task_snapshots(
        store: SQLiteStore,
        snapshots: tuple[Task, ...],
    ) -> None:
        for index, task in enumerate(snapshots, start=1):
            event = TaskEventDraft(
                f"event-initial-{index}",
                f"TASK_{task.status.value}",
                {},
                task.updated_at,
            )
            if index == 1:
                store.create_task(task, event=event)
            else:
                store.save_task(
                    task,
                    expected_version=index - 1,
                    event=event,
                )


class RunStoreTests(unittest.TestCase):
    """Workflow issuance와 AgentRun history의 원자성과 소유권을 검증한다."""

    def test_atomically_persists_issuance_and_restores_all_agent_run_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            workflow = WorkflowRun.start(
                workflow_run_id="workflow-1",
                task=task,
                max_agent_runs=3,
                at=NOW,
            )
            first_workflow, first_run = workflow.begin_agent_run(
                agent_run_id="agent-run-1",
                agent_id="classifier",
                at=NOW + timedelta(seconds=1),
            )
            completed_first = first_run.complete(
                agent_id="classifier",
                outcome="SUCCESS",
                output="분류 완료",
                at=NOW + timedelta(seconds=2),
            )
            second_workflow, second_run = first_workflow.begin_agent_run(
                agent_run_id="agent-run-2",
                agent_id="insight",
                at=NOW + timedelta(seconds=3),
            )

            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                )
                store.create_workflow_run(workflow)
                store.record_agent_run(workflow, first_workflow, first_run)
                store.complete_agent_run(completed_first)
                store.record_agent_run(
                    first_workflow,
                    second_workflow,
                    second_run,
                )

            with SQLiteStore(database_path) as reopened:
                restored_workflow = reopened.get_workflow_run("workflow-1")
                history = reopened.list_agent_runs("workflow-1")

        self.assertEqual(restored_workflow, second_workflow)
        self.assertEqual(history, (completed_first, second_run))

    def test_rejects_a_stale_issuance_without_partially_inserting_agent_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            workflow = WorkflowRun.start(
                workflow_run_id="workflow-1",
                task=task,
                max_agent_runs=2,
                at=NOW,
            )
            persisted_workflow, persisted_run = workflow.begin_agent_run(
                agent_run_id="agent-run-1",
                agent_id="classifier",
                at=NOW + timedelta(seconds=1),
            )
            stale_workflow, stale_run = workflow.begin_agent_run(
                agent_run_id="agent-run-stale",
                agent_id="insight",
                at=NOW + timedelta(seconds=2),
            )

            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                )
                store.create_workflow_run(workflow)
                store.record_agent_run(
                    workflow,
                    persisted_workflow,
                    persisted_run,
                )
                with self.assertRaises(OptimisticConcurrencyError):
                    store.record_agent_run(workflow, stale_workflow, stale_run)

                self.assertIsNone(store.get_agent_run("agent-run-stale"))
                self.assertEqual(
                    store.get_workflow_run("workflow-1"),
                    persisted_workflow,
                )

    def test_rolls_back_workflow_ledger_when_agent_run_insert_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            first_task = Task.receive(task_id="task-1", input="첫 요청", at=NOW)
            second_task = Task.receive(task_id="task-2", input="둘째 요청", at=NOW)
            first_workflow = WorkflowRun.start(
                workflow_run_id="workflow-1",
                task=first_task,
                max_agent_runs=1,
                at=NOW,
            )
            second_workflow = WorkflowRun.start(
                workflow_run_id="workflow-2",
                task=second_task,
                max_agent_runs=1,
                at=NOW,
            )
            issued_first, first_run = first_workflow.begin_agent_run(
                agent_run_id="globally-duplicate-run",
                agent_id="classifier",
                at=NOW + timedelta(seconds=1),
            )
            issued_second, second_run = second_workflow.begin_agent_run(
                agent_run_id="globally-duplicate-run",
                agent_id="classifier",
                at=NOW + timedelta(seconds=1),
            )
            with SQLiteStore(database_path) as store:
                for task in (first_task, second_task):
                    store.create_task(
                        task,
                        event=TaskEventDraft(
                            f"event-{task.task_id}",
                            "TASK_RECEIVED",
                            {},
                            NOW,
                        ),
                    )
                store.create_workflow_run(first_workflow)
                store.create_workflow_run(second_workflow)
                store.record_agent_run(first_workflow, issued_first, first_run)
                with self.assertRaises(PersistenceConflictError):
                    store.record_agent_run(
                        second_workflow,
                        issued_second,
                        second_run,
                    )

                restored_second = store.get_workflow_run("workflow-2")
                second_history = store.list_agent_runs("workflow-2")

        self.assertEqual(restored_second, second_workflow)
        self.assertEqual(second_history, ())

    def test_rejects_non_exact_agent_run_successors_without_persisting_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            workflow = WorkflowRun.start(
                workflow_run_id="workflow-1",
                task=task,
                max_agent_runs=2,
                at=NOW,
            )
            issued, agent_run = workflow.begin_agent_run(
                agent_run_id="agent-run-1",
                agent_id="classifier",
                at=NOW + timedelta(seconds=1),
            )

            phase_snapshot = issued.to_snapshot() | {"phase": Phase.ANALYZING.value}
            forged_workflow_phase = WorkflowRun.from_snapshot(phase_snapshot)

            updated_at_snapshot = issued.to_snapshot() | {
                "updated_at": (NOW + timedelta(seconds=2)).isoformat()
            }
            forged_updated_at = WorkflowRun.from_snapshot(updated_at_snapshot)

            forged_issuance_snapshot = issued.to_snapshot()
            forged_issuance_snapshot["agent_run_issuances"][0]["phase"] = (
                Phase.ANALYZING.value
            )
            forged_issuance = WorkflowRun.from_snapshot(forged_issuance_snapshot)
            forged_run_snapshot = agent_run.to_snapshot() | {
                "phase": Phase.ANALYZING.value
            }
            forged_run_phase = AgentRun.from_snapshot(
                forged_run_snapshot,
                workflow=forged_issuance,
            )

            completed_run = agent_run.complete(
                agent_id="classifier",
                outcome="SUCCESS",
                output="완료",
                at=NOW + timedelta(seconds=2),
            )
            invalid_cases = (
                ("workflow phase", forged_workflow_phase, agent_run),
                ("workflow updated_at", forged_updated_at, agent_run),
                ("issuance/run phase", forged_issuance, forged_run_phase),
                ("completed run", issued, completed_run),
            )

            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                )
                store.create_workflow_run(workflow)
                for case_name, candidate, candidate_run in invalid_cases:
                    with (
                        self.subTest(case_name=case_name),
                        self.assertRaises(InvalidPersistenceValueError),
                    ):
                        store.record_agent_run(
                            workflow,
                            candidate,
                            candidate_run,
                        )
                    self.assertEqual(
                        store.get_workflow_run("workflow-1"),
                        workflow,
                    )
                    self.assertEqual(store.list_agent_runs("workflow-1"), ())

    def test_validates_restored_agent_run_against_owning_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            workflow = WorkflowRun.start(
                workflow_run_id="workflow-1",
                task=task,
                max_agent_runs=1,
                at=NOW,
            )
            issued_workflow, agent_run = workflow.begin_agent_run(
                agent_run_id="agent-run-1",
                agent_id="classifier",
                at=NOW + timedelta(seconds=1),
            )
            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                )
                store.create_workflow_run(workflow)
                store.record_agent_run(workflow, issued_workflow, agent_run)

            with sqlite3.connect(database_path) as connection:
                snapshot = json.loads(
                    connection.execute(
                        "SELECT snapshot_json FROM agent_runs WHERE agent_run_id = ?",
                        ("agent-run-1",),
                    ).fetchone()[0]
                )
                snapshot["agent_id"] = "forged-agent"
                connection.execute(
                    "UPDATE agent_runs SET snapshot_json = ? WHERE agent_run_id = ?",
                    (json.dumps(snapshot), "agent-run-1"),
                )

            with (
                SQLiteStore(database_path) as reopened,
                self.assertRaises(AgentRunOwnershipError),
            ):
                reopened.get_agent_run("agent-run-1")

    def test_optimistically_saves_a_workflow_phase_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
            workflow = WorkflowRun.start(
                workflow_run_id="workflow-1",
                task=task,
                max_agent_runs=2,
                at=NOW,
            )
            analyzing = workflow.advance(
                Phase.ANALYZING,
                at=NOW + timedelta(seconds=1),
            )
            with SQLiteStore(database_path) as store:
                store.create_task(
                    task,
                    event=TaskEventDraft("event-1", "TASK_RECEIVED", {}, NOW),
                )
                store.create_workflow_run(workflow)
                saved = store.save_workflow_run(workflow, analyzing)
                with self.assertRaises(OptimisticConcurrencyError):
                    store.save_workflow_run(workflow, analyzing)

                restored = store.get_workflow_run("workflow-1")

        self.assertEqual(saved, analyzing)
        self.assertEqual(restored, analyzing)


class ApprovalStoreTests(unittest.TestCase):
    """Approval binding의 atomic compare-and-transition을 검증한다."""

    def test_rejects_reserved_notification_topic_from_approval_outbox(self) -> None:
        """Approval ingress가 trusted notification namespace를 위조하는 버그를 잡는다."""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            approval = Approval.grant_for(waiting, at=NOW + timedelta(seconds=4))
            with SQLiteStore(database_path) as store:
                with self.assertRaises(InvalidPersistenceValueError):
                    store.apply_approval(
                        approval,
                        decision_id="forged-notification-decision",
                        resumed_at=NOW + timedelta(seconds=5),
                        event=TaskEventDraft(
                            "event-forged-notification",
                            "TASK_APPROVED",
                            {},
                            NOW + timedelta(seconds=5),
                        ),
                        outbox=OutboxDraft(
                            "forged-approval-notification",
                            "task.status_changed",
                            {"secret": "must-not-reach-sender"},
                            NOW + timedelta(seconds=5),
                        ),
                    )

                self.assertEqual(store.get_task(waiting.task_id), waiting)
                self.assertEqual(store.list_approvals(waiting.task_id), ())

    def test_consumes_one_concurrent_approval_and_replays_the_stored_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            approval = Approval.grant_for(
                waiting,
                at=NOW + timedelta(seconds=4),
            )

            def apply(suffix: str):
                with SQLiteStore(database_path) as store:
                    return store.apply_approval(
                        approval,
                        decision_id="decision-1",
                        resumed_at=NOW + timedelta(seconds=5),
                        event=TaskEventDraft(
                            f"event-approved-{suffix}",
                            "TASK_APPROVED",
                            {"decision_id": "decision-1"},
                            NOW + timedelta(seconds=5),
                        ),
                        outbox=OutboxDraft(
                            f"outbox-approved-{suffix}",
                            "task.approved",
                            {"task_id": "task-1"},
                            NOW + timedelta(seconds=5),
                        ),
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(apply, "a")
                second_future = executor.submit(apply, "b")
                results = (first_future.result(), second_future.result())

            with SQLiteStore(database_path) as store:
                current = store.get_task("task-1")
                events = store.list_task_events("task-1")
                approvals = store.list_approvals("task-1")
                outbox = store.list_outbox(status=OutboxStatus.PENDING)

        self.assertEqual(
            sorted(result.status for result in results),
            [ApprovalApplyStatus.ALREADY_APPLIED, ApprovalApplyStatus.APPLIED],
        )
        self.assertEqual(results[0].task, results[1].task)
        self.assertEqual(current, results[0].task)
        self.assertEqual(current.status, Status.RUNNING)
        self.assertEqual(current.version, waiting.version + 1)
        self.assertEqual(len(events), 5)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0].approval, approval)
        self.assertEqual(len(outbox), 2)
        self.assertEqual(
            {message.topic for message in outbox},
            {"task.approved", "task.status_changed"},
        )

    def test_atomically_rejects_and_replays_the_exact_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            response = ApprovalResponse.reject(
                decision_id="decision-reject",
                reason="운영자가 변경을 거절함",
            )
            event = TaskEventDraft(
                "event-rejected",
                "TASK_REJECTED",
                {"decision_id": response.decision_id},
                NOW + timedelta(seconds=5),
            )

            with SQLiteStore(database_path) as store:
                applied = store.consume_approval(
                    task=waiting,
                    response=response,
                    at=NOW + timedelta(seconds=5),
                    event=event,
                )
                replayed = store.consume_approval(
                    task=waiting,
                    response=response,
                    at=NOW + timedelta(seconds=6),
                    event=TaskEventDraft(
                        "event-rejected-replay",
                        "TASK_REJECTED",
                        {"decision_id": response.decision_id},
                        NOW + timedelta(seconds=6),
                    ),
                )
                current = store.get_task(waiting.task_id)
                events = store.list_task_events(waiting.task_id)
                decisions = store.list_approval_decisions(waiting.task_id)

        self.assertEqual(applied.status, ApprovalConsumeStatus.APPLIED)
        self.assertEqual(applied.task.status, Status.REJECTED)
        self.assertEqual(applied.failure, FailureCode.HUMAN_REJECTED)
        self.assertEqual(replayed.status, ApprovalConsumeStatus.ALREADY_APPLIED)
        self.assertEqual(replayed.task, applied.task)
        self.assertEqual(replayed.failure, applied.failure)
        self.assertEqual(current, applied.task)
        self.assertEqual(len(events), 5)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].response, response)
        self.assertEqual(decisions[0].result_task, applied.task)

    def test_concurrently_accepts_once_across_separate_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            response = ApprovalResponse(
                decision_id="decision-accept",
                accepted=True,
                approval=Approval.grant_for(
                    waiting,
                    at=NOW + timedelta(seconds=4),
                ),
            )

            def consume(suffix: str):
                with SQLiteStore(database_path) as store:
                    return store.consume_approval(
                        task=waiting,
                        response=response,
                        at=NOW + timedelta(seconds=5),
                        event=TaskEventDraft(
                            f"event-consumed-{suffix}",
                            "TASK_APPROVED",
                            {"decision_id": response.decision_id},
                            NOW + timedelta(seconds=5),
                        ),
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(consume, "a"),
                    executor.submit(consume, "b"),
                )
                results = tuple(future.result() for future in futures)

            with SQLiteStore(database_path) as store:
                current = store.get_task(waiting.task_id)
                decisions = store.list_approval_decisions(waiting.task_id)

        self.assertEqual(
            sorted(result.status for result in results),
            [ApprovalConsumeStatus.ALREADY_APPLIED, ApprovalConsumeStatus.APPLIED],
        )
        self.assertEqual(results[0].task, results[1].task)
        self.assertEqual(current, results[0].task)
        self.assertEqual(current.status, Status.RUNNING)
        self.assertEqual(current.version, waiting.version + 1)
        self.assertEqual(len(decisions), 1)
        self.assertIsNone(decisions[0].failure)

    def test_rolls_back_rejection_when_event_append_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            response = ApprovalResponse.reject(
                decision_id="decision-reject-rollback",
                reason="작업 중단",
            )

            with SQLiteStore(database_path) as store:
                with self.assertRaises(PersistenceConflictError):
                    store.consume_approval(
                        task=waiting,
                        response=response,
                        at=NOW + timedelta(seconds=5),
                        event=TaskEventDraft(
                            "event-waiting",
                            "TASK_REJECTED",
                            {},
                            NOW + timedelta(seconds=5),
                        ),
                    )
                current = store.get_task(waiting.task_id)
                decisions = store.list_approval_decisions(waiting.task_id)
                events = store.list_task_events(waiting.task_id)

        self.assertEqual(current, waiting)
        self.assertEqual(decisions, ())
        self.assertEqual(len(events), 4)

    def test_rejects_changed_decision_content_and_binding_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            response = ApprovalResponse.reject(
                decision_id="decision-conflict",
                reason="승인하지 않음",
            )
            with SQLiteStore(database_path) as store:
                store.consume_approval(
                    task=waiting,
                    response=response,
                    at=NOW + timedelta(seconds=5),
                    event=TaskEventDraft(
                        "event-reject-conflict-base",
                        "TASK_REJECTED",
                        {},
                        NOW + timedelta(seconds=5),
                    ),
                )
                with self.assertRaises(ApprovalConflictError):
                    store.consume_approval(
                        task=waiting,
                        response=ApprovalResponse.reject(
                            decision_id=response.decision_id,
                            reason="변경된 거절 사유",
                        ),
                        at=NOW + timedelta(seconds=6),
                        event=TaskEventDraft(
                            "event-changed-content",
                            "TASK_REJECTED",
                            {},
                            NOW + timedelta(seconds=6),
                        ),
                    )
                changed_binding = waiting.update_plan(
                    "sha256:plan-v2",
                    at=NOW + timedelta(seconds=6),
                )
                with self.assertRaises(ApprovalConflictError):
                    store.consume_approval(
                        task=changed_binding,
                        response=response,
                        at=NOW + timedelta(seconds=7),
                        event=TaskEventDraft(
                            "event-changed-binding",
                            "TASK_REJECTED",
                            {},
                            NOW + timedelta(seconds=7),
                        ),
                    )
                with self.assertRaises(ApprovalConflictError):
                    store.consume_approval(
                        task=waiting,
                        response=ApprovalResponse.reject(
                            decision_id="decision-other",
                            reason="승인하지 않음",
                        ),
                        at=NOW + timedelta(seconds=7),
                        event=TaskEventDraft(
                            "event-binding-reuse",
                            "TASK_REJECTED",
                            {},
                            NOW + timedelta(seconds=7),
                        ),
                    )

    def test_rejects_stale_version_and_terminal_task_conflicts(self) -> None:
        for terminal in (False, True):
            with (
                self.subTest(terminal=terminal),
                tempfile.TemporaryDirectory() as directory,
            ):
                database_path = Path(directory) / "state.sqlite3"
                waiting = self._create_waiting_task(database_path)
                if terminal:
                    changed = waiting.transition(
                        Status.CANCELLED,
                        at=NOW + timedelta(seconds=4),
                    )
                    event_type = "TASK_CANCELLED"
                else:
                    changed = waiting.update_plan(
                        "sha256:plan-v2",
                        at=NOW + timedelta(seconds=4),
                    )
                    event_type = "PLAN_UPDATED"
                with SQLiteStore(database_path) as store:
                    store.save_task(
                        changed,
                        expected_version=waiting.version,
                        event=TaskEventDraft(
                            "event-authoritative-change",
                            event_type,
                            {},
                            changed.updated_at,
                        ),
                    )
                    with self.assertRaises(OptimisticConcurrencyError):
                        store.consume_approval(
                            task=waiting,
                            response=ApprovalResponse.reject(
                                decision_id="decision-stale",
                                reason="오래된 화면에서 거절",
                            ),
                            at=NOW + timedelta(seconds=5),
                            event=TaskEventDraft(
                                "event-stale-rejection",
                                "TASK_REJECTED",
                                {},
                                NOW + timedelta(seconds=5),
                            ),
                        )

    def test_rolls_back_approval_consumption_when_event_append_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            approval = Approval.grant_for(
                waiting,
                at=NOW + timedelta(seconds=4),
            )

            with SQLiteStore(database_path) as store:
                with self.assertRaises(PersistenceConflictError):
                    store.apply_approval(
                        approval,
                        decision_id="decision-rollback",
                        resumed_at=NOW + timedelta(seconds=5),
                        event=TaskEventDraft(
                            "event-waiting",
                            "TASK_APPROVED",
                            {},
                            NOW + timedelta(seconds=5),
                        ),
                    )

                current = store.get_task("task-1")
                approvals = store.list_approvals("task-1")
                events = store.list_task_events("task-1")

        self.assertEqual(current, waiting)
        self.assertEqual(approvals, ())
        self.assertEqual(len(events), 4)

    def test_returns_plan_conflict_and_decision_key_conflict_deterministically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            waiting = self._create_waiting_task(database_path)
            stale_plan_approval = Approval.grant_for(
                waiting,
                at=NOW + timedelta(seconds=4),
            )
            changed = waiting.update_plan(
                "sha256:plan-v2",
                at=NOW + timedelta(seconds=5),
            )
            current_approval = Approval.grant_for(
                changed,
                at=NOW + timedelta(seconds=6),
            )

            with SQLiteStore(database_path) as store:
                store.save_task(
                    changed,
                    expected_version=waiting.version,
                    event=TaskEventDraft(
                        "event-plan-v2", "PLAN_UPDATED", {}, changed.updated_at
                    ),
                )
                with self.assertRaises(PlanChangedError):
                    store.apply_approval(
                        stale_plan_approval,
                        decision_id="decision-stale-plan",
                        resumed_at=NOW + timedelta(seconds=7),
                        event=TaskEventDraft(
                            "event-stale-plan", "TASK_APPROVED", {}, NOW
                        ),
                    )
                store.apply_approval(
                    current_approval,
                    decision_id="decision-current",
                    resumed_at=NOW + timedelta(seconds=7),
                    event=TaskEventDraft(
                        "event-approved",
                        "TASK_APPROVED",
                        {},
                        NOW + timedelta(seconds=7),
                    ),
                )
                changed_delivery = Approval(
                    task_id=current_approval.task_id,
                    task_version=current_approval.task_version,
                    plan_hash=current_approval.plan_hash,
                    approved_at=NOW + timedelta(seconds=7),
                )
                with self.assertRaises(ApprovalConflictError):
                    store.apply_approval(
                        changed_delivery,
                        decision_id="decision-current",
                        resumed_at=NOW + timedelta(seconds=8),
                        event=TaskEventDraft(
                            "event-conflict", "TASK_APPROVED", {}, NOW
                        ),
                    )

    @staticmethod
    def _create_waiting_task(database_path: Path) -> Task:
        upgrade_database(database_path)
        received = Task.receive(task_id="task-1", input="노드 점검", at=NOW)
        running = received.transition(
            Status.RUNNING,
            at=NOW + timedelta(seconds=1),
        )
        planned = running.update_plan(
            "sha256:plan-v1",
            at=NOW + timedelta(seconds=2),
        )
        waiting = planned.transition(
            Status.WAITING_APPROVAL,
            at=NOW + timedelta(seconds=3),
        )
        snapshots = (
            (received, None, "event-received", "TASK_RECEIVED"),
            (running, received.version, "event-running", "TASK_RUNNING"),
            (planned, running.version, "event-plan", "PLAN_UPDATED"),
            (waiting, planned.version, "event-waiting", "WAITING_APPROVAL"),
        )
        with SQLiteStore(database_path) as store:
            for task, expected_version, event_id, event_type in snapshots:
                event = TaskEventDraft(
                    event_id,
                    event_type,
                    {},
                    task.updated_at,
                )
                if expected_version is None:
                    store.create_task(task, event=event)
                else:
                    store.save_task(
                        task,
                        expected_version=expected_version,
                        event=event,
                    )
        return waiting


class RecoveryAndCheckpointTests(unittest.TestCase):
    """startup recovery 분류와 checkpoint connection 수명을 검증한다."""

    def test_recovers_only_non_terminal_tasks_and_keeps_approval_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            running = self._create_planless_task(
                database_path,
                task_id="task-running",
                terminal=False,
            )
            waiting = self._create_waiting_task(
                database_path,
                task_id="task-waiting",
            )
            self._create_planless_task(
                database_path,
                task_id="task-completed",
                terminal=True,
            )
            running_workflow = WorkflowRun.start(
                workflow_run_id="workflow-running",
                task=running,
                max_agent_runs=2,
                at=running.updated_at,
            )
            waiting_workflow = WorkflowRun.start(
                workflow_run_id="workflow-waiting",
                task=waiting,
                max_agent_runs=2,
                at=waiting.updated_at,
            )

            with SQLiteStore(database_path) as store:
                store.create_workflow_run(running_workflow)
                store.create_workflow_run(waiting_workflow)
                candidates = store.list_recovery_candidates()

        self.assertEqual(
            [
                (candidate.task.task_id, candidate.disposition)
                for candidate in candidates
            ],
            [
                ("task-running", RecoveryDisposition.RESUME),
                ("task-waiting", RecoveryDisposition.WAITING_APPROVAL),
            ],
        )
        self.assertEqual(candidates[0].thread_id, "task-running")
        self.assertEqual(candidates[1].thread_id, "task-waiting")

    def test_resumes_an_interrupted_graph_with_a_new_owned_connection(self) -> None:
        class CheckpointState(TypedDict):
            result: str

        def wait_for_approval(_state: CheckpointState) -> dict[str, str]:
            decision = interrupt("승인이 필요합니다.")
            return {"result": str(decision)}

        builder = StateGraph(CheckpointState)
        builder.add_node("wait_for_approval", wait_for_approval)
        builder.add_edge(START, "wait_for_approval")
        builder.add_edge("wait_for_approval", END)

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"
            upgrade_database(database_path)
            task = self._create_planless_task(
                database_path,
                task_id="task-running",
                terminal=False,
            )
            workflow = WorkflowRun.start(
                workflow_run_id="workflow-running",
                task=task,
                max_agent_runs=1,
                at=task.updated_at,
            )
            config = {"configurable": {"thread_id": task.task_id}}

            with SQLiteStore(database_path) as store:
                store.create_workflow_run(workflow)
                with store.open_checkpointer() as checkpointer:
                    graph = builder.compile(checkpointer=checkpointer)
                    interrupted = graph.invoke({"result": "pending"}, config)
                    self.assertIn("__interrupt__", interrupted)
                    self.assertEqual(
                        checkpointer.conn.execute("PRAGMA journal_mode").fetchone(),
                        ("wal",),
                    )
                    self.assertEqual(
                        checkpointer.conn.execute("PRAGMA foreign_keys").fetchone(),
                        (1,),
                    )
                    self.assertEqual(
                        checkpointer.conn.execute("PRAGMA busy_timeout").fetchone(),
                        (5000,),
                    )
                    closed_connection = checkpointer.conn

            with self.assertRaises(sqlite3.ProgrammingError):
                closed_connection.execute("SELECT 1")

            with SQLiteStore(database_path) as reopened:
                candidate = reopened.list_recovery_candidates()[0]
                with reopened.open_checkpointer() as checkpointer:
                    resumed_graph = builder.compile(checkpointer=checkpointer)
                    resumed = resumed_graph.invoke(
                        Command(resume="approved"),
                        {"configurable": {"thread_id": candidate.thread_id}},
                    )

            with sqlite3.connect(database_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertEqual(resumed["result"], "approved")
        self.assertIn("tasks", tables)
        self.assertIn("checkpoints", tables)
        self.assertIn("writes", tables)

    @staticmethod
    def _create_planless_task(
        database_path: Path,
        *,
        task_id: str,
        terminal: bool,
    ) -> Task:
        received = Task.receive(task_id=task_id, input="노드 점검", at=NOW)
        running = received.transition(
            Status.RUNNING,
            at=NOW + timedelta(seconds=1),
        )
        snapshots = [received, running]
        if terminal:
            snapshots.append(
                running.transition(
                    Status.COMPLETED,
                    at=NOW + timedelta(seconds=2),
                )
            )
        with SQLiteStore(database_path) as store:
            for index, task in enumerate(snapshots, start=1):
                event = TaskEventDraft(
                    f"event-{task_id}-{index}",
                    f"TASK_{task.status.value}",
                    {},
                    task.updated_at,
                )
                if index == 1:
                    store.create_task(task, event=event)
                else:
                    store.save_task(
                        task,
                        expected_version=index - 1,
                        event=event,
                    )
        return snapshots[-1]

    @staticmethod
    def _create_waiting_task(database_path: Path, *, task_id: str) -> Task:
        received = Task.receive(task_id=task_id, input="변경 요청", at=NOW)
        running = received.transition(Status.RUNNING, at=NOW + timedelta(seconds=1))
        planned = running.update_plan(
            "sha256:plan-v1",
            at=NOW + timedelta(seconds=2),
        )
        waiting = planned.transition(
            Status.WAITING_APPROVAL,
            at=NOW + timedelta(seconds=3),
        )
        with SQLiteStore(database_path) as store:
            for index, task in enumerate(
                (received, running, planned, waiting),
                start=1,
            ):
                event = TaskEventDraft(
                    f"event-{task_id}-{index}",
                    f"TASK_{task.status.value}",
                    {},
                    task.updated_at,
                )
                if index == 1:
                    store.create_task(task, event=event)
                else:
                    store.save_task(
                        task,
                        expected_version=index - 1,
                        event=event,
                    )
        return waiting


if __name__ == "__main__":
    unittest.main()
