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

Private / Shared (기획 05): private 방은 만든 사람만 보고, 조회·진단만 — 변경 도구가 있는 모드(실습)로 못 들어간다.
shared 방은 팀원이 같이 보고 같이 입력하고 승인한다. 팀 소속·Operator 권한 검사는 Spring 팀 API 가 생기면
row.team_id 로 붙인다 — 지금은 로그인한 사용자면 된다. Private → Shared 전환은 없다 (문서 05 §3).
"""
from __future__ import annotations

import dataclasses
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
from kukie.store.models import RUN_ACTIVE
from kukie.tools.mutate import MUTATING_TOOLS

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
        "user_id": row.user_id,                  # 작성자 — 목록에 표시 (문서 05 §6)
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


def _load(conversation_id: str, user: User, store: ChatStore) -> tuple[SessionRow, Conversation]:
    """private 방은 주인만 (남에게는 없는 방, 404). shared 방은 팀원 누구나 읽고 입력한다 (문서 05 §4)."""
    row = store.get_session(conversation_id)
    if row is None or (row.user_id != user.id and not row.shared):
        raise _error(404, "NOT_FOUND", f"대화가 없다: {conversation_id}")
    if registry.get(conversation_id) is None:
        registry.get_or_load(conversation_id, store)   # 복원하면서 밀린 run 을 닫으므로 row 를 다시 읽는다
        row = store.get_session(conversation_id) or row
    conversation = registry.get(conversation_id)
    assert conversation is not None
    return row, conversation


def _mutating_mode(name: str) -> bool:
    skill = SKILLS.get(name)
    return skill is not None and bool(skill.allowed_tools & MUTATING_TOOLS)


def _private_change_blocked(row: SessionRow) -> HTTPException:
    return _error(403, "PRIVATE_SESSION",
                  "Private 대화에서는 클러스터를 변경할 수 없다 — 변경은 Shared 대화를 새로 만들어서 (기획 05)")


def _require_approver(row: SessionRow, user: User) -> None:
    """승인·재개 = 클러스터 변경. 기획 06 은 Admin 또는 대상의 Operator 인 팀원에게 여는데, 팀·권한 정보는 Spring 팀 API 가
    생겨야 온다. 그 전까지는 로그인한 아무나 승인하지 않도록 **방을 만든 사람만** (팀원 리뷰 6) — 팀 API 가 붙으면 넓힌다."""
    if not row.shared:
        raise _private_change_blocked(row)
    if row.user_id != user.id:
        raise _error(403, "FORBIDDEN", "승인·재개는 지금은 대화를 만든 사람만 할 수 있다 (팀 권한 검사가 붙기 전까지)")


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


def _bind_run(session: Any, run: RunRow, user: User) -> None:
    """이번 요청이 어느 run 의 것이고 누가 보냈는지 세션에 싣는다 — 가드레일 훅이 Action Plan 을
    tbl_action_plan 에 넣고 decision 에 결정자를 적을 때 쓴다 (#58). 매 요청 새로 덮어쓴다."""
    session.deps = dataclasses.replace(session.deps, run_id=run.id, user_id=user.id)


def _busy() -> HTTPException:
    return _error(409, "BUSY", "이전 요청 처리 중 — 끝난 뒤 다시 보내라")


# ── 엔드포인트 ─────────────────────────────────────────────

@router.post("")
async def create_conversation(
    body: ConversationIn,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    context = (body.context or "").strip()
    namespace = (body.namespace or "").strip()
    if not context:
        # 빈 context 로 방을 만들면 `kubectl --context ''` 가 kubeconfig 의 현재 context 를 따라가서, 나중에 current-context 가
        # 바뀌면 같은 방의 실행 대상이 바뀐다 (팀원 리뷰 1). 만들 때 실제 이름으로 확정한다.
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
    row, conversation = _load(conversation_id, user, store)
    session = conversation.session
    is_mode = body.text.startswith("/mode ")
    kind = "mode_change" if is_mode else "chat"
    if is_mode and not row.shared and _mutating_mode(body.text.removeprefix("/mode ").strip()):
        raise _private_change_blocked(row)       # 변경 도구가 있는 모드는 shared 방에서만 (문서 05 §3)

    # 재전송이면 실행하지 않고 저장된 결과를 그대로 — 승인 대기 검사보다 먼저 (응답 유실 뒤 재시도 규약)
    stored = store.find_run(conversation_id, body.request_id)
    if stored is not None:
        if stored.input_text != body.text or stored.kind != kind:
            raise _error(409, "REQUEST_MISMATCH", f"같은 request_id 로 다른 입력을 보냈다: {body.request_id}")
        if stored.status == "running" and not conversation.lock.locked():
            # 실행 중이라는데 이 방의 잠금이 비어 있다 = 결과 저장에 실패한 run 이다. 영원히 BUSY 를 재생하지 않도록 닫는다
            _interrupt(store, conversation_id)
            stored = store.find_run(conversation_id, body.request_id) or stored
        return _replay(stored)

    if conversation.lock.locked():
        raise _busy()
    if session.pending is not None:
        if session.pending_ids:
            raise _error(409, "PENDING_APPROVAL", "승인 대기 중 — /approve 로 먼저 결정")
        raise _error(409, "RECOVERY_REQUIRED", "재개가 실패한 요청이 있다 — /resume 으로 다시 시도")

    async with conversation.lock:
        # 잠금을 쥔 지금 이 방에 실행 중인 run 은 없고, 여기까지 왔으면 메모리에 이어갈 티켓(session.pending)도 없다.
        # 그런데 DB 에 활성 run(running / awaiting_approval / recovery_required)이 남아 있다면 결과 저장에 실패한 run 이다
        # (팀원 리뷰 3, 자동 리뷰 6차) — 그대로 두면 이 방은 영원히 BUSY 라, 중단으로 닫고 진행한다. 저장 실패 사정(error)은 남는다.
        stale = store.active_run(conversation_id)
        if stale is not None:
            logger.warning("결과가 저장되지 않은 run 을 닫는다 (conversation=%s, run=%s, status=%s)",
                           conversation_id, stale.id, stale.status)
            _interrupt(store, conversation_id)
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

        _bind_run(session, run, user)
        try:
            payload, result = await _server._chat_turn(session, body.text)
        except HTTPException as exc:
            wrapped = _wrap(exc)
            store.update_run(run.id, status="failed", response_payload=_error_record(wrapped))
            _close_plans(store, run)   # failed 는 종료 상태다 — 안 닫으면 계획이 영원히 열린 채 남는다
            raise wrapped
        except Exception as exc:                 # 모델·툴 예외 — run 은 실패로 남기고 세션은 유지
            logger.exception("run 실패 (conversation=%s, run=%s)", conversation_id, run.id)
            failed = _error(500, "RUN_FAILED", f"요청 처리에 실패했다 ({type(exc).__name__}) — 서버 로그 참고")
            store.update_run(run.id, status="failed", response_payload=_error_record(failed))
            _close_plans(store, run)
            raise failed from exc

        # 승인 카드면 run 은 열린 채(awaiting_approval) 남는다 — approve/resume 이 이어서 끝낸다 (문서 7절)
        status = "awaiting_approval" if payload.get("kind") == "approval" else "completed"
        _save_or_fail(
            store, conversation_id, run, on_failure=_chat_store_failure(), failure_status="failed",
            session=session, status=status, response_payload=payload,
            agent_messages=_messages_json(result) if result is not None else None,
            usage_summary=_usage_json(result) if result is not None else None,
        )
        if status == "completed":
            _close_plans(store, run)
        store.update_session(conversation_id, current_mode=session.skill.name)
        return payload


def _interrupt(store: ChatStore, conversation_id: str) -> None:
    """결과 저장에 실패해 활성으로 남은 run 을 닫는다. 함께 닫힌 계획의 .md 사본도 따라오게 한다 —
    계획을 DB 에서 직접 닫는 자리는 넷이고 규칙은 한 벌이어야 한다 (자동 리뷰 지적)."""
    from kukie.guardrail.action_plan import sync_markdown   # 순환 import 회피

    closed = store.interrupt_active_runs(
        conversation_id, "결과를 저장하지 못해 중단된 요청이다 — 새 요청으로 보내라"
    )
    try:
        sync_markdown(closed.plans)
    except Exception:
        logger.exception("중단으로 닫힌 계획의 .md 갱신 실패 (conversation=%s)", conversation_id)


def _close_plans(store: ChatStore, run: RunRow) -> None:
    """run 이 종료 상태로 닫힐 때 남은 계획도 닫는다. **여기서 터져도 원래 응답을 삼키면 안 된다.**

    성공(completed)에도 부른다. 정상 흐름이면 그 시점에 열린 계획이 없지만, kubectl 이 돈 뒤
    record_execution 이 실패하면 훅이 경고만 붙이고 결과를 정상 반환해 run 은 completed 로 닫히고
    계획은 EXECUTING 으로 남는다 (자동 리뷰 지적). 종료 상태면 종류를 가리지 않고 닫는다.

    같은 트랜잭션으로 합칠 수 없어(run 은 이미 커밋됐다) 좁은 틈이 남는다 — 둘 사이에 죽으면 계획이
    열린 채 남는다. 그 틈을 없애려면 update_run 과 한 트랜잭션이어야 하는데, 저장 실패 경로마다
    상태·payload 가 달라 지금 구조로는 묶이지 않는다 (자동 리뷰 지적). 다음 정리 대상으로 남긴다.
    """
    from kukie.guardrail.action_plan import sync_markdown   # 순환 import 회피 — 부를 때만 가져온다

    try:
        sync_markdown(store.expire_open_plans(run.id))
    except Exception:
        logger.exception("계획 닫기 실패 — run 은 이미 종료로 닫혔다 (run=%s)", run.id)


def _save_or_fail(
    store: ChatStore, conversation_id: str, run: RunRow, *, on_failure: HTTPException, failure_status: str,
    session: Any = None, keep_payload: bool = False, **fields: Any,
) -> None:
    """실행 결과 저장. 실패하면 run 을 failure_status 로라도 남기고 on_failure 를 던진다 — 그것도 안 되면 다음 chat 이
    잠금을 쥔 채 활성 run 을 닫는다. 호출자가 상태·문구를 고른다: 모델 답변만 잃은 chat 은 failed, kubectl 이 이미 돈
    승인·재개는 recovery_required ("변경은 적용됐을 수 있다", 문서 4절). keep_payload 면 대체 쓰기가 payload 를
    건드리지 않는다 — "여기서 계속하라" 는 경로는 계속할 재료(남은 카드)를 기록에 남겨야 한다.

    **종료 상태로 닫으면 메모리의 승인 티켓도 함께 버린다** (팀원 리뷰). DB 의 run 은 닫혔는데
    session.pending 이 남으면 그 방은 이 프로세스가 사는 동안 아무것도 못 한다 — /chat 은
    PENDING_APPROVAL, /approve 는 활성 run 이 없어 NOT_PENDING, /resume 은 다시 PENDING_APPROVAL
    이고, 다음 chat 의 복구 로직은 pending 검사에 먼저 막혀 닿지 못한다. 활성 상태로 남기는
    경로(awaiting_approval·recovery_required)는 티켓이 있어야 이어갈 수 있으므로 그대로 둔다.
    """
    try:
        store.update_run(run.id, **fields)
    except Exception as exc:
        logger.exception("실행 결과 저장 실패 (conversation=%s, run=%s)", conversation_id, run.id)
        try:
            if keep_payload:
                store.update_run(run.id, status=failure_status)
            else:
                store.update_run(run.id, status=failure_status, response_payload=_error_record(on_failure))
        except Exception:
            logger.exception("실패 표시도 저장하지 못했다 (run=%s)", run.id)
        # failed 는 종료 상태다 — 이 문으로 닫힌 run 의 계획도 함께 닫고(자동 리뷰 지적),
        # 메모리의 승인 티켓도 함께 버린다(팀원 리뷰). 둘 다 "DB 가 닫았으면 나머지도 닫는다" 다.
        # recovery_required·awaiting_approval 은 활성이라 나중에 interrupt 가 지나간다.
        if failure_status not in RUN_ACTIVE:
            _close_plans(store, run)
            if session is not None:
                session.pending = None
                session.decisions.clear()
        raise on_failure from exc


def _approved_plan_ids(session: Any, also_approved: str | None = None) -> list[str]:
    """티켓 중 **승인한** 카드의 Plan id 만 — "적용됐을 수 있는 변경" 목록에 거절한 카드가 섞이면 안 된다.
    also_approved 는 지금 처리 중인 결정(아직 session.decisions 에 없다)."""
    if session.pending is None:
        return []
    from pydantic_ai.tools import ToolApproved
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
    row, conversation = _load(conversation_id, user, store)
    session = conversation.session
    _require_approver(row, user)
    if conversation.lock.locked():
        raise _busy()
    run = _open_run(store, conversation_id)
    async with conversation.lock:
        _bind_run(session, run, user)
        # 재개가 돌면 _to_payload 가 티켓을 지우므로, 저장 실패 안내에 실을 Plan id(승인한 카드만)는 여기서 미리 뽑는다
        plan_ids = _approved_plan_ids(session, body.call_id if body.approved else None)
        # 결정 검사·기록은 server._approve 가 한다 (문자열 detail). 여기서는 코드 객체로 감싼다.
        try:
            outcome, result = await _server._approve(session, body.call_id, body.approved)
        except HTTPException as exc:
            raise _record_failure(store, run, _wrap(exc))
        return _continue_run(store, conversation, run, outcome, result, plan_ids)


@router.post("/{conversation_id}/resume")
async def resume(
    conversation_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    row, conversation = _load(conversation_id, user, store)
    session = conversation.session
    _require_approver(row, user)
    if conversation.lock.locked():
        raise _busy()
    if session.pending is None:
        raise _error(409, "NOT_PENDING", "재개할 승인 건이 없다")
    if session.pending_ids:
        raise _error(409, "PENDING_APPROVAL", f"아직 결정하지 않은 승인 건이 있다: {session.pending_ids}")
    run = _open_run(store, conversation_id)
    async with conversation.lock:
        _bind_run(session, run, user)
        plan_ids = _approved_plan_ids(session)
        try:
            outcome, result = await _server._resume(session)
        except HTTPException as exc:
            raise _record_failure(store, run, _wrap(exc))
        return _continue_run(store, conversation, run, outcome, result, plan_ids)


def _open_run(store: ChatStore, conversation_id: str) -> RunRow:
    """승인·재개가 이어갈 run — 이 방의 활성 run(awaiting_approval / recovery_required). 없으면 결정할 게 없다."""
    run = store.active_run(conversation_id)
    if run is None or run.status == "running":
        raise _error(409, "NOT_PENDING", "대기 중인 승인 건이 없다")
    return run


def _continue_run(
    store: ChatStore, conversation: Conversation, run: RunRow, outcome: dict[str, Any], result: Any,
    plan_ids: list[str],
) -> dict[str, Any]:
    """승인/재개 결과를 같은 run 에 남긴다 (문서 7절 "승인만으로 새 run 을 만들지 않는다").

    result 가 있으면 실제로 재개가 돌았다 — 그 메시지(tool 결과, 다음 tool call)를 이 run 에 이어 붙인다. 재개 뒤 새 카드가
    나와도 마찬가지다 (팀원 리뷰 2: 여기서 안 붙이면 복원 history 에 첫 작업의 결과와 둘째 작업의 호출이 빠진다).
    result 가 없으면 남은 카드 재전송이라 payload 만 바꾼다. 답이 나왔으면 completed, 카드가 나왔으면 awaiting_approval 그대로.
    """
    messages = (run.agent_messages or []) + (_messages_json(result) if result is not None else [])
    usage = _usage_json(result) if result is not None else run.usage_summary
    # 저장 실패의 뜻은 "실제로 재개가 돌았나"(result 유무)로 정한다: 안 돌았으면 카드 대기 그대로, 돌았으면 확인 필요 (문서 4절)
    failure = (
        dict(on_failure=_decision_store_failure(), failure_status="awaiting_approval", keep_payload=True)
        if result is None
        else dict(on_failure=_resume_store_failure(plan_ids), failure_status="recovery_required")
    )
    if outcome.get("kind") == "approval":
        _save_or_fail(store, conversation.id, run, **failure, status="awaiting_approval", response_payload=outcome,
                      agent_messages=messages or None, usage_summary=usage)
        return outcome
    _save_or_fail(store, conversation.id, run, **failure, status="completed", response_payload=outcome,
                  agent_messages=messages or None, usage_summary=usage)
    _close_plans(store, run)      # completed 도 종료 상태다 — 남은 계획이 있으면 닫는다
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
