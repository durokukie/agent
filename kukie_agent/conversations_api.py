"""대화(채팅방) 단위 실행 엔드포인트 — kukie-electron docs/api-spec.md 1절.

  POST /conversations/{id}/chat          한 턴. request_id 로 멱등
  POST /conversations/{id}/approve       승인 카드 결정
  POST /conversations/{id}/resume        재개 실패 뒤 다시 시도

채팅방 만들기·목록·조회는 Kukie API 서버(kukie-server)의 공개 API 다 (DURO-113). agent 는 DB 를 갖지 않고,
대화·run·Action Plan 을 server 의 내부 API(kukie_agent/server_api.py)로 읽고 쓴다.

flat 엔드포인트(/session /chat /approve /resume)는 server.py 에 그대로 있다. 실제 run 로직은 server.py 의
_chat_turn / _approve / _resume 을 같이 쓴다 — 여기는 "어느 채팅방인지 찾고, 잠그고, server 에 기록" 만 더한다.

run 은 사용자 요청 한 건이다 (DB 문서 7절). chat 이 승인 카드를 주면 그 run 은 awaiting_approval 로 열려
있고, approve / resume 은 새 run 을 만들지 않고 **그 run 을 이어서** completed 로 끝낸다. 재개가 실패하면
recovery_required 로 남아 /resume 으로 다시 시도한다. 채팅방 하나에 활성 run 은 하나 — 그동안 새 chat 은 409.
run 을 종료 상태로 닫으면 server 가 같은 트랜잭션에서 남은 계획도 닫는다 (결정 있고 결과 없음 → UNKNOWN, 나머지 → EXPIRED).

권한은 server 가 판단한다. 입구 호출이 사용자 토큰으로 묻고 404 / 403 을 준다.
  - 대화 읽기·입력: 대화 컨텍스트 purpose=chat (private 는 주인만, 팀 shared 는 지금 구성원, 팀 없는 방은 주인만)
  - 승인·재개: 대화 컨텍스트 purpose=approve (PRIVATE_SESSION / NOT_TEAM_ADMIN / FORBIDDEN)
  - 클러스터로 실행: 연결 정보 (못 보는 클러스터 404, 방이 복사해 둔 지문과 다르면 409 CLUSTER_CHANGED)
여기서 직접 막는 것은 private 방의 변경 모드 진입(PRIVATE_SESSION) 하나다 — 입력 내용으로 정해지는 규칙이라.

오류는 전부 FastAPI 의 detail 자리에 {code, message} 객체로 간다 — 응답은 {"detail": {code, message}}.
앱의 http.ts 가 detail 이 객체면 code 를 꺼낸다 (api-spec 공통 절). server 의 오류 코드도 같은 상태로 그대로 나간다.
"""
from __future__ import annotations

import dataclasses
import logging
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.tools import ToolApproved

import kukie_agent.server as _server  # 순환 import: 이름은 호출 시점에만 쓴다
from kukie_agent import progress, server_api
from kukie_agent.auth import User, current_user
from kukie_agent.clusters import access
from kukie_agent.conversations import Conversation, registry
from kukie_agent.skills import SKILLS
from kukie_agent.tools.mutate import MUTATING_TOOLS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

#: 결과를 저장하지 못하고 활성으로 남은 run 을 닫을 때 남기는 안내
STALE_MESSAGE = "결과를 저장하지 못해 중단된 요청이다 — 새 요청으로 보내라"


# ── 요청 본문 ──────────────────────────────────────────────

class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    approved: bool


# ── 공통 ───────────────────────────────────────────────────

def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def error_payload(code: str, message: str, http_status: int, **extra: Any) -> dict[str, Any]:
    """run 의 response_payload 에 남기는 실패 형태. http_status 는 같은 request_id 재전송에 같은 응답을 주기 위해."""
    return {"kind": "error", "code": code, "message": message, "http_status": http_status, **extra}


async def _open(
    conversation_id: str, user: User, purpose: str,
) -> tuple[server_api.ConversationContext, Conversation]:
    """server 에 이 방을 한 번 묻고(권한 판단 포함), 메모리 세션을 찾거나 그 답으로 복원한다.

    복원이 되살릴 수 없는 활성 run 을 닫았으면 돌려받는 컨텍스트에 이미 반영돼 있다.
    """
    context = await server_api.conversation_context(conversation_id, user, purpose=purpose)
    conversation, context = await registry.get_or_restore(context)
    conversation.session.progress_key = conversation_id   # 이 방의 run 이 진행 상황을 올릴 자리
    return context, conversation


