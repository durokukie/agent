"""대화(채팅방) 단위 실행 엔드포인트 — kukie-electron docs/api-spec.md 1절.

  POST /conversations/{id}/chat          한 턴. request_id 로 멱등. 오래 걸리면 202 accepted 로 먼저 답한다 (DURO-258)
  POST /conversations/{id}/approve       승인 카드 결정. PR 머지 카드는 오래 걸리면 202 accepted 로 먼저 답한다 (_finish_locked)
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
private 방의 변경은 툴 목록이 막는다 — private 방 스킬에는 변경 툴이 없다 (DURO-100, skills/__init__.py).

오류는 전부 FastAPI 의 detail 자리에 {code, message} 객체로 간다 — 응답은 {"detail": {code, message}}.
앱의 http.ts 가 detail 이 객체면 code 를 꺼낸다 (api-spec 공통 절). server 의 오류 코드도 같은 상태로 그대로 나간다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import uuid
from contextlib import contextmanager, nullcontext
from typing import Any, Awaitable, Callable, Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.tools import ToolApproved

import kukie_agent.server as _server  # 순환 import: 이름은 호출 시점에만 쓴다
from kukie_agent import progress, server_api, title
from kukie_agent.auth import User, current_user
from kukie_agent.clusters import access
from kukie_agent.conversations import Conversation, registry
from kukie_agent.gitops import memo as repo_link_memo
from kukie_agent.guardrail import gitops_after_merge
from kukie_agent.tools.gitops import TOOL_NAME as GITOPS_TOOL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

#: 결과를 저장하지 못하고 활성으로 남은 run 을 닫을 때 남기는 안내
STALE_MESSAGE = "결과를 저장하지 못해 중단된 요청이다 — 새 요청으로 보내라"
#: PR 머지 승인이 답을 기다리는 시간(초). 앞단(CloudFront origin_read_timeout 60초)이 끊기 전에 답한다 (DURO-158).
#: 대개는 머지와 모델의 답까지 이 안에 끝난다 — 넘으면 일은 뒤에서 마저 하고 202 로 먼저 답한다
APPROVE_WAIT = float(os.environ.get("KUKIE_GITOPS_APPROVE_WAIT") or 45)
#: 채팅 한 턴이 답을 기다리는 시간(초). 같은 이유로 앞단 60초 전에 답한다 (DURO-258). 넘으면 run 은 뒤에서 끝까지 돌고
#: 202 accepted 로 먼저 답한다 — 앱은 대화 기록을 다시 읽어 답을 받는다
CHAT_WAIT = float(os.environ.get("KUKIE_CHAT_WAIT") or 45)
#: 실패 표시(failed)를 Kukie API 서버에 못 남겼을 때 뒤에서 다시 남기는 간격(초). server 는 배포 때(Recreate, 1개)
#: 1~2분 비어 약 5분을 기다린다. 그래도 못 남기면 running 이 남고 다음 chat·재전송·재시작 복원이 닫는다
FAIL_RETRY_SECONDS: tuple[float, ...] = (2, 5, 10, 20, 30, 60, 60, 60, 60)
_fail_retries: set[asyncio.Task[None]] = set()


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
        session.deps = dataclasses.replace(session.deps, kubeconfig=path, connection=conn)
        try:
            yield
        finally:
            # 파일은 곧 사라진다 — 경로를 세션에 남기면 다음 요청이 없는 파일을 가리킨다
            session.deps = dataclasses.replace(session.deps, kubeconfig=None, connection=None)


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
) -> Any:
    """한 턴. CHAT_WAIT 안에 끝나면 답(answer·approval)을, 넘으면 202 accepted 를 먼저 준다 (DURO-258).

    답이 앞단 시간 제한(CloudFront 60초)을 넘으면 화면에는 504 만 남고 에이전트는 뒤에서 일을 끝낸다 — 그래서 run 을 요청과 떼어
    돌린다. 넘어도 run·기록 저장은 그대로이고 잠금도 끝날 때까지 쥔다(그동안 새 chat 은 BUSY). 앱은 진행 상황(/progress)을
    계속 보이고 대화 기록을 다시 읽어 답을 받는다.
    """
    context, conversation = await _open(conversation_id, user, "chat")
    room = context.conversation
    session = conversation.session
    # "/mode " 는 옛 앱이 보내는 입력이다 — 아무것도 바꾸지 않지만 run 종류는 예전대로 남긴다 (server._chat_turn)
    kind = "mode_change" if body.text.startswith("/mode ") else "chat"

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

    # 잠금은 locked() 검사 바로 뒤에 쥔다 (잠금 틈). 일을 요청과 떼어 돌리므로 async with 대신 쥐고, 일이 끝날 때 푼다 (_detach)
    await conversation.lock.acquire()

    async def work() -> dict[str, Any]:
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
        # 제목은 답과 동시에 모델로 짓는다 — 답이 끝날 즈음엔 대개 와 있어 기다림이 늘지 않는다
        naming = (asyncio.create_task(title.suggest(body.text))
                  if kind == "chat" and title.wanted(room.title, body.text) else None)
        try:
            try:
                with _with_cluster(session, conn):
                    payload, result = await _server._chat_turn(session, body.text)
            except HTTPException as exc:
                await repo_link_memo.settle(session.deps, saved=False)
                wrapped = _wrap(exc)
                await _fail_run(run, wrapped)
                raise wrapped
            except Exception as exc:                 # 모델·툴 예외 — run 은 실패로 남기고 세션은 유지
                await repo_link_memo.settle(session.deps, saved=False)
                logger.exception("run 실패 (conversation=%s, run=%s)", conversation_id, run.id)
                failed = _error(500, "RUN_FAILED", f"요청 처리에 실패했다 ({type(exc).__name__}) — 서버 로그 참고")
                await _fail_run(run, failed)
                raise failed from exc

            # 승인 카드면 run 은 열린 채(awaiting_approval) 남는다 — approve/resume 이 이어서 끝낸다 (문서 7절)
            status = "awaiting_approval" if payload.get("kind") == "approval" else "completed"
            # 첫 마디로 제목 짓기(#59)를 run 기록과 한 번에 보낸다 — server 가 한 트랜잭션에서 한다.
            # 제목이 될 수 있는 마디인지, 첫 마디인지는 server 가 본다 (판정이 두 곳에 갈라지면 한쪽만 고쳐진다).
            # 모델이 지은 제목(title)은 함께 보내는 후보일 뿐이다 — 못 쓰면 server 가 첫 마디로 짓는다.
            await _save_or_fail(
                conversation_id, run, on_failure=_chat_store_failure(), failure_status="failed",
                session=session, history_before=before, status=status, response_payload=payload,
                append_agent_messages=_messages_json(result) if result is not None else None,
                usage_summary=_usage_json(result) if result is not None else None,
                title_from=body.text if kind == "chat" else None,
                title=await naming if naming is not None else None,
            )
            return payload
        finally:
            if naming is not None and not naming.done():
                naming.cancel()                      # 답이 실패했거나 요청이 끊겼다 — 제목은 버린다

    accepted = {
        "kind": "accepted", "skill": session.skill.name, "plan_ids": [], "request_id": body.request_id,
        "message": "요청은 받았고 처리는 계속돼요. 답이 오면 이 대화에 나타나요.",
    }
    return await _detach(conversation, work, CHAT_WAIT, accepted, what="채팅")


async def _fail_run(run: server_api.Run, exc: HTTPException) -> None:
    """실행이 실패한 run 을 failed 로 남긴다. 이것마저 못 남기면 로그만 — 원래 오류를 덮지 않는다.
    running 으로 남은 run 은 다음 chat(잠금 안의 정리)이나 같은 request_id 재전송이 닫는다."""
    fields = dict(status="failed", response_payload=_error_record(exc))
    try:
        await server_api.update_run(run.id, **fields)
    except Exception as failure:
        logger.exception("실패 기록도 저장하지 못했다 (run=%s)", run.id)
        _retry_failure_later(run, fields, failure)


def _retry_failure_later(run: server_api.Run, fields: dict[str, Any], exc: Exception) -> None:
    """실패 표시를 Kukie API 서버가 돌아올 때까지 뒤에서 다시 남긴다.

    앱은 기록에 running 턴이 있으면 입력을 막는다. 그래서 server 배포로 잠깐 닿지 못해 running 이 남으면, 다음 chat 이
    남은 run 을 닫는 길에 사용자가 닿지 못하고 방이 "처리 중" 에 갇힌다 (2026-11-13 dev member 재배포).

    종료 상태만 다시 쓴다. server 는 상태 전이를 막지 않아, 그 사이 다른 길(다음 chat·재전송·재시작 복원)이 닫은
    run 을 활성(awaiting_approval·recovery_required)으로 되살리면 안 된다. 종료 → 종료(interrupted → failed)는 방을 잠그지 않는다.
    """
    if not FAIL_RETRY_SECONDS or fields.get("status") in server_api.RUN_ACTIVE or not _unreachable(exc):
        return
    task = asyncio.create_task(_save_failure_later(run, fields))
    _fail_retries.add(task)
    task.add_done_callback(_fail_retries.discard)


def _unreachable(exc: Exception) -> bool:
    """server 에 닿지 못했거나 server 가 5xx 로 답했다 — 기다리면 될 수 있다. 4xx 는 다시 해도 같다.
    내부 토큰 거절도 503 SERVER_UNAVAILABLE 로 오지만 설정 오류라 기다려도 바뀌지 않는다 (PR #153 리뷰)."""
    return (isinstance(exc, server_api.ServerError)
            and (exc.code == "SERVER_UNAVAILABLE" or exc.status_code >= 500)
            and exc.message != server_api.INTERNAL_AUTH_MISMATCH)


async def _save_failure_later(run: server_api.Run, fields: dict[str, Any]) -> None:
    """쓰기 전에 지금 상태를 다시 읽어 아직 running 일 때만 남긴다 (PR #153 리뷰). 닫힌 run 을 덮어쓰면 두 가지가 깨진다.
      - 결과 저장(completed)이 server 에 들어간 뒤 응답만 끊긴 경우(ReadTimeout 도 SERVER_UNAVAILABLE) — 정상 결과가 failed 가 된다
      - 같은 request_id 재전송이 interrupted 로 닫은 run — 같은 재전송에 다른 응답이 나간다
    읽기는 빈 PATCH 다 — server 는 준 칸만 바꾸고 run 을 돌려준다. 읽기와 쓰기는 이 방의 잠금 안에서 한다. 이 run 을 닫는
    다른 길(다음 chat·재전송)도 같은 잠금을 쥐고, replica 가 하나라 그 사이에 끼어들 수 없다. 방이 메모리에 없으면 잠글 것도
    없다 — 닫는 길이 먼저 방을 복원해야 하고, 재시작이면 이 작업도 함께 사라진다.
    """
    run_id = run.id
    for delay in FAIL_RETRY_SECONDS:
        await asyncio.sleep(delay)
        live = registry.get(run.conversation_id)
        try:
            async with live.lock if live is not None else nullcontext():
                current = (await server_api.update_run(run_id)).run
                if current.status != "running":
                    logger.warning("뒤늦은 실패 표시를 그만둔다 — run 이 이미 %s 다 (run=%s)", current.status, run_id)
                    return
                await server_api.update_run(run_id, **fields)
        except Exception as exc:
            if _unreachable(exc):
                continue
            logger.exception("실패 표시를 뒤늦게 남기다 그만뒀다 (run=%s)", run_id)
            return
        logger.warning("server 가 돌아와 실패 표시를 뒤늦게 남겼다 (run=%s)", run_id)
        return
    logger.error("실패 표시를 끝내 남기지 못했다 (run=%s) — 다음 chat·재전송·재시작 복원이 닫는다", run_id)


async def stop_failure_retries() -> None:
    """끌 때 기다리던 다시 남기기를 멈춘다. 남은 running 은 다음 기동의 interrupt-orphaned 가 닫는다."""
    for task in list(_fail_retries):
        task.cancel()
    await asyncio.gather(*_fail_retries, return_exceptions=True)


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
        if session is not None:
            await repo_link_memo.settle(session.deps, saved=False)   # 메모가 든 기록이 저장되지 않았다 — 칸도 그대로
        failure = (dict(status=failure_status) if keep_payload
                   else dict(status=failure_status, response_payload=_error_record(on_failure)))
        try:
            await server_api.update_run(run.id, **failure)
        except Exception as unsaved:
            logger.exception("실패 표시도 저장하지 못했다 (run=%s)", run.id)
            _retry_failure_later(run, failure, unsaved)
        # failed 는 종료 상태다 — 메모리의 승인 티켓과 그 턴의 기록도 함께 버린다 (팀원 리뷰 + 자동 리뷰).
        # "server 가 닫았으면 나머지도 닫는다" 하나다. recovery_required·awaiting_approval 은 활성이라 나중에 interrupt 가 지나간다.
        if failure_status not in server_api.RUN_ACTIVE and session is not None:
            session.pending = None
            session.decisions.clear()
            if history_before is not None:
                session.history = history_before
        raise on_failure from exc
    if session is not None:
        # 메모가 든 이 run 의 기록이 server 에 남았다 — 이제 숨은 칸을 지금 상태로 바꾼다 (DURO-135, gitops/memo.py)
        await repo_link_memo.settle(session.deps, saved=True)


def _approved_call_ids(session: Any) -> set[str]:
    return {call_id for call_id, decision in session.decisions.items() if isinstance(decision, ToolApproved)}


def _approved_plan_ids(session: Any, also_approved: str | None = None) -> list[str]:
    """티켓 중 **승인한** 카드의 Plan id 만 — "적용됐을 수 있는 변경" 목록에 거절한 카드가 섞이면 안 된다.
    also_approved 는 지금 처리 중인 결정(아직 session.decisions 에 없다)."""
    if session.pending is None:
        return []
    approved = _approved_call_ids(session)
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
) -> Any:
    context, conversation = await _open(conversation_id, user, "approve")   # 승인 권한은 server 가 본다
    session = conversation.session
    conn = await _connection(context.conversation, user)   # 잠금 검사보다 먼저 — 그 사이에 await 가 있으면 안 된다
    if conversation.lock.locked():
        raise _busy()
    run = _open_run(context)
    # 재개가 돌면 _to_payload 가 티켓을 지우므로, 저장 실패 안내에 실을 Plan id(승인한 카드만)는 여기서 미리 뽑는다
    plan_ids = _approved_plan_ids(session, body.call_id if body.approved else None)
    # 이 요청에서 함께 돌 승인 카드로 본다 — 먼저 승인한 kubectl 카드가 있거나 지금이 kubectl 승인·PR 거절이면 예전처럼 끝난 뒤 답한다
    detach = body.approved and _only_merge_cards(session, _approved_call_ids(session) | {body.call_id})
    await conversation.lock.acquire()

    async def work() -> dict[str, Any]:
        _bind_run(session, run, user)
        # 결정 검사·기록은 server._approve 가 한다 (문자열 detail). 여기서는 코드 객체로 감싼다.
        try:
            with _with_cluster(session, conn):
                outcome, result = await _server._approve(session, body.call_id, body.approved)
        except HTTPException as exc:
            await repo_link_memo.settle(session.deps, saved=False)
            raise await _record_failure(run, _wrap(exc))
        return await _continue_run(conversation, run, outcome, result, plan_ids)

    return await _finish_locked(conversation, work, plan_ids, detach=detach)


