"""FastAPI adapter의 공개 HTTP 계약을 검증한다."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import httpx

from agent_system.http import create_app
from agent_system.runtime import (
    AcceptedTask,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApprovalCommand,
    ApprovalDecision,
    ApprovalView,
    CancelCommand,
    ResultView,
    Submission,
    TaskApplication,
    TaskView,
)

NOW = datetime(2026, 7, 26, 1, 2, 3, tzinfo=UTC)


class _RecordingApplication(TaskApplication):
    """HTTP 변환 결과를 기록하는 외부 I/O 없는 runtime fake다."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.submissions: list[Submission] = []
        self.approvals: list[tuple[str, ApprovalCommand]] = []
        self.cancellations: list[tuple[str, CancelCommand]] = []
        self.read_error: Exception | None = None
        self.view = TaskView(
            task_id="task-1",
            status="RECEIVED",
            version=1,
            created_at=NOW,
            updated_at=NOW,
            approval=None,
            result=None,
            errors=(),
        )

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def submit(self, submission: Submission) -> AcceptedTask:
        self.submissions.append(submission)
        return AcceptedTask(
            task_id="task-1",
            status="RECEIVED",
            version=1,
            replayed=False,
        )

    async def get_task(self, task_id: str) -> TaskView:
        if self.read_error is not None:
            raise self.read_error
        return self.view

    async def approve(
        self,
        task_id: str,
        command: ApprovalCommand,
    ) -> AcceptedTask:
        self.approvals.append((task_id, command))
        return AcceptedTask(task_id, "RUNNING", 5, False)

    async def cancel(
        self,
        task_id: str,
        command: CancelCommand,
    ) -> AcceptedTask:
        self.cancellations.append((task_id, command))
        return AcceptedTask(task_id, "CANCELLED", 3, False)