async def _connection(room: server_api.ConversationInfo, user: User) -> server_api.ClusterConnection:
    """이 방의 클러스터를 지금 쓸 수 있나 + 접속 정보. 읽기(_open)와 달리 **실행 직전**에 본다.

    팀에서 나간 사람이 자기 private 방은 계속 읽되 그 팀 클러스터로 kubectl 을 돌리지는 못하게 한다 (server 가 404).
    방이 만들 때 복사한 지문을 함께 보내 "승인한 대상 = 실행 대상" 을 server 가 대조한다 (409 CLUSTER_CHANGED).
    """
    return await access.connection(room.cluster_id, user, expected_fingerprint=room.cluster_fingerprint)


def _mutating_mode(name: str) -> bool:
    skill = SKILLS.get(name)
    return skill is not None and bool(skill.allowed_tools & MUTATING_TOOLS)


def _private_change_blocked() -> HTTPException:
    return _error(403, "PRIVATE_SESSION",
                  "Private 대화에서는 클러스터를 변경할 수 없다 — 변경은 Shared 대화를 새로 만들어서 (기획 05)")


def _messages_json(result: Any) -> list[Any]:
    """이 run 에서 새로 생긴 ModelMessage 들만. 복원할 때 run 순서대로 이어 붙인다."""
    return ModelMessagesTypeAdapter.dump_python(result.new_messages(), mode="json")


def _usage_json(result: Any) -> dict[str, Any] | None:
    """문서 4절 usage_summary. 결과가 사용량을 모르면 null."""
    usage = getattr(result, "usage", None)
    if not callable(usage):
        return None
    try:
        u = usage()
    except Exception:            # 가짜 결과·오래된 버전 — 사용량은 부가 정보라 실패해도 run 은 살린다
        return None
    return {
        "requests": getattr(u, "requests", None),
        "input_tokens": getattr(u, "input_tokens", None),
        "output_tokens": getattr(u, "output_tokens", None),
    }


@contextmanager
def _with_cluster(session: Any, conn: server_api.ClusterConnection) -> Iterator[None]:
    """실행 동안만 임시 kubeconfig 를 연다 (기획 04 §8). 블록을 벗어나면 파일이 지워진다."""
    with access.kubeconfig(conn) as path:
        session.deps = dataclasses.replace(session.deps, kubeconfig=path)
        try:
            yield
        finally:
            # 파일은 곧 사라진다 — 경로를 세션에 남기면 다음 요청이 없는 파일을 가리킨다
            session.deps = dataclasses.replace(session.deps, kubeconfig=None)


def _bind_run(session: Any, run: server_api.Run, user: User) -> None:
    """이번 요청이 어느 run 의 것이고 누가 보냈는지 세션에 싣는다 — 가드레일 훅이 Action Plan 을
    그 run 에 만들고 decision 에 결정자를 적을 때 쓴다 (#58). 매 요청 새로 덮어쓴다."""
    session.deps = dataclasses.replace(session.deps, run_id=run.id, user_id=user.id)


def _busy() -> HTTPException:
    return _error(409, "BUSY", "이전 요청 처리 중 — 끝난 뒤 다시 보내라")


def _pending_error(status: str | None) -> HTTPException:
    """활성 run 의 상태로 앱이 아는 409 코드를 고른다 (활성 run 하나 규칙)."""
    if status == "awaiting_approval":
        return _error(409, "PENDING_APPROVAL", "승인 대기 중 — /approve 로 먼저 결정")
    if status == "recovery_required":
        return _error(409, "RECOVERY_REQUIRED", "재개가 실패한 요청이 있다 — /resume 으로 다시 시도")
    return _busy()


async def _active_run_conflict(conversation_id: str, user: User) -> HTTPException:
    """run 시작이 409 ACTIVE_RUN_EXISTS 로 막혔다 — server 는 코드만 주므로 지금 활성 run 을 다시 읽어 고른다."""
    try:
        fresh = await server_api.conversation_context(conversation_id, user, purpose="chat")
    except Exception:
        logger.exception("활성 run 재조회 실패 (conversation=%s)", conversation_id)
        return _busy()
    return _pending_error(fresh.active_run.status if fresh.active_run else None)