@router.post("/{conversation_id}/resume")
async def resume(
    conversation_id: str,
    user: User = Depends(current_user),
) -> Any:
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
    plan_ids = _approved_plan_ids(session)
    # 새 결정이 없다 — 승인해 둔 카드로 본다. kubectl 카드까지 함께 돌면 그 동작을 바꾸지 않게 끝난 뒤 답한다
    detach = _only_merge_cards(session, _approved_call_ids(session))
    await conversation.lock.acquire()

    async def work() -> dict[str, Any]:
        _bind_run(session, run, user)
        try:
            with _with_cluster(session, conn):
                outcome, result = await _server._resume(session)
        except HTTPException as exc:
            await repo_link_memo.settle(session.deps, saved=False)
            raise await _record_failure(run, _wrap(exc))
        return await _continue_run(conversation, run, outcome, result, plan_ids)

    return await _finish_locked(conversation, work, plan_ids, detach=detach)


def _only_merge_cards(session: Any, call_ids: set[str]) -> bool:
    """이 카드들이 있고 전부 PR 머지 카드인가."""
    if session.pending is None:
        return False
    calls = [call for call in session.pending.approvals if call.tool_call_id in call_ids]
    return bool(calls) and all(call.tool_name == GITOPS_TOOL for call in calls)


