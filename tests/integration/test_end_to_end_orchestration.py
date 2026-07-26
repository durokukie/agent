"""외부 요청부터 알림 전달까지 실제 로컬 adapter를 잇는 수용 테스트."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import httpx

from agent_system.agents import (
    Agent,
    AgentMetadata,
    AgentOutcome,
    AgentRegistry,
    AgentRequest,
    AgentResult,
    FakeAgent,
)
from agent_system.config import RuntimeSettings
from agent_system.http import create_app
from agent_system.models import ModelSettings
from agent_system.notifications import FakeNotificationSender
from agent_system.orchestration import (
    ActionKind,
    ActionPlan,
    FakeGovernance,
    FakeRequestClassifier,
    RequestKind,
    RoutingDecision,
    Status,
)
from agent_system.persistence import OutboxStatus, SQLiteStore
from agent_system.runtime import RuntimeApplication, build_runtime

NOW = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
E2E_TIMEOUT_SECONDS = 10.0
_T = TypeVar("_T")


async def _await_bounded(operation: str, awaitable: Awaitable[_T]) -> _T:
    """E2E 비동기 경계의 hang을 operation 이름이 있는 실패로 바꾼다."""

    timeout_scope = asyncio.timeout(E2E_TIMEOUT_SECONDS)
    try:
        async with timeout_scope:
            return await awaitable
    except TimeoutError as error:
        if not timeout_scope.expired():
            raise
        raise AssertionError(
            f"{operation}이 {E2E_TIMEOUT_SECONDS:g}초 안에 끝나지 않았습니다."
        ) from error


class _StepClock:
    """여러 background task에서도 서로 다른 UTC instant를 발급한다."""

    def __init__(self, *, start: datetime = NOW) -> None:
        self._next = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = self._next
            self._next += timedelta(milliseconds=1)
            return value


class _StableIds:
    """Application instance별 namespace에서 stable ID를 발급한다."""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._next = 1
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            value = f"{self._namespace}-{self._next}"
            self._next += 1
            return value


class _RuntimeSession:
    """실제 FastAPI lifespan과 HTTP client 자원을 함께 소유하는 test utility."""

    def __init__(self, application: RuntimeApplication) -> None:
        self.application = application
        self.web = create_app(application)
        self._lifespan = self.web.router.lifespan_context(self.web)
        self.client: httpx.AsyncClient | None = None
        self._lifespan_entered = False

    async def open(self) -> _RuntimeSession:
        await _await_bounded("FastAPI lifespan startup", self._lifespan.__aenter__())
        self._lifespan_entered = True
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.web),
            base_url="http://test",
        )
        return self

    async def close(self) -> None:
        primary_error: BaseException | None = None
        if self.client is not None:
            client = self.client
            self.client = None
            try:
                await _await_bounded("ASGI HTTP client 종료", client.aclose())
            except BaseException as error:  # noqa: BLE001 - lifespan 정리까지 계속한다.
                primary_error = error
        if self._lifespan_entered:
            self._lifespan_entered = False
            try:
                await _await_bounded(
                    "FastAPI lifespan shutdown",
                    self._lifespan.__aexit__(None, None, None),
                )
            except BaseException as error:  # noqa: BLE001 - 앞선 정리 오류를 보존한다.
                if primary_error is None:
                    primary_error = error
                else:
                    primary_error.add_note("FastAPI lifespan 정리도 실패했습니다.")
        if primary_error is not None:
            raise primary_error

    async def drain(self, stage: str) -> None:
        """Runtime queue와 notification outbox를 stage별 timeout 안에 비운다."""

        await _await_bounded(
            f"{stage} runtime/outbox drain",
            self.application.drain(),
        )


class _CrashAfterEffectAgent:
    """첫 effect 직후 process cancellation을 재현하고 replay를 멱등 처리한다."""

    def __init__(self) -> None:
        self._metadata = AgentMetadata(
            "operations-agent",
            "복구 Agent",
            "idempotency key로 변경 effect를 보호한다.",
        )
        self.entered = asyncio.Event()
        self.release_crash = asyncio.Event()
        self.calls: list[str] = []
        self.effects: list[str] = []

    @property
    def metadata(self) -> AgentMetadata:
        return self._metadata

    async def run(self, request: AgentRequest) -> AgentResult:
        self.calls.append(request.idempotency_key)
        if request.idempotency_key not in self.effects:
            self.effects.append(request.idempotency_key)
            self.entered.set()
            await _await_bounded(
                "Crash Agent release gate",
                self.release_crash.wait(),
            )
            raise asyncio.CancelledError
        return AgentResult(
            agent_id=self.metadata.agent_id,
            outcome=AgentOutcome.SUCCESS,
            output="복구 effect 확인 완료",
        )


class EndToEndOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    """실제 ASGI, graph, SQLite와 outbox를 하나의 사용자 여정으로 검증한다."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "agent-system.sqlite3"
        self.sessions: list[_RuntimeSession] = []

    async def asyncTearDown(self) -> None:
        primary_error: BaseException | None = None
        while self.sessions:
            try:
                await _await_bounded(
                    "E2E session teardown",
                    self.sessions.pop().close(),
                )
            except BaseException as error:  # noqa: BLE001 - 남은 session도 정리한다.
                if primary_error is None:
                    primary_error = error
                else:
                    primary_error.add_note("다른 E2E session 정리도 실패했습니다.")
        try:
            self.directory.cleanup()
        except BaseException as error:  # noqa: BLE001 - async 정리 오류를 primary로 보존한다.
            if primary_error is None:
                primary_error = error
            else:
                primary_error.add_note("임시 SQLite 디렉터리 정리도 실패했습니다.")
        if primary_error is not None:
            raise primary_error

    def _settings(self, *, max_agent_runs: int = 2) -> RuntimeSettings:
        return RuntimeSettings(
            db_path=self.database_path,
            model_settings=ModelSettings("upstage", "solar", "not-called"),
            max_agent_runs=max_agent_runs,
            queue_capacity=4,
            worker_count=1,
        )

    async def _open_runtime(
        self,
        *,
        namespace: str,
        agent: Agent,
        sender: FakeNotificationSender,
        max_agent_runs: int = 2,
        clock: _StepClock | None = None,
    ) -> _RuntimeSession:
        plan = ActionPlan(
            summary="장애 서비스를 안전하게 재시작합니다.",
            steps=("현재 상태 확인", "서비스 재시작", "정상 응답 검증"),
        )
        classifier = FakeRequestClassifier(
            {
                RequestKind.ALERT: RoutingDecision(
                    request_kind=RequestKind.ALERT,
                    agent_id="operations-agent",
                    action=ActionKind.MUTATING,
                    reason="자동 복구에 변경 작업이 필요합니다.",
                    plan=plan,
                )
            }
        )
        registry = AgentRegistry()
        registry.register(agent)
        application = build_runtime(
            self._settings(max_agent_runs=max_agent_runs),
            classifier=classifier,
            governance=FakeGovernance(approved=True, reason="정책 허용"),
            registry=registry,
            clock=clock or _StepClock(),
            id_factory=_StableIds(namespace),
            notification_sender=sender,
            notification_id_factory=_StableIds(f"{namespace}-notification-lease"),
        )
        session = await _RuntimeSession(application).open()
        self.sessions.append(session)
        return session

    @staticmethod
    async def _submit_alert(session: _RuntimeSession, alert_id: str) -> str:
        assert session.client is not None
        response = await _await_bounded(
            "Alert webhook POST",
            session.client.post(
                "/v1/webhooks/alerts",
                json={
                    "alert_id": alert_id,
                    "severity": "critical",
                    "message": "checkout 서비스가 응답하지 않습니다.",
                },
            ),
        )
        if response.status_code != 202:
            raise AssertionError(response.text)
        return response.json()["task_id"]

    @staticmethod
    async def _get_task(session: _RuntimeSession, task_id: str) -> dict[str, object]:
        assert session.client is not None
        response = await _await_bounded(
            "Task GET",
            session.client.get(f"/v1/tasks/{task_id}"),
        )
        if response.status_code != 200:
            raise AssertionError(response.text)
        return response.json()

    @staticmethod
    async def _decide(
        session: _RuntimeSession,
        task_id: str,
        waiting: dict[str, object],
        *,
        decision: str,
        decision_id: str,
    ) -> None:
        assert session.client is not None
        approval = waiting["approval"]
        assert isinstance(approval, dict)
        payload: dict[str, object] = {
            "decision_id": decision_id,
            "decision": decision,
            "task_version": waiting["version"],
            "plan_hash": approval["plan_hash"],
        }
        if decision == "reject":
            payload["reason"] = "현재 변경 창구가 닫혔습니다."
        response = await _await_bounded(
            "Approval POST",
            session.client.post(
                f"/v1/tasks/{task_id}/approval",
                json=payload,
            ),
        )
        if response.status_code != 202:
            raise AssertionError(response.text)

    async def test_mutating_alert_waits_for_bound_approval_then_completes_and_notifies(
        self,
    ) -> None:
        """승인 binding, 실행·검증 또는 두 상태 notification이 끊기면 실패한다."""

        sender = FakeNotificationSender()
        agent = FakeAgent(
            AgentMetadata("operations-agent", "운영 Agent", "복구 실행"),
            output="서비스 재시작과 정상 응답 검증 완료",
        )
        session = await self._open_runtime(
            namespace="happy", agent=agent, sender=sender
        )

        task_id = await self._submit_alert(session, "alert-happy")
        await session.drain("승인 대기")
        waiting = await self._get_task(session, task_id)

        self.assertEqual(waiting["status"], "WAITING_APPROVAL")
        approval = waiting["approval"]
        self.assertIsInstance(approval, dict)
        assert isinstance(approval, dict)
        self.assertEqual(approval["task_version"], waiting["version"])
        self.assertTrue(str(approval["plan_hash"]).startswith("sha256:"))
        self.assertEqual(len(str(approval["plan_hash"])), 71)
        self.assertEqual(
            [notification.status for notification in sender.sent],
            ["WAITING_APPROVAL"],
        )

        await self._decide(
            session,
            task_id,
            waiting,
            decision="approve",
            decision_id="decision-happy",
        )
        await session.drain("승인 완료")
        completed = await self._get_task(session, task_id)

        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(
            completed["result"],
            {
                "output": "서비스 재시작과 정상 응답 검증 완료",
                "agent_run_count": 1,
            },
        )
        self.assertEqual(len(agent.received_requests), 1)
        self.assertEqual(
            [notification.status for notification in sender.sent],
            ["WAITING_APPROVAL", "COMPLETED"],
        )
        self.assertEqual(
            [notification.task_id for notification in sender.sent],
            [task_id, task_id],
        )

        with SQLiteStore(self.database_path) as store:
            self.assertEqual(
                [event.event_type for event in store.list_task_events(task_id)],
                [
                    "TASK_RECEIVED",
                    "TASK_STARTED",
                    "TASK_PLAN_UPDATED",
                    "TASK_WAITING_APPROVAL",
                    "TASK_APPROVED",
                    "TASK_COMPLETED",
                ],
            )
            workflow = store.get_workflow_run_for_task(task_id)
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.phase.value, "VERIFYING")
            runs = store.list_agent_runs(workflow.workflow_run_id)
            self.assertEqual(len(runs), 1)
            self.assertTrue(runs[0].is_completed)
            outbox = store.list_outbox()
            self.assertEqual(len(outbox), 2)
            self.assertTrue(all(row.status is OutboxStatus.DELIVERED for row in outbox))

    async def test_human_rejection_is_terminal_and_notified_without_agent_execution(
        self,
    ) -> None:
        """거절 CAS가 Agent를 호출하거나 REJECTED 감사·알림을 누락하면 실패한다."""

        sender = FakeNotificationSender()
        agent = FakeAgent(AgentMetadata("operations-agent", "운영 Agent", "복구 실행"))
        session = await self._open_runtime(
            namespace="reject", agent=agent, sender=sender
        )
        task_id = await self._submit_alert(session, "alert-reject")
        await session.drain("거절 승인 대기")
        waiting = await self._get_task(session, task_id)

        await self._decide(
            session,
            task_id,
            waiting,
            decision="reject",
            decision_id="decision-reject",
        )
        await session.drain("사람 거절")
        rejected = await self._get_task(session, task_id)

        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["errors"], ["human_rejected"])
        self.assertEqual(agent.received_requests, [])
        self.assertEqual(
            [notification.status for notification in sender.sent],
            ["WAITING_APPROVAL", "REJECTED"],
        )
        with SQLiteStore(self.database_path) as store:
            terminal = store.list_task_events(task_id)[-1]
            self.assertEqual(terminal.event_type, "TASK_REJECTED")
            self.assertEqual(terminal.payload["decision_id"], "decision-reject")

    async def test_action_failure_exhausts_retry_budget_and_notifies_escalation(
        self,
    ) -> None:
        """실패 retry 횟수, terminal 정책 또는 ESCALATED 알림이 바뀌면 실패한다."""

        sender = FakeNotificationSender()
        agent = FakeAgent(
            AgentMetadata("operations-agent", "운영 Agent", "복구 실행"),
            outcome=AgentOutcome.FAILURE,
            output="외부에 노출하면 안 되는 상세",
        )
        session = await self._open_runtime(
            namespace="failure",
            agent=agent,
            sender=sender,
            max_agent_runs=2,
        )
        task_id = await self._submit_alert(session, "alert-failure")
        await session.drain("실패 시나리오 승인 대기")
        waiting = await self._get_task(session, task_id)

        await self._decide(
            session,
            task_id,
            waiting,
            decision="approve",
            decision_id="decision-failure",
        )
        await session.drain("Agent retry 소진")
        escalated = await self._get_task(session, task_id)

        self.assertEqual(escalated["status"], "ESCALATED")
        self.assertEqual(
            escalated["errors"],
            ["agent_failure", "agent_failure", "retry_exhausted"],
        )
        self.assertNotIn("노출하면 안 되는", str(escalated))
        self.assertEqual(len(agent.received_requests), 2)
        self.assertEqual(
            len({request.idempotency_key for request in agent.received_requests}),
            2,
        )
        self.assertEqual(
            [notification.status for notification in sender.sent],
            ["WAITING_APPROVAL", "ESCALATED"],
        )
        with SQLiteStore(self.database_path) as store:
            terminal = store.list_task_events(task_id)[-1]
            self.assertEqual(terminal.event_type, "TASK_ESCALATED")
            workflow = store.get_workflow_run_for_task(task_id)
            assert workflow is not None
            self.assertEqual(
                len(store.list_agent_runs(workflow.workflow_run_id)),
                2,
            )

    async def test_restart_recovers_waiting_and_open_agent_run_without_duplicate_effect(
        self,
    ) -> None:
        """재시작이 승인 대기·열린 issuance를 잃거나 effect/알림을 중복하면 실패한다."""

        agent = _CrashAfterEffectAgent()
        first_sender = FakeNotificationSender()
        first = await self._open_runtime(
            namespace="restart-first",
            agent=agent,
            sender=first_sender,
        )
        task_id = await self._submit_alert(first, "alert-restart")
        await first.drain("재시작 전 승인 대기")
        waiting = await self._get_task(first, task_id)
        self.assertEqual(waiting["status"], "WAITING_APPROVAL")
        await _await_bounded("첫 application 종료", first.close())
        self.sessions.remove(first)

        second_sender = FakeNotificationSender()
        second = await self._open_runtime(
            namespace="restart-second",
            agent=agent,
            sender=second_sender,
            clock=_StepClock(start=NOW + timedelta(minutes=1)),
        )
        recovered_waiting = await self._get_task(second, task_id)
        self.assertEqual(recovered_waiting, waiting)
        self.assertEqual(second_sender.sent, ())

        stopping: asyncio.Task[None] | None = None
        try:
            await self._decide(
                second,
                task_id,
                recovered_waiting,
                decision="approve",
                decision_id="decision-restart",
            )
            await _await_bounded("Crash Agent 진입", agent.entered.wait())
            stopping = asyncio.create_task(second.application.stop())
            # stop()은 첫 await 전에 admission을 닫고 queue drain을 기다린다.
            await asyncio.sleep(0)
        finally:
            # 앞선 assertion/timeout/cancellation에서도 Agent gate를 반드시 연다.
            agent.release_crash.set()
        if stopping is None:
            self.fail(
                "Crash Agent 진입 뒤 application stop task가 생성되지 않았습니다."
            )
        await _await_bounded("두 번째 application crash-stop", stopping)
        await _await_bounded("두 번째 application lifespan 종료", second.close())
        self.sessions.remove(second)

        with SQLiteStore(self.database_path) as crashed_store:
            crashed_task = crashed_store.get_task(task_id)
            self.assertIsNotNone(crashed_task)
            assert crashed_task is not None
            self.assertEqual(crashed_task.status, Status.RUNNING)
            workflow = crashed_store.get_workflow_run_for_task(task_id)
            self.assertIsNotNone(workflow)
            assert workflow is not None
            open_runs = crashed_store.list_agent_runs(workflow.workflow_run_id)
            self.assertEqual(len(open_runs), 1)
            self.assertFalse(open_runs[0].is_completed)

        third_sender = FakeNotificationSender()
        third = await self._open_runtime(
            namespace="restart-third",
            agent=agent,
            sender=third_sender,
            clock=_StepClock(start=NOW + timedelta(minutes=2)),
        )
        await third.drain("열린 AgentRun 재시작 복구")
        completed = await self._get_task(third, task_id)

        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(agent.effects, [agent.calls[0]])
        self.assertEqual(agent.calls, [agent.calls[0], agent.calls[0]])
        self.assertEqual(
            [notification.status for notification in first_sender.sent],
            ["WAITING_APPROVAL"],
        )
        self.assertEqual(second_sender.sent, ())
        self.assertEqual(
            [notification.status for notification in third_sender.sent],
            ["COMPLETED"],
        )
        with SQLiteStore(self.database_path) as recovered_store:
            workflow = recovered_store.get_workflow_run_for_task(task_id)
            assert workflow is not None
            runs = recovered_store.list_agent_runs(workflow.workflow_run_id)
            self.assertEqual(len(runs), 1)
            self.assertTrue(runs[0].is_completed)
            notifications = recovered_store.list_outbox()
            self.assertEqual(len(notifications), 2)
            self.assertTrue(
                all(row.status is OutboxStatus.DELIVERED for row in notifications)
            )


class ServerEntrypointTests(unittest.IsolatedAsyncioTestCase):
    """운영자가 환경 설정만으로 FastAPI factory를 실행할 수 있는지 검증한다."""

    async def test_server_factory_builds_and_closes_runtime_without_provider_call(
        self,
    ) -> None:
        """배포용 factory 또는 runtime lifespan 연결이 사라지면 실패한다."""

        from agent_system.server import create_app_from_env

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "server.sqlite3"
            app = create_app_from_env(
                {
                    "AGENT_SYSTEM_DB_PATH": str(database_path),
                    "MODEL_PROVIDER": "upstage",
                    "MODEL_NAME": "solar",
                    "UPSTAGE_API_KEY": "not-called",
                }
            )
            lifespan = app.router.lifespan_context(app)
            await _await_bounded("Server factory startup", lifespan.__aenter__())
            await _await_bounded(
                "Server factory shutdown",
                lifespan.__aexit__(None, None, None),
            )

            self.assertEqual(app.title, "Agent System")
            self.assertTrue(database_path.is_file())


if __name__ == "__main__":
    unittest.main()
