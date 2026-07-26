"""FastAPI 기반 HTTP adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from agent_system.runtime import (
    AcceptedTask,
    ApplicationBusyError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApprovalCommand,
    ApprovalDecision,
    CancelCommand,
    Submission,
    SubmissionKind,
    TaskApplication,
    TaskView,
)

TrimmedText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]

_STARTUP_CLEANUP_TIMEOUT_SECONDS = 5.0
_STARTUP_CLEANUP_TASKS: set[asyncio.Task[None]] = set()


def _retrieve_startup_cleanup(task: asyncio.Task[None]) -> None:
    """Deadline 뒤 끝난 cleanup 예외를 회수하고 strong reference를 제거한다."""

    _STARTUP_CLEANUP_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:  # noqa: BLE001 - background Task 예외를 소비한다.
        return


async def _cleanup_after_start_failure(
    application: TaskApplication,
    start_error: BaseException,
) -> None:
    """Startup primary 오류를 보존하며 cleanup에 hard grace period를 적용한다."""

    cleanup = asyncio.create_task(
        application.stop(),
        name="agent-system-startup-cleanup",
    )
    _STARTUP_CLEANUP_TASKS.add(cleanup)
    cleanup.add_done_callback(_retrieve_startup_cleanup)
    try:
        done, _pending = await asyncio.wait(
            {cleanup},
            timeout=_STARTUP_CLEANUP_TIMEOUT_SECONDS,
        )
    except BaseException:  # noqa: BLE001 - startup primary 오류를 보존한다.
        cleanup.cancel()
        start_error.add_note("Startup 실패 뒤 application 정리 대기가 중단되었습니다.")
        return
    if cleanup not in done:
        cleanup.cancel()
        start_error.add_note(
            "Startup 실패 뒤 application 정리가 제한 시간 안에 끝나지 않았습니다."
        )
        return
    if cleanup.cancelled():
        start_error.add_note("Startup 실패 뒤 application 정리도 취소되었습니다.")
        return
    try:
        cleanup.result()
    except BaseException:  # noqa: BLE001 - cleanup 상세를 startup 오류에 노출하지 않는다.
        start_error.add_note("Startup 실패 뒤 application 정리도 실패했습니다.")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UserTaskRequest(_StrictModel):
    """일반 Task 생성 payload."""

    input: TrimmedText


class AlertWebhookRequest(_StrictModel):
    """Alert webhook payload."""

    alert_id: ShortText
    severity: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    message: TrimmedText


class TicketWebhookRequest(_StrictModel):
    """Ticket webhook payload."""

    ticket_id: ShortText
    subject: ShortText
    description: TrimmedText


class ApprovalRequest(_StrictModel):
    """Task version과 plan에 결합된 승인·거절 payload."""

    decision_id: ShortText
    decision: Literal["approve", "reject"]
    task_version: int = Field(strict=True, ge=1)
    plan_hash: ShortText
    reason: TrimmedText | None = None

    @model_validator(mode="after")
    def require_rejection_reason(self) -> Self:
        if self.decision == "reject" and self.reason is None:
            raise ValueError("거절 결정에는 reason이 필요합니다.")
        return self


class CancelRequest(_StrictModel):
    """Optimistic Task version 취소 payload."""

    expected_version: int = Field(strict=True, ge=1)
    reason: TrimmedText | None = None


class AcceptedTaskResponse(_StrictModel):
    """비동기 명령의 202 응답."""

    task_id: str
    status: str
    version: int
    replayed: bool


class ErrorBody(_StrictModel):
    """상세 예외를 숨기는 안정적인 오류 값."""

    code: str
    message: str


class ErrorResponse(_StrictModel):
    """모든 application 오류의 공통 envelope."""

    error: ErrorBody


class ApprovalMetadataResponse(_StrictModel):
    """조회 응답의 사람 승인 metadata."""

    task_version: int
    plan_hash: str
    plan_summary: str
    plan_steps: list[str]
    agent_id: str
    action: str


class ResultMetadataResponse(_StrictModel):
    """조회 응답의 실행 결과 metadata."""

    output: str | None
    agent_run_count: int


class TaskResponse(_StrictModel):
    """Persistence authority 기반 Task 조회 응답."""

    task_id: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    approval: ApprovalMetadataResponse | None
    result: ResultMetadataResponse | None
    errors: list[str]


def _accepted(value: AcceptedTask) -> AcceptedTaskResponse:
    return AcceptedTaskResponse(
        task_id=value.task_id,
        status=value.status,
        version=value.version,
        replayed=value.replayed,
    )


def _task_response(value: TaskView) -> TaskResponse:
    approval = None
    if value.approval is not None:
        approval = ApprovalMetadataResponse(
            task_version=value.approval.task_version,
            plan_hash=value.approval.plan_hash,
            plan_summary=value.approval.plan_summary,
            plan_steps=list(value.approval.plan_steps),
            agent_id=value.approval.agent_id,
            action=value.approval.action,
        )
    result = None
    if value.result is not None:
        result = ResultMetadataResponse(
            output=value.result.output,
            agent_run_count=value.result.agent_run_count,
        )
    return TaskResponse(
        task_id=value.task_id,
        status=value.status,
        version=value.version,
        created_at=value.created_at,
        updated_at=value.updated_at,
        approval=approval,
        result=result,
        errors=list(value.errors),
    )


def _error(status_code: int, *, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump())


def create_app(application: TaskApplication) -> FastAPI:
    """주입된 runtime interface만 호출하는 FastAPI application을 만든다."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await application.start()
        except BaseException as start_error:
            await _cleanup_after_start_failure(application, start_error)
            raise
        try:
            yield
        finally:
            await application.stop()

    app = FastAPI(title="Agent System", version="1", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request,
        _error_value: RequestValidationError,
    ) -> JSONResponse:
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message="요청 payload가 올바르지 않습니다.",
        )

    @app.exception_handler(ApplicationNotFoundError)
    async def not_found(
        _request: Request,
        _error_value: ApplicationNotFoundError,
    ) -> JSONResponse:
        return _error(
            status.HTTP_404_NOT_FOUND,
            code="task_not_found",
            message="Task를 찾을 수 없습니다.",
        )

    @app.exception_handler(ApplicationConflictError)
    async def conflict(
        _request: Request,
        _error_value: ApplicationConflictError,
    ) -> JSONResponse:
        return _error(
            status.HTTP_409_CONFLICT,
            code="task_conflict",
            message="Task 명령이 현재 상태와 충돌합니다.",
        )

    @app.exception_handler(ApplicationBusyError)
    async def busy(
        _request: Request,
        _error_value: ApplicationBusyError,
    ) -> JSONResponse:
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="runner_busy",
            message="작업 queue가 가득 찼습니다.",
        )

    @app.post(
        "/v1/tasks",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
        responses={
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def submit_task(
        body: UserTaskRequest,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=255,
                pattern=r"^\S(?:.*\S)?$",
            ),
        ] = None,
    ) -> AcceptedTaskResponse:
        accepted = await application.submit(
            Submission(
                kind=SubmissionKind.USER_TASK,
                payload=body.model_dump(),
                idempotency_key=idempotency_key,
            )
        )
        return _accepted(accepted)

    @app.post(
        "/v1/webhooks/alerts",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
        responses={
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def submit_alert(body: AlertWebhookRequest) -> AcceptedTaskResponse:
        accepted = await application.submit(
            Submission(
                kind=SubmissionKind.ALERT,
                payload=body.model_dump(),
            )
        )
        return _accepted(accepted)

    @app.post(
        "/v1/webhooks/tickets",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
        responses={
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def submit_ticket(body: TicketWebhookRequest) -> AcceptedTaskResponse:
        accepted = await application.submit(
            Submission(
                kind=SubmissionKind.TICKET,
                payload=body.model_dump(),
            )
        )
        return _accepted(accepted)

    @app.get(
        "/v1/tasks/{task_id}",
        response_model=TaskResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_task(task_id: str) -> TaskResponse:
        return _task_response(await application.get_task(task_id))

    @app.post(
        "/v1/tasks/{task_id}/approval",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
        responses={
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def approve_task(
        task_id: str,
        body: ApprovalRequest,
    ) -> AcceptedTaskResponse:
        accepted = await application.approve(
            task_id,
            ApprovalCommand(
                decision_id=body.decision_id,
                decision=ApprovalDecision(body.decision),
                task_version=body.task_version,
                plan_hash=body.plan_hash,
                reason=body.reason,
            ),
        )
        return _accepted(accepted)

    @app.post(
        "/v1/tasks/{task_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
        responses={
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def cancel_task(
        task_id: str,
        body: CancelRequest,
    ) -> AcceptedTaskResponse:
        accepted = await application.cancel(
            task_id,
            CancelCommand(
                expected_version=body.expected_version,
                reason=body.reason,
            ),
        )
        return _accepted(accepted)

    return app


__all__ = [
    "AcceptedTaskResponse",
    "AlertWebhookRequest",
    "ApprovalRequest",
    "CancelRequest",
    "ErrorResponse",
    "TaskResponse",
    "TicketWebhookRequest",
    "UserTaskRequest",
    "create_app",
]