async def _interrupt(conversation_id: str, message: str = STALE_MESSAGE) -> None:
    """활성으로 남은 run 을 닫는다. 그 run 의 열린 계획도 server 가 같은 트랜잭션에서 닫는다."""
    await server_api.interrupt(conversation_id, message)


# ── 엔드포인트 ─────────────────────────────────────────────

@router.post("/{conversation_id}/chat")
async def chat(
    conversation_id: str,
    body: ChatIn,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    context, conversation = await _open(conversation_id, user, "chat")
    room = context.conversation
    session = conversation.session
    is_mode = body.text.startswith("/mode ")
    kind = "mode_change" if is_mode else "chat"
    if is_mode and not room.shared and _mutating_mode(body.text.removeprefix("/mode ").strip()):
        raise _private_change_blocked()          # 변경 도구가 있는 모드는 shared 방에서만 (문서 05 §3)

    # 재전송이면 실행하지 않고 저장된 결과를 그대로 — 승인 대기 검사보다 먼저 (응답 유실 뒤 재시도 규약)
    stored = await server_api.find_run(conversation_id, body.request_id, user)
    if stored is not None:
        if stored.input_text != body.text or stored.kind != kind:
            raise _error(409, "REQUEST_MISMATCH", f"같은 request_id 로 다른 입력을 보냈다: {body.request_id}")
        if stored.status == "running":
            # 실행 중이라는데 이 방의 잠금이 비어 있다 = 결과 저장에 실패한 run 이다. 영원히 BUSY 를 재생하지 않도록 닫는다.
            # 판단은 방금 읽은 상태가 아니라 **지금 메모리**로 한다 (자동 리뷰). 조회를 기다리는 사이 원래 요청이 승인 카드를
            # 남기고 끝났을 수 있다 — 그 run(이제 awaiting_approval)을 닫으면 카드는 만료되는데 메모리 티켓은 남아,
            # 재시작 전까지 approve 는 NOT_PENDING, chat·resume 은 PENDING_APPROVAL 로 방이 갇힌다.
            if conversation.lock.locked():
                return _replay(stored)                # 원래 요청이 아직 돈다 — 저장된 그대로 BUSY
            if session.pending is None:
                # 닫는 동안 잠금을 쥔다. interrupt 는 "이 방의 활성 run" 을 닫으므로, 그 사이 새 chat 이 연 run 을
                # 늦게 도착한 interrupt 가 대신 닫으면 안 된다 (복원의 interrupt 와 같은 규칙, conversations.py)
                async with conversation.lock:
                    await _interrupt(conversation_id)
            stored = await server_api.find_run(conversation_id, body.request_id, user) or stored
        return _replay(stored)

    # 클러스터 권한·접속 정보는 **잠금 검사보다 먼저** 받는다. 이유가 둘이다 (자동 리뷰).
    #   - run 을 만들기 전이어야 한다. 뒤에서 던지면 그 run 이 running 인 채 남고, 같은 request_id 로
    #     재시도하면 403 대신 409 INTERRUPTED 라는 엉뚱한 안내가 나간다
    #   - `lock.locked()` 검사와 `async with lock` 사이에 await 가 있으면 안 된다. 그 창에서 루프를
    #     놓으면 두 요청이 나란히 통과해 같은 방에 run 이 둘 생긴다 (체크리스트 "잠금 틈")
    conn = await _connection(room, user)
    if conversation.lock.locked():
        raise _busy()
    if session.pending is not None:
        raise _pending_error("awaiting_approval" if session.pending_ids else "recovery_required")

    async with conversation.lock:
        # 잠금을 쥔 지금 이 방에 실행 중인 run 은 없고, 여기까지 왔으면 메모리에 이어갈 티켓(session.pending)도 없다.
        # 그런데 server 에 활성 run(running / awaiting_approval / recovery_required)이 남아 있다면 결과 저장에 실패한 run 이다
        # (팀원 리뷰 3, 자동 리뷰 6차) — 그대로 두면 이 방은 영원히 BUSY 라, 중단으로 닫고 진행한다. 저장 실패 사정(error)은 남는다.
        stale = context.active_run
        if stale is not None:
            logger.warning("결과가 저장되지 않은 run 을 닫는다 (conversation=%s, run=%s, status=%s)",
                           conversation_id, stale.id, stale.status)
            await _interrupt(conversation_id)
        try:
            run, created = await server_api.start_run(
                conversation_id, user, request_id=body.request_id, kind=kind,
                mode=session.skill.name, input_text=body.text,
            )
        except server_api.ServerError as exc:
            if exc.code == "ACTIVE_RUN_EXISTS":
                raise await _active_run_conflict(conversation_id, user) from None
            raise                                 # REQUEST_MISMATCH 등은 server 의 코드 그대로
        if not created:
            return _replay(run)

        _bind_run(session, run, user)
        before = list(session.history)   # 저장에 실패해 이 턴을 버릴 때 되돌릴 자리 (자동 리뷰 지적)
        try:
            with _with_cluster(session, conn):
                payload, result = await _server._chat_turn(session, body.text)
        except HTTPException as exc:
            wrapped = _wrap(exc)
            await _fail_run(run, wrapped)
            raise wrapped
        except Exception as exc:                 # 모델·툴 예외 — run 은 실패로 남기고 세션은 유지
            logger.exception("run 실패 (conversation=%s, run=%s)", conversation_id, run.id)
            failed = _error(500, "RUN_FAILED", f"요청 처리에 실패했다 ({type(exc).__name__}) — 서버 로그 참고")
            await _fail_run(run, failed)
            raise failed from exc

        # 승인 카드면 run 은 열린 채(awaiting_approval) 남는다 — approve/resume 이 이어서 끝낸다 (문서 7절)
        status = "awaiting_approval" if payload.get("kind") == "approval" else "completed"
        # 모드 갱신과 첫 마디로 제목 짓기(#59)를 run 기록과 한 번에 보낸다 — server 가 한 트랜잭션에서 한다.
        # 제목이 될 수 있는 마디인지, 첫 마디인지는 server 가 본다 (판정이 두 곳에 갈라지면 한쪽만 고쳐진다).
        await _save_or_fail(
            conversation_id, run, on_failure=_chat_store_failure(), failure_status="failed",
            session=session, history_before=before, status=status, response_payload=payload,
            append_agent_messages=_messages_json(result) if result is not None else None,
            usage_summary=_usage_json(result) if result is not None else None,
            current_mode=session.skill.name,
            title_from=body.text if kind == "chat" else None,
        )
        return payload


async def _fail_run(run: server_api.Run, exc: HTTPException) -> None:
    """실행이 실패한 run 을 failed 로 남긴다. 이것마저 못 남기면 로그만 — 원래 오류를 덮지 않는다.
    running 으로 남은 run 은 다음 chat(잠금 안의 정리)이나 같은 request_id 재전송이 닫는다."""
    try:
        await server_api.update_run(run.id, status="failed", response_payload=_error_record(exc))
    except Exception:
        logger.exception("실패 기록도 저장하지 못했다 (run=%s)", run.id)


async def _save_or_fail(
    conversation_id: str, run: server_api.Run, *, on_failure: HTTPException, failure_status: str,
    session: Any = None, history_before: list[Any] | None = None, keep_payload: bool = False,
    **fields: Any,
) -> None:
    """실행 결과 저장. 실패하면 run 을 failure_status 로라도 남기고 on_failure 를 던진다 — 그것도 안 되면 다음 chat 이
    잠금을 쥔 채 활성 run 을 닫는다. 호출자가 상태·문구를 고른다: 모델 답변만 잃은 chat 은 failed, kubectl 이 이미 돈
    승인·재개는 recovery_required ("변경은 적용됐을 수 있다", 문서 4절). keep_payload 면 대체 쓰기가 payload 를
    건드리지 않는다 — "여기서 계속하라" 는 경로는 계속할 재료(남은 카드)를 기록에 남겨야 한다.

    **종료 상태로 닫으면 메모리의 승인 티켓도, 그 턴의 기록도 함께 버린다** (팀원 리뷰 + 자동 리뷰).
    server 의 run 은 닫혔는데 session.pending 이 남으면 그 방은 이 프로세스가 사는 동안 아무것도 못
    한다 — /chat 은 PENDING_APPROVAL, /approve 는 활성 run 이 없어 NOT_PENDING, /resume 은 다시
    PENDING_APPROVAL 이고, 다음 chat 의 복구 로직은 pending 검사에 먼저 막혀 닿지 못한다.

    history 도 같이 되돌린다. _to_payload 는 카드 분기에서도 history 를 먼저 갱신하므로, 티켓만
    비우면 **결과 없는 tool call 로 끝난 기록**이 메모리에 남는다. 그걸 실은 채 다음 chat 을 돌리면
    모델이 대화를 거부해 500 이 나고, 그때는 _to_payload 가 안 돌아 history 가 그대로라 무한
    반복이다. 재시작 복원이 지키는 규칙(conversations.py 머리말)과 같은 규칙이다.

    활성 상태로 남기는 경로(awaiting_approval·recovery_required)는 티켓·기록이 있어야 이어갈 수
    있으므로 그대로 둔다. 종료 상태로 닫힌 run 의 계획은 server 가 같은 트랜잭션에서 닫는다.
    """
    try:
        await server_api.update_run(run.id, **fields)
    except Exception as exc:
        logger.exception("실행 결과 저장 실패 (conversation=%s, run=%s)", conversation_id, run.id)
        try:
            if keep_payload:
                await server_api.update_run(run.id, status=failure_status)
            else:
                await server_api.update_run(run.id, status=failure_status, response_payload=_error_record(on_failure))
        except Exception:
            logger.exception("실패 표시도 저장하지 못했다 (run=%s)", run.id)
        # failed 는 종료 상태다 — 메모리의 승인 티켓과 그 턴의 기록도 함께 버린다 (팀원 리뷰 + 자동 리뷰).
        # "server 가 닫았으면 나머지도 닫는다" 하나다. recovery_required·awaiting_approval 은 활성이라 나중에 interrupt 가 지나간다.
        if failure_status not in server_api.RUN_ACTIVE and session is not None:
            session.pending = None
            session.decisions.clear()
            if history_before is not None:
                session.history = history_before
        raise on_failure from exc


def _approved_plan_ids(session: Any, also_approved: str | None = None) -> list[str]:
    """티켓 중 **승인한** 카드의 Plan id 만 — "적용됐을 수 있는 변경" 목록에 거절한 카드가 섞이면 안 된다.
    also_approved 는 지금 처리 중인 결정(아직 session.decisions 에 없다)."""
    if session.pending is None:
        return []
    approved = {cid for cid, d in session.decisions.items() if isinstance(d, ToolApproved)}
    if also_approved:
        approved.add(also_approved)
    return [
        str(plan_id)
        for call in session.pending.approvals
        if call.tool_call_id in approved
        and (plan_id := session.pending.metadata.get(call.tool_call_id, {}).get("plan_id"))
    ]


def _chat_store_failure() -> HTTPException:
    return _error(503, "STORE_FAILED",
                  "실행은 끝났지만 결과를 저장하지 못했다 — 같은 내용을 새 요청(request_id)으로 보내라")


def _decision_store_failure() -> HTTPException:
    """남은 카드 재전송(아무것도 실행하지 않음)에서 저장 실패: 결정은 메모리에 남았고 티켓은 살아 있다."""
    return _error(503, "STORE_FAILED", "결정은 받았지만 기록을 저장하지 못했다 — 남은 카드를 계속 결정하라")


def _resume_store_failure(plan_ids: list[str]) -> HTTPException:
    """승인·재개 뒤 저장 실패: kubectl 은 이미 돌았다. 같은 지시를 다시 넣게 하면 안 된다 — Plan 을 확인하게 한다.
    plan_ids 는 재개 직전 티켓 전체에서 뽑은 것 (카드 여러 장·재시도 경로 모두 빠짐없이)."""
    return HTTPException(503, {
        "code": "STORE_FAILED",
        "message": "승인 결과는 처리됐지만 기록을 저장하지 못했다. 변경은 이미 적용됐을 수 있다 — 같은 지시를 다시 보내지 말고 Action Plan 을 확인하라",
        "plan_ids": plan_ids,
    })


def _replay(run: server_api.Run) -> dict[str, Any]:
    if run.status == "running":
        raise _error(409, "BUSY", "같은 요청을 아직 처리 중이다")
    error = run.error
    if error is not None:
        raise HTTPException(int(error.get("http_status", 500)),
                            {"code": error.get("code", "RUN_FAILED"), "message": error.get("message", "")})
    assert run.payload is not None
    return run.payload


@router.get("/{conversation_id}/progress")
async def progress_stream(
    conversation_id: str,
    user: User = Depends(current_user),
) -> StreamingResponse:
    """지금 도는 run 의 단계(도구 호출·실행한 명령)를 SSE 로. 화면이 답을 기다리는 동안 연다.

    대화를 읽을 수 있는 사람만 (purpose=chat — 기록과 같은 권한). 답(`POST /chat` 등)과 별개라, 이 연결이 끊겨도
    실행에는 영향이 없다. run 이 끝나면 `{"type": "done", "run": true}` 를 보내고 닫는다 — 연결했는데 run 이 시작되지 않으면
    `run: false` (progress.py).
    """
    # 권한만 본다. _open 은 방을 메모리에 되살리며 되살릴 수 없는 활성 run 을 닫기도 한다 — 보기만 하는 GET 이
    # 그런 일을 하면 안 된다 (리뷰). 방이 메모리에 없으면 도는 run 도 없으니 기다리다 done 으로 닫힌다
    await server_api.conversation_context(conversation_id, user, purpose="chat")
    return StreamingResponse(
        progress.hub.stream(conversation_id),
        media_type="text/event-stream",
        # 프록시가 모았다가 한 번에 보내지 않게 (nginx 계열은 X-Accel-Buffering 을 본다)
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{conversation_id}/approve")
async def approve(
    conversation_id: str,
    body: ApproveIn,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    context, conversation = await _open(conversation_id, user, "approve")   # 승인 권한은 server 가 본다
    session = conversation.session
    conn = await _connection(context.conversation, user)   # 잠금 검사보다 먼저 — 그 사이에 await 가 있으면 안 된다
    if conversation.lock.locked():
        raise _busy()
    run = _open_run(context)
    async with conversation.lock:
        _bind_run(session, run, user)
        # 재개가 돌면 _to_payload 가 티켓을 지우므로, 저장 실패 안내에 실을 Plan id(승인한 카드만)는 여기서 미리 뽑는다
        plan_ids = _approved_plan_ids(session, body.call_id if body.approved else None)
        # 결정 검사·기록은 server._approve 가 한다 (문자열 detail). 여기서는 코드 객체로 감싼다.
        try:
            with _with_cluster(session, conn):
                outcome, result = await _server._approve(session, body.call_id, body.approved)
        except HTTPException as exc:
            raise await _record_failure(run, _wrap(exc))
        return await _continue_run(conversation, run, outcome, result, plan_ids)


@router.post("/{conversation_id}/resume")
async def resume(
    conversation_id: str,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    context, conversation = await _open(conversation_id, user, "approve")   # 재개도 승인과 같은 권한
    session = conversation.session
    conn = await _connection(context.conversation, user)   # 잠금 검사보다 먼저 — 그 사이에 await 가 있으면 안 된다
    if conversation.lock.locked():
        raise _busy()
    if session.pending is None:
        raise _error(409, "NOT_PENDING", "재개할 승인 건이 없다")
    if session.pending_ids:
        raise _error(409, "PENDING_APPROVAL", f"아직 결정하지 않은 승인 건이 있다: {session.pending_ids}")
    run = _open_run(context)
    async with conversation.lock:
        _bind_run(session, run, user)
        plan_ids = _approved_plan_ids(session)
        try:
            with _with_cluster(session, conn):
                outcome, result = await _server._resume(session)
        except HTTPException as exc:
            raise await _record_failure(run, _wrap(exc))
        return await _continue_run(conversation, run, outcome, result, plan_ids)


def _open_run(context: server_api.ConversationContext) -> server_api.Run:
    """승인·재개가 이어갈 run — 이 방의 활성 run(awaiting_approval / recovery_required). 없으면 결정할 게 없다."""
    run = context.active_run
    if run is None or run.status == "running":
        raise _error(409, "NOT_PENDING", "대기 중인 승인 건이 없다")
    return run


async def _continue_run(
    conversation: Conversation, run: server_api.Run, outcome: dict[str, Any], result: Any,
    plan_ids: list[str],
) -> dict[str, Any]:
    """승인/재개 결과를 같은 run 에 남긴다 (문서 7절 "승인만으로 새 run 을 만들지 않는다").

    result 가 있으면 실제로 재개가 돌았다 — 그 메시지(tool 결과, 다음 tool call)를 이 run 에 이어 붙인다. 재개 뒤 새 카드가
    나와도 마찬가지다 (팀원 리뷰 2: 여기서 안 붙이면 복원 history 에 첫 작업의 결과와 둘째 작업의 호출이 빠진다).
    result 가 없으면 남은 카드 재전송이라 payload 만 바꾼다. 답이 나왔으면 completed, 카드가 나왔으면 awaiting_approval 그대로.
    """
    fields: dict[str, Any] = {
        "response_payload": outcome,
        "append_agent_messages": (_messages_json(result) or None) if result is not None else None,
        "usage_summary": _usage_json(result) if result is not None else None,
    }
    # 저장 실패의 뜻은 "실제로 재개가 돌았나"(result 유무)로 정한다: 안 돌았으면 카드 대기 그대로, 돌았으면 확인 필요 (문서 4절)
    failure = (
        dict(on_failure=_decision_store_failure(), failure_status="awaiting_approval", keep_payload=True)
        if result is None
        else dict(on_failure=_resume_store_failure(plan_ids), failure_status="recovery_required")
    )
    if outcome.get("kind") == "approval":
        await _save_or_fail(conversation.id, run, **failure, status="awaiting_approval", **fields)
        return outcome
    # completed 는 종료 상태다 — 남은 계획이 있으면 server 가 같이 닫는다
    await _save_or_fail(conversation.id, run, **failure, status="completed",
                        current_mode=conversation.session.skill.name, **fields)
    return outcome


async def _record_failure(run: server_api.Run, exc: HTTPException) -> HTTPException:
    """승인/재개가 **재개를 시작한 뒤** 실패하면(503 RESUME_RETRYABLE) run 을 recovery_required 로 — 문서 4절
    "실제 변경 결과가 불명확해 확인 필요". 티켓은 살아 있어 /resume 으로 이어진다. 기록마저 실패하면 로그만 남기고
    원래 오류를 돌려준다.

    kubectl 은 재개 안에서만 돈다 (server._resume 은 시작한 뒤의 모든 실패를 RESUME_RETRYABLE 로 감싼다).
    그 밖의 실패는 실행 전이라 run 을 건드리지 않는다 — 4xx(모르는 call_id, 이미 결정)뿐 아니라 카드 조회·거절 기록이
    server 장애로 난 5xx 도 (자동 리뷰). 여기서 recovery_required 로 덮으면 결정도 안 한 카드의 payload 가 사라지고,
    재시작 뒤 복원이 awaiting_approval 이 아닌 run 을 닫아 그 카드가 EXPIRED 가 된다."""
    if exc.status_code >= 500 and exc.detail.get("code") == "RESUME_RETRYABLE":
        try:
            await server_api.update_run(run.id, status="recovery_required", response_payload=_error_record(exc))
        except Exception:
            logger.exception("재개 실패를 기록하지 못했다 (run=%s)", run.id)
    return exc


def _error_record(exc: HTTPException) -> dict[str, Any]:
    detail = dict(exc.detail)
    return error_payload(detail.pop("code", "ERROR"), detail.pop("message", ""), exc.status_code, **detail)


# ── server.py 의 문자열 detail 을 {code, message} 로 ─────────

_CODES: list[tuple[str, str]] = [
    ("세션이 없다", "NO_SESSION"),
    ("이전 요청 처리 중", "BUSY"),
    ("요청 처리 중", "BUSY"),
    ("승인 대기 중", "PENDING_APPROVAL"),
    ("이미 결정한", "ALREADY_DECIDED"),
    ("대기 중인 승인 건이 아니다", "NOT_PENDING"),
    ("재개할 승인 건이 없다", "NOT_PENDING"),
    ("아직 결정하지 않은", "PENDING_APPROVAL"),
    ("거절을 기록하지 못했다", "REJECT_FAILED"),
    ("kubeconfig", "KUBECONFIG"),
    ("pending approval mismatch", "APPROVAL_MISMATCH"),
]


def _wrap(exc: HTTPException) -> HTTPException:
    """server.py 가 던진 문자열 detail 을 코드 객체로. 이미 객체면(RESUME_RETRYABLE, server 오류) 그대로."""
    if isinstance(exc.detail, dict):
        return exc
    text = str(exc.detail)
    code = next((c for prefix, c in _CODES if text.startswith(prefix) or prefix in text), "ERROR")
    return HTTPException(exc.status_code, {"code": code, "message": text})


_server.app.include_router(router)   # server.py 맨 아래의 plain import 가 이 줄을 실행시킨다

__all__ = ["router"]