def _log_detached(task: asyncio.Task[dict[str, Any]]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    # HTTPException 은 run·기록에 이미 남았다 (_record_failure·_save_or_fail). 그 밖의 것은 받아 줄 요청이 없을 수 있다
    if error is not None and not isinstance(error, HTTPException):
        logger.error("뒤에서 돈 요청(승인·채팅) 실패", exc_info=error)


async def _finish_locked(
    conversation: Conversation, work: Callable[[], Awaitable[dict[str, Any]]], plan_ids: list[str], *, detach: bool,
) -> dict[str, Any] | JSONResponse:
    """이미 쥔 방 잠금 안에서 승인·재개를 돌리고 잠금을 푼다. detach(PR 머지 승인)면 요청과 떼어 돌려 APPROVE_WAIT 를 넘으면 먼저 답한다 (DURO-158).

    머지 자체보다 그 앞뒤가 길 수 있다 — 길 다시 판단·GitHub 다시 보기·모델의 답(툴을 더 부르면 끝이 없다). 답을 앞단이 끊으면 앱은
    실패로 읽으므로 먼저 202 accepted 로 답한다. 일은 멈추지 않고 run·Plan 에 그대로 남고, 잠금도 끝날 때까지 쥔다(재클릭은 BUSY).
    앱은 Plan 과 대화 기록을 다시 읽어 결과와 답을 받는다. kubectl 길은 예전처럼 끝난 뒤 답한다.
    """
    session = conversation.session
    if not detach:
        try:
            return await work()
        finally:
            conversation.lock.release()
    return await _detach(conversation, work, APPROVE_WAIT, {
        "kind": "accepted", "skill": session.skill.name, "plan_ids": plan_ids,
        "message": "승인은 받았고 처리는 계속돼요. 결과는 승인 카드에 나타나요.",
    }, what="승인")


async def _detach(
    conversation: Conversation, work: Callable[[], Awaitable[dict[str, Any]]], wait: float,
    accepted: dict[str, Any], *, what: str,
) -> dict[str, Any] | JSONResponse:
    """이미 쥔 방 잠금 안에서 work 를 요청과 떼어 돌린다. wait 초 안에 끝나면 그 답(또는 오류)을, 넘으면 202 accepted 를 준다.

    잠금은 work 가 끝날 때 푼다. 끌 때는 server 연결을 닫기 전에 이 일을 정리한다 (gitops_after_merge.stop).
    """
    async def owned() -> dict[str, Any]:
        try:
            return await work()
        finally:
            conversation.lock.release()

    task = asyncio.create_task(owned())
    gitops_after_merge.keep_approval(task)      # 끌 때 server 연결을 닫기 전에 정리한다
    task.add_done_callback(_log_detached)
    await asyncio.wait({task}, timeout=wait)
    if task.done():
        return task.result()
    logger.warning("%s 처리가 %s초를 넘어 먼저 답한다 (conversation=%s)", what, wait, conversation.id)
    return JSONResponse(status_code=202, content=accepted)


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
    # session 은 숨은 칸 정리(repo_link_memo.settle)에만 쓰인다 — 실패 상태가 둘 다 활성이라 티켓·기록은 건드리지 않는다
    session = conversation.session
    if outcome.get("kind") == "approval":
        await _save_or_fail(conversation.id, run, **failure, session=session, status="awaiting_approval", **fields)
        return outcome
    # completed 는 종료 상태다 — 남은 계획이 있으면 server 가 같이 닫는다
    await _save_or_fail(conversation.id, run, **failure, session=session, status="completed", **fields)
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
