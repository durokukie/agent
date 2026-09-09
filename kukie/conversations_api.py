"""대화(채팅방) 단위 엔드포인트 — kukie-electron docs/api-spec.md 1절.

  POST /conversations                    채팅방 만들기 (= 세션 시작)
  GET  /conversations?cluster_id=        내 채팅방 목록
  GET  /conversations/{id}               채팅방 + 대화 기록(turns)
  POST /conversations/{id}/chat          한 턴. request_id 로 멱등
  POST /conversations/{id}/approve       승인 카드 결정
  POST /conversations/{id}/resume        재개 실패 뒤 다시 시도

flat 엔드포인트(/session /chat /approve /resume)는 server.py 에 그대로 있다. 앱이 대화 단위로
옮겨 오면 flat 은 지운다. 실제 run 로직은 server.py 의 _chat_turn / _approve / _resume 을 같이 쓴다 —
여기는 "어느 채팅방인지 찾고, 잠그고, DB 에 기록" 만 더한다.

run 은 사용자 요청 한 건이다 (DB 문서 7절). chat 이 승인 카드를 주면 그 run 은 awaiting_approval 로 열려
있고, approve / resume 은 새 run 을 만들지 않고 **그 run 을 이어서** completed 로 끝낸다. 재개가 실패하면
recovery_required 로 남아 /resume 으로 다시 시도한다. 채팅방 하나에 활성 run 은 하나 — 그동안 새 chat 은 409.

오류는 전부 FastAPI 의 detail 자리에 {code, message} 객체로 간다 — 응답은 {"detail": {code, message}}.
앱의 http.ts 가 detail 이 객체면 code 를 꺼낸다 (api-spec 공통 절).

권한: 남의 방은 404. shared 방은 남도 읽을 수 있지만 chat/approve/resume 은 주인만 (승인 = 클러스터 변경).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelMessagesTypeAdapter

import kukie.server as _server  # 순환 import: 이름은 호출 시점에만 쓴다
from kukie.auth import User, current_user
from kukie.conversations import Conversation, registry
from kukie.skills import SKILLS
from kukie.store import ChatStore, get_store
from kukie.store.chat_store import ActiveRunExists, RequestMismatch, RunRow, SessionRow, error_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ── 요청 본문 ──────────────────────────────────────────────

class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str | None = None
    team_id: str | None = None
    # 클러스터 레코드(Spring)가 생기기 전까지는 context/namespace 를 직접 받거나 kubeconfig 를 읽는다
    context: str | None = None
    namespace: str | None = None
    installation_id: str | None = None        # 문서 3절 — kubectl 을 실행하는 agent 설치 환경 (앱이 알면 준다)
    cluster_fingerprint: str | None = None    # 문서 3절 — 실제 대상 클러스터 확인값 (아직 서버가 계산하지 않는다)
    title: str = "새 대화"
    shared: bool = False


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    approved: bool


# ── 응답 조립 ──────────────────────────────────────────────

def _conversation_view(row: SessionRow, running: bool) -> dict[str, Any]:
    return {
        "id": row.id,
        "cluster_id": row.cluster_id,
        "team_id": row.team_id,
        "title": row.title,
        "shared": row.shared,
        "version": row.version,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "running": running,
    }


def _turn_view(run: RunRow) -> dict[str, Any]:
    return {
        "id": run.id,
        "seq": run.turn_no,
        "kind": run.kind,
        "mode": run.mode,
        "status": run.status,
        "input_text": run.input_text,
        "payload": run.response_payload,
        "error": run.error,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _load(
    conversation_id: str, user: User, store: ChatStore, *, write: bool = False,
) -> tuple[SessionRow, Conversation]:
    row = store.get_session(conversation_id)
    if row is None or (row.user_id != user.id and not row.shared):
        raise _error(404, "NOT_FOUND", f"대화가 없다: {conversation_id}")
    if write and row.user_id != user.id:
        raise _error(403, "FORBIDDEN", "공유 대화는 읽기만 할 수 있다 — 메시지·승인은 만든 사람만")
    if registry.get(conversation_id) is None:
        registry.get_or_load(conversation_id, store)   # 복원하면서 밀린 run 을 닫으므로 row 를 다시 읽는다
        row = store.get_session(conversation_id) or row
    conversation = registry.get(conversation_id)
    assert conversation is not None
    return row, conversation


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


def _busy() -> HTTPException:
    return _error(409, "BUSY", "이전 요청 처리 중 — 끝난 뒤 다시 보내라")


# ── 엔드포인트 ─────────────────────────────────────────────

@router.post("")
async def create_conversation(
    body: ConversationIn,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    context, namespace = body.context, body.namespace
    if context is None:
        try:
            context, kube_ns = _server.read_kubeconfig()
        except _server.KubeconfigError as exc:
            raise _error(503, "KUBECONFIG", f"kubeconfig 를 읽을 수 없다: {exc}") from exc
        namespace = namespace or kube_ns
    row = store.create_session(
        user_id=user.id, context_name=context, namespace=namespace or "default",
        mode=_server.DEFAULT_SKILL.name, title=body.title,
        installation_id=body.installation_id, cluster_fingerprint=body.cluster_fingerprint,
        team_id=body.team_id, cluster_id=body.cluster_id, shared=body.shared,
    )
    conversation = registry.register(row)
    return {
        "conversation": _conversation_view(row, running=False),
        "session": _server._session_view(conversation.session),
    }


@router.get("")
async def list_conversations(
    cluster_id: str | None = None,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> list[dict[str, Any]]:
    rows = store.list_sessions(user.id, cluster_id=cluster_id)
    return [_conversation_view(r, running=_is_running(r)) for r in rows]


def _is_running(row: SessionRow) -> bool:
    live = registry.get(row.id)
    return row.running or (live is not None and live.running)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    row, conversation = _load(conversation_id, user, store)
    return {
        "conversation": _conversation_view(row, running=_is_running(row)),
        "session": _server._session_view(conversation.session),
        "turns": [_turn_view(r) for r in store.list_runs(conversation_id)],
    }


@router.post("/{conversation_id}/chat")
async def chat(
    conversation_id: str,
    body: ChatIn,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    _, conversation = _load(conversation_id, user, store, write=True)
    session = conversation.session
    is_mode = body.text.startswith("/mode ")
    kind = "mode_change" if is_mode else "chat"

    # 재전송이면 실행하지 않고 저장된 결과를 그대로 — 승인 대기 검사보다 먼저 (응답 유실 뒤 재시도 규약)
    stored = store.find_run(conversation_id, body.request_id)
    if stored is not None:
        if stored.input_text != body.text or stored.kind != kind:
            raise _error(409, "REQUEST_MISMATCH", f"같은 request_id 로 다른 입력을 보냈다: {body.request_id}")
        return _replay(stored)

    if conversation.lock.locked():
        raise _busy()
    if session.pending is not None:
        if session.pending_ids:
            raise _error(409, "PENDING_APPROVAL", "승인 대기 중 — /approve 로 먼저 결정")
        raise _error(409, "RECOVERY_REQUIRED", "재개가 실패한 요청이 있다 — /resume 으로 다시 시도")

    async with conversation.lock:
        try:
            run, created = store.start_run(
                conversation_id, request_id=body.request_id, kind=kind,
                mode=session.skill.name, input_text=body.text,
            )
        except RequestMismatch:
            raise _error(409, "REQUEST_MISMATCH", f"같은 request_id 로 다른 입력을 보냈다: {body.request_id}")
        except ActiveRunExists:
            raise _busy()
        if not created:
            return _replay(run)

        try:
            payload, result = await _server._chat_turn(session, body.text)
        except HTTPException as exc:
            wrapped = _wrap(exc)
            store.update_run(run.id, status="failed", response_payload=_error_record(wrapped))
            raise wrapped
        except Exception as exc:                 # 모델·툴 예외 — run 은 실패로 남기고 세션은 유지
            logger.exception("run 실패 (conversation=%s, run=%s)", conversation_id, run.id)
            failed = _error(500, "RUN_FAILED", f"요청 처리에 실패했다 ({type(exc).__name__}) — 서버 로그 참고")
            store.update_run(run.id, status="failed", response_payload=_error_record(failed))
            raise failed from exc

        # 승인 카드면 run 은 열린 채(awaiting_approval) 남는다 — approve/resume 이 이어서 끝낸다 (문서 7절)
        status = "awaiting_approval" if payload.get("kind") == "approval" else "completed"
        store.update_run(
            run.id, status=status, response_payload=payload,
            agent_messages=_messages_json(result) if result is not None else None,
            usage_summary=_usage_json(result) if result is not None else None,
        )
        store.update_session(conversation_id, current_mode=session.skill.name)
        return payload


def _replay(run: RunRow) -> dict[str, Any]:
    if run.status == "running":
        raise _error(409, "BUSY", "같은 요청을 아직 처리 중이다")
    if run.error is not None:
        raise HTTPException(int(run.error.get("http_status", 500)),
                            {"code": run.error.get("code", "RUN_FAILED"), "message": run.error.get("message", "")})
    assert run.response_payload is not None
    return run.response_payload


@router.post("/{conversation_id}/approve")
async def approve(
    conversation_id: str,
    body: ApproveIn,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    _, conversation = _load(conversation_id, user, store, write=True)
    session = conversation.session
    if conversation.lock.locked():
        raise _busy()
    run = _open_run(store, conversation_id)
    async with conversation.lock:
        # 결정 검사·기록은 server._approve 가 한다 (문자열 detail). 여기서는 코드 객체로 감싼다.
        try:
            outcome, result = await _server._approve(session, body.call_id, body.approved)
        except HTTPException as exc:
            raise _record_failure(store, run, _wrap(exc))
        return _continue_run(store, conversation, run, outcome, result)


@router.post("/{conversation_id}/resume")
async def resume(
    conversation_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    _, conversation = _load(conversation_id, user, store, write=True)
    session = conversation.session
    if conversation.lock.locked():
        raise _busy()
    if session.pending is None:
        raise _error(409, "NOT_PENDING", "재개할 승인 건이 없다")
    if session.pending_ids:
        raise _error(409, "PENDING_APPROVAL", f"아직 결정하지 않은 승인 건이 있다: {session.pending_ids}")
    run = _open_run(store, conversation_id)
    async with conversation.lock:
        try:
            outcome, result = await _server._resume(session)
        except HTTPException as exc:
            raise _record_failure(store, run, _wrap(exc))
        return _continue_run(store, conversation, run, outcome, result)


def _open_run(store: ChatStore, conversation_id: str) -> RunRow:
    """승인·재개가 이어갈 run — 이 방의 활성 run(awaiting_approval / recovery_required). 없으면 결정할 게 없다."""
    run = store.active_run(conversation_id)
    if run is None or run.status == "running":
        raise _error(409, "NOT_PENDING", "대기 중인 승인 건이 없다")
    return run


def _continue_run(
    store: ChatStore, conversation: Conversation, run: RunRow, outcome: dict[str, Any], result: Any,
) -> dict[str, Any]:
    """승인/재개 결과를 같은 run 에 남긴다 (문서 7절 "승인만으로 새 run 을 만들지 않는다").

    카드가 남았으면(outcome 이 approval) 남은 카드로 payload 만 바꾸고 awaiting_approval 그대로.
    재개돼 답이 나왔으면 이 run 의 메시지에 재개 run 의 메시지(tool 결과 + 답)를 이어 붙이고 completed.
    """
    if outcome.get("kind") == "approval":
        store.update_run(run.id, status="awaiting_approval", response_payload=outcome)
        return outcome
    messages = (run.agent_messages or []) + (_messages_json(result) if result is not None else [])
    store.update_run(
        run.id, status="completed", response_payload=outcome, agent_messages=messages or None,
        usage_summary=_usage_json(result) if result is not None else run.usage_summary,
    )
    store.update_session(conversation.id, current_mode=conversation.session.skill.name)
    return outcome


def _record_failure(store: ChatStore, run: RunRow, exc: HTTPException) -> HTTPException:
    """승인/재개가 실행 중 실패하면(5xx) run 을 recovery_required 로 — 문서 4절 "실제 변경 결과가 불명확해 확인
    필요". 티켓은 살아 있어 /resume 으로 이어진다. 4xx(모르는 call_id, 이미 결정)는 실행 전 거절이라 run 을
    건드리지 않는다."""
    if exc.status_code >= 500:
        store.update_run(run.id, status="recovery_required", response_payload=_error_record(exc))
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
    """server.py 가 던진 문자열 detail 을 코드 객체로. 이미 객체면(RESUME_RETRYABLE) 그대로."""
    if isinstance(exc.detail, dict):
        return exc
    text = str(exc.detail)
    code = next((c for prefix, c in _CODES if text.startswith(prefix) or prefix in text), "ERROR")
    return HTTPException(exc.status_code, {"code": code, "message": text})


_server.app.include_router(router)   # server.py 맨 아래의 plain import 가 이 줄을 실행시킨다

__all__ = ["router", "SKILLS"]