class HttpApiContractTests(unittest.IsolatedAsyncioTestCase):
    """HTTP adapter가 runtime interface만 호출하는지 검증한다."""

    async def asyncSetUp(self) -> None:
        self.application = _RecordingApplication()
        self.app = create_app(self.application)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def test_accepts_a_user_task_with_an_explicit_idempotency_header(
        self,
    ) -> None:
        """POST route나 header 변환이 제거되면 실패한다."""

        response = await self.client.post(
            "/v1/tasks",
            headers={"Idempotency-Key": "request-42"},
            json={"input": "현재 장애 영향을 조사해 주세요."},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(),
            {
                "task_id": "task-1",
                "status": "RECEIVED",
                "version": 1,
                "replayed": False,
            },
        )
        self.assertEqual(len(self.application.submissions), 1)
        submission = self.application.submissions[0]
        self.assertEqual(submission.kind.value, "user_task")
        self.assertEqual(
            submission.payload,
            {"input": "현재 장애 영향을 조사해 주세요."},
        )
        self.assertEqual(submission.idempotency_key, "request-42")

    async def test_rejects_malformed_and_extra_payload_without_calling_runtime(
        self,
    ) -> None:
        """Strict body schema나 input bound가 빠지면 실패한다."""

        empty = await self.client.post("/v1/tasks", json={"input": "   "})
        extra = await self.client.post(
            "/v1/webhooks/alerts",
            json={
                "alert_id": "alert-1",
                "severity": "critical",
                "message": "database unavailable",
                "api_key": "must-not-be-reflected",
            },
        )
        rejection_without_reason = await self.client.post(
            "/v1/tasks/task-1/approval",
            json={
                "decision_id": "decision-invalid",
                "decision": "reject",
                "task_version": 4,
                "plan_hash": "sha256:plan",
            },
        )

        self.assertEqual(empty.status_code, 422)
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(rejection_without_reason.status_code, 422)
        self.assertEqual(
            empty.json(),
            {
                "error": {
                    "code": "invalid_request",
                    "message": "요청 payload가 올바르지 않습니다.",
                }
            },
        )
        self.assertNotIn("must-not-be-reflected", extra.text)
        self.assertEqual(self.application.submissions, [])

    async def test_accepts_alert_and_ticket_webhooks_with_stable_identity_fields(
        self,
    ) -> None:
        """Webhook route 또는 identity field 전달이 빠지면 실패한다."""

        alert = await self.client.post(
            "/v1/webhooks/alerts",
            json={
                "alert_id": "alert-7",
                "severity": "critical",
                "message": "database unavailable",
            },
        )
        ticket = await self.client.post(
            "/v1/webhooks/tickets",
            json={
                "ticket_id": "ticket-9",
                "subject": "로그인 실패",
                "description": "신규 사용자가 로그인할 수 없습니다.",
            },
        )

        self.assertEqual(alert.status_code, 202)
        self.assertEqual(ticket.status_code, 202)
        self.assertEqual(
            [
                (value.kind.value, dict(value.payload))
                for value in self.application.submissions
            ],
            [
                (
                    "alert",
                    {
                        "alert_id": "alert-7",
                        "severity": "critical",
                        "message": "database unavailable",
                    },
                ),
                (
                    "ticket",
                    {
                        "ticket_id": "ticket-9",
                        "subject": "로그인 실패",
                        "description": "신규 사용자가 로그인할 수 없습니다.",
                    },
                ),
            ],
        )

    async def test_get_returns_approval_result_and_sanitized_error_metadata(
        self,
    ) -> None:
        """조회 schema가 실행 metadata를 누락하면 실패한다."""

        self.application.view = TaskView(
            task_id="task-1",
            status="WAITING_APPROVAL",
            version=4,
            created_at=NOW,
            updated_at=NOW,
            approval=ApprovalView(
                task_version=4,
                plan_hash="sha256:plan",
                plan_summary="재시작",
                plan_steps=("배포 중지", "서비스 재시작"),
                agent_id="operations-agent",
                action="mutating",
            ),
            result=ResultView(output=None, agent_run_count=0),
            errors=("governance_rejected",),
        )

        response = await self.client.get("/v1/tasks/task-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "task_id": "task-1",
                "status": "WAITING_APPROVAL",
                "version": 4,
                "created_at": "2026-07-26T01:02:03Z",
                "updated_at": "2026-07-26T01:02:03Z",
                "approval": {
                    "task_version": 4,
                    "plan_hash": "sha256:plan",
                    "plan_summary": "재시작",
                    "plan_steps": ["배포 중지", "서비스 재시작"],
                    "agent_id": "operations-agent",
                    "action": "mutating",
                },
                "result": {"output": None, "agent_run_count": 0},
                "errors": ["governance_rejected"],
            },
        )

    async def test_accepts_approval_rejection_and_cancel_commands(self) -> None:
        """명령 body의 binding 또는 decision 변환이 깨지면 실패한다."""

        accepted = await self.client.post(
            "/v1/tasks/task-1/approval",
            json={
                "decision_id": "decision-1",
                "decision": "approve",
                "task_version": 4,
                "plan_hash": "sha256:plan",
            },
        )
        rejected = await self.client.post(
            "/v1/tasks/task-2/approval",
            json={
                "decision_id": "decision-2",
                "decision": "reject",
                "task_version": 6,
                "plan_hash": "sha256:other",
                "reason": "변경 창구가 닫혔습니다.",
            },
        )
        cancelled = await self.client.post(
            "/v1/tasks/task-3/cancel",
            json={"expected_version": 2, "reason": "사용자 요청"},
        )

        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(rejected.status_code, 202)
        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(
            self.application.approvals,
            [
                (
                    "task-1",
                    ApprovalCommand(
                        "decision-1",
                        ApprovalDecision.APPROVE,
                        4,
                        "sha256:plan",
                    ),
                ),
                (
                    "task-2",
                    ApprovalCommand(
                        "decision-2",
                        ApprovalDecision.REJECT,
                        6,
                        "sha256:other",
                        "변경 창구가 닫혔습니다.",
                    ),
                ),
            ],
        )
        self.assertEqual(
            self.application.cancellations,
            [("task-3", CancelCommand(2, "사용자 요청"))],
        )

    async def test_maps_unknown_and_conflicting_tasks_to_stable_errors(self) -> None:
        """Persistence/provider 예외 문자열이 HTTP body로 새면 실패한다."""

        self.application.read_error = ApplicationNotFoundError(
            "secret database path /private/state.sqlite3"
        )
        missing = await self.client.get("/v1/tasks/missing")
        self.application.read_error = ApplicationConflictError(
            "provider exception bearer-secret"
        )
        conflict = await self.client.get("/v1/tasks/task-1")

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.json(),
            {
                "error": {
                    "code": "task_not_found",
                    "message": "Task를 찾을 수 없습니다.",
                }
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn("bearer-secret", conflict.text)

    async def test_openapi_declares_command_conflict_and_queue_busy_responses(
        self,
    ) -> None:
        """모든 비동기 command endpoint가 409/503 계약을 공개한다."""

        paths = self.app.openapi()["paths"]
        expected = {
            "/v1/tasks": {"202", "409", "422", "503"},
            "/v1/webhooks/alerts": {"202", "409", "422", "503"},
            "/v1/webhooks/tickets": {"202", "409", "422", "503"},
            "/v1/tasks/{task_id}/approval": {
                "202",
                "404",
                "409",
                "422",
                "503",
            },
            "/v1/tasks/{task_id}/cancel": {
                "202",
                "404",
                "409",
                "422",
                "503",
            },
        }
        for path, response_codes in expected.items():
            self.assertEqual(set(paths[path]["post"]["responses"]), response_codes)

    async def test_lifespan_starts_and_stops_the_injected_application(self) -> None:
        """FastAPI lifespan에서 runtime 자원 수명 호출이 빠지면 실패한다."""

        self.assertEqual(self.application.started, 1)
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.assertEqual(self.application.stopped, 1)

        # tearDown의 중복 종료를 피하면서 idempotent stop 계약도 검증한다.
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )
        self.lifespan = _AlreadyExitedLifespan()


class _AlreadyExitedLifespan:
    """명시적으로 종료한 lifespan을 tearDown에서 다시 닫지 않게 한다."""

    async def __aexit__(self, *_args: object) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
