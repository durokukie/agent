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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai.messages import ModelMessagesTypeAdapter

import kukie.server as _server  # 순환 import: 이름은 호출 시점에만 쓴다
from kukie.auth import User, current_user
from kukie.clusters import crypto
from kukie import membership
from kukie.clusters.access import ClusterChanged, ClusterGone, kubeconfig_or_none
from kukie.conversations import Conversation, registry
from kukie.fields import blank_is_none, none_if_blank, stripped
from kukie.skills import SKILLS
from kukie.store import ChatStore, get_store
from kukie.store.chat_store import ActiveRunExists, RequestMismatch, RunRow, SessionRow, error_payload
from kukie.store.models import DEFAULT_TITLE, RUN_ACTIVE
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
    title: str = DEFAULT_TITLE
    shared: bool = False

    # `team_id: ""` 는 falsy 검사(팀 없음)와 IN 검사(그 팀만) 사이로 새어, 목록에서는 사라지는데
    # 상세는 200 이 되는 방을 만든다 (자동 리뷰 지적). 클러스터 등록과 같은 규칙으로 접는다.
    _blank = field_validator("cluster_id", "team_id", "context", "namespace",
                             "installation_id", "cluster_fingerprint", mode="before")(blank_is_none)

    # 빈 제목은 기본 제목으로. `title: ""` 로 만든 방은 `row.title != DEFAULT_TITLE` 이 늘 참이라
    # 첫 마디로 제목을 받을 자격을 영영 잃는다 (자동 리뷰 지적).
    _title = field_validator("title", mode="before")(
        # 문자열이 아닌 값은 stripped 가 그대로 흘려보내 str 검사에서 422 가 된다. 여기서 바로
        # value.strip() 을 부르면 `{"title": 123}` 이 AttributeError → 500 으로 샌다 (자동 리뷰 지적).
        lambda cls, value: DEFAULT_TITLE if blank_is_none(cls, value) is None else stripped(cls, value)
    )


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


async def _load(conversation_id: str, user: User, store: ChatStore) -> tuple[SessionRow, Conversation]:
    """private 방은 주인만 (남에게는 없는 방, 404). shared 방은 **그 팀의 구성원**이 읽고 입력한다 (기획 05 §4).

    팀 검사가 여기에도 있어야 한다. `/chat` 은 이 방의 클러스터로 kubectl 을 돌리므로, 로그인한
    아무나 들어올 수 있으면 클러스터 소유권 검사를 방 만들 때만 걸어 둔 것이 무의미해진다 (자동 리뷰 🔴).
    팀이 없는 방과 회원 서버가 없는 개발 모드는 예전대로 로그인한 사용자면 된다.
    """
    row = store.get_session(conversation_id)
    if row is None or (row.user_id != user.id and not row.shared):
        raise _error(404, "NOT_FOUND", f"대화가 없다: {conversation_id}")
    # 팀이 붙은 **shared** 방은 만든 사람에게도 지금 소속을 묻는다. 팀 공간이므로 나간 사람은 보면 안 된다.
    #
    # 팀이 없는 shared 방은 **만든 사람만** 연다 (#75). 누구에게 열지 물을 팀이 없는데, 예전처럼
    # "로그인한 아무나" 로 열면 남이 서버 kubeconfig 로 도는 방에 들어온다. 회원 서버 모드에서 새로
    # 생기는 팀 없는 방은 개인 클러스터 방뿐이고(그 클러스터는 등록한 사람만 쓴다 — _readable),
    # 전에 만들어진 클러스터 없는 방도 남아 있다.
    #
    # private 방은 읽기까지 막지 않는다. 자기 대화 기록인데 팀에서 나갔다는 이유로 열 수 없으면
    # 되돌릴 방법이 없다 (대화 삭제 API 도 없다 — 자동 리뷰 지적). 대신 **클러스터를 쓰는 것**은
    # 아래 _require_cluster_access 가 막는다. 읽기는 열고 실행은 닫는다.
    if row.shared and membership.available():
        if not row.team_id:
            if row.user_id != user.id:
                raise _error(404, "NOT_FOUND", f"대화가 없다: {conversation_id}")
        elif not await membership.is_member(user, row.team_id):
            raise _error(404, "NOT_FOUND", f"대화가 없다: {conversation_id}")
    if membership.available() and not row.cluster_id:
        _close_unrunnable(store, conversation_id)   # 복원(get_or_load)보다 먼저 — 닫힌 카드는 되살아나지 않는다
    if registry.get(conversation_id) is None:
        registry.get_or_load(conversation_id, store)   # 복원하면서 밀린 run 을 닫으므로 row 를 다시 읽는다
        row = store.get_session(conversation_id) or row
    conversation = registry.get(conversation_id)
    assert conversation is not None
    return row, conversation


def _close_unrunnable(store: ChatStore, conversation_id: str) -> None:
    """회원 서버 모드의 클러스터 없는 방에 걸린 활성 run 을 닫는다 (#75, PR #76 리뷰).

    그런 방은 chat · approve · resume 이 전부 CLUSTER_REQUIRED 로 끊긴다. 승인 카드가 걸려 있으면
    닫을 길이 없다 — 활성 run 을 정리하는 자리(chat 의 잠금 안쪽)가 그 409 뒤에 있고, 재시작 복원은
    카드를 되살린다. 그래서 **불러올 때** 닫는다. 되살릴 수 없는 카드를 get_or_load 가 닫는 것과 같은
    규칙이고, 계획도 같이 EXPIRED / UNKNOWN 으로 닫힌다.

    메모리에 떠 있는 세션은 고치지 않고 **버린다**. 카드만 지우면 history 에 답 없는 tool call 이 남아,
    완료된 run 만 모으는 재시작 복원(_restore_history)과 상태가 갈린다 (PR #76 리뷰). 버리면 _load 의
    get_or_load 가 재시작과 같은 규칙으로 다시 만들고, row 도 같이 다시 읽힌다.

    실행 중인 방은 건드리지 않는다. locked() 검사와 닫기 사이에 await 가 없다 (체크리스트 "잠금 틈").
    회원 서버 모드에서 이 방은 잠금을 잡기 전에 CLUSTER_REQUIRED 로 끊기므로 실제로는 닿지 않는 방어다.
    """
    live = registry.get(conversation_id)
    if live is not None and live.lock.locked():
        return
    if store.active_run(conversation_id) is None:
        return
    _interrupt(store, conversation_id,
               "클러스터 없는 대화라 실행할 수 없어 닫았다 — 클러스터를 골라 새 대화를 시작해 주세요")
    registry.forget(conversation_id)


def _mutating_mode(name: str) -> bool:
    skill = SKILLS.get(name)
    return skill is not None and bool(skill.allowed_tools & MUTATING_TOOLS)


def _private_change_blocked(row: SessionRow) -> HTTPException:
    return _error(403, "PRIVATE_SESSION",
                  "Private 대화에서는 클러스터를 변경할 수 없다 — 변경은 Shared 대화를 새로 만들어서 (기획 05)")


async def _require_approver(row: SessionRow, user: User) -> None:
    """승인·재개 = 클러스터 변경. 기획 06 §3 은 팀 Admin 에게 연다.

    팀이 붙은 shared 방은 Spring 에 역할을 물어 **구성원인지 먼저 보고**, Admin 이거나 방을 만든
    사람이면 통과시킨다 (kukie/membership.py).
    팀이 없는 방이나 회원 서버가 없는 개발 모드는 예전 규칙대로 **만든 사람만** — 로그인한 아무나
    남의 클러스터를 바꾸지 못하게 (팀원 리뷰 6).

    회원 서버 모드에서 팀이 없는 shared 방은 개인 클러스터 방뿐이다 (#75 뒤로 클러스터 없는 방은 못 쓴다).
    그 방은 만든 사람만 보므로(_load) shared 는 "같이 본다" 가 아니라 **변경 모드를 여는 것**만 남고,
    승인도 만든 사람 자신이 한다 — 클러스터가 등록한 사람 것이라 자기 승인이다 (PR #76 리뷰).
    앱은 클러스터를 항상 팀으로 등록하므로 이 조합을 만들지 않는다. 회원 서버 모드에 개인 클러스터를
    둘지는 기획 02 §2("Cluster 는 하나의 Team 에 소속") 결정으로 넘긴다.

    기획 06 은 "대상의 Operator 권한이 있는 Member" 도 승인할 수 있다고 하는데 Operator 는 아직 없다 (기획 03).
    """
    if not row.shared:
        raise _private_change_blocked(row)
    if row.team_id and membership.available():
        # 소속을 먼저 본다. 주인을 먼저 통과시키면 나간 사람이 옛 방에서 계속 승인한다 (자동 리뷰 🔴).
        # 소속만 확인되면 Admin 이거나 **방을 만든 사람**이면 승인할 수 있다.
        #
        # 기획 06 §3 은 "요청한 사용자 본인도 자신의 Plan 을 승인할 수 있다" 인데, 여기 비교하는 값은
        # 요청자가 아니라 방 생성자다 — run 에 요청자 칸이 없다 (DB 문서 6절이 회원 id 중복 저장을
        # 금한다). shared 방은 팀원 누구나 입력하므로 둘이 갈릴 수 있다 (자동 리뷰 지적):
        # 남이 요청한 카드를 방 주인이 누를 수 있고, 요청한 Member 는 자기 카드를 못 누른다.
        # 팀 밖으로 새지는 않아 지금은 이대로 두고, 요청자를 run 에 남길지는 #62 의 requested_by
        # 질문과 함께 정한다.
        if await membership.require_member(user, row.team_id) == "ADMIN" or row.user_id == user.id:
            return
        raise _error(403, "NOT_TEAM_ADMIN", "승인·재개는 팀 Admin 이나 대화를 만든 사람만 할 수 있다")
    if row.user_id == user.id:
        return
    raise _error(403, "FORBIDDEN", "승인·재개는 대화를 만든 사람이나 팀 Admin 만 할 수 있다")


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


#: 등록된 클러스터로 실행할 수 없는 사정들. 셋 다 서버·설정 문제라 대화는 살려 두고 안내만 한다.
CLUSTER_FAILURES = (ClusterGone, ClusterChanged, crypto.CredentialUnreadable, crypto.SecretKeyMissing)


def _cluster_error(exc: Exception, cluster_id: str | None) -> HTTPException:
    if isinstance(exc, ClusterChanged):
        return _error(409, "CLUSTER_CHANGED",
                      "이 대화가 쓰던 클러스터의 접속 대상이 바뀌었습니다 — 새 대화를 시작해 주세요")
    if isinstance(exc, ClusterGone):
        return _error(404, "CLUSTER_GONE",
                      f"이 대화가 쓰던 클러스터가 삭제되었습니다: {cluster_id}")
    if isinstance(exc, crypto.SecretKeyMissing):
        return _error(503, "SECRET_KEY_MISSING", str(exc))
    return _error(503, "CREDENTIAL_UNREADABLE", str(exc))


async def _require_cluster_access(store: ChatStore, row: SessionRow, user: User) -> None:
    """이 방의 클러스터를 지금 쓸 수 있나. 읽기(_load)와 달리 **실행 직전**에 본다.

    팀에서 나간 사람이 자기 private 방은 계속 읽되 그 팀 클러스터로 kubectl 을 돌리지는 못하게 한다.
    판단 규칙은 /clusters 의 _readable 과 같다.

    회원 서버 모드에서 클러스터가 없는 방은 실행하지 않는다 (#75). 그런 방은 _with_cluster 가 서버
    컴퓨터의 kubeconfig 를 쓰는데, 그 클러스터가 누구 것인지 물을 곳이 없다 — 기획 05 §1 의
    Team → Cluster → Session 에도 없는 모양이다. 개발 모드는 예전대로 그 kubeconfig 로 돈다.
    """
    if not membership.available():
        return
    if not row.cluster_id:
        raise _error(409, "CLUSTER_REQUIRED",
                     "이 대화에는 클러스터가 없어 실행할 수 없습니다 — 클러스터를 골라 새 대화를 시작해 주세요")
    cluster = store.get_cluster(row.cluster_id)
    if cluster is None or not cluster.team_id:
        return
    if not await membership.is_member(user, cluster.team_id):
        raise _error(403, "NOT_TEAM_MEMBER",
                     "이 대화가 쓰는 클러스터의 팀 구성원이 아닙니다")


def _with_cluster(store: ChatStore, session: Any, row: SessionRow):
    """실행 동안만 임시 kubeconfig 를 연다 (기획 04 §8). 블록을 벗어나면 파일이 지워진다.

    등록된 클러스터가 없는 방(옛 방·로컬 개발)은 아무것도 하지 않고 서버 컴퓨터의 기본 kubeconfig 를 쓴다.
    """
    from contextlib import contextmanager

    @contextmanager
    def opened():
        with kubeconfig_or_none(store, row.cluster_id, expect_fingerprint=row.cluster_fingerprint) as path:
            before = session.deps
            session.deps = dataclasses.replace(before, kubeconfig=path)
            try:
                yield
            finally:
                # 파일은 곧 사라진다 — 경로를 세션에 남기면 다음 요청이 없는 파일을 가리킨다
                session.deps = dataclasses.replace(session.deps, kubeconfig=None)

    return opened()


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
    fingerprint = body.cluster_fingerprint
    team_id = body.team_id
    if not body.cluster_id and membership.available():
        # 회원 서버 모드에서는 등록된 클러스터를 골라야 방이 생긴다 (#75, 기획 05 §1 Team → Cluster → Session).
        # 클러스터 없는 방은 서버 컴퓨터의 kubeconfig 로 도는데, 예전에는 팀만 적거나 아무것도 안 적으면
        # 만들어져서 로그인한 누구나 그 kubeconfig 를 쓰는 방을 열 수 있었다. 팀만 적은 방도 막는다 —
        # 서버 kubeconfig 는 그 팀의 클러스터가 아니다.
        raise _error(400, "CLUSTER_REQUIRED",
                     "클러스터를 골라야 대화를 만들 수 있습니다 — 팀 설정에서 클러스터를 먼저 연결해 주세요")
    if body.cluster_id:
        # 등록된 클러스터를 골랐다 (기획 04 §8). 접속 대상은 그 행이 정한다 — 화면이 보낸 값보다 우선한다.
        #
        # **소유권을 반드시 확인한다.** 이 검사가 없으면 남의 클러스터 id 를 넣어 방을 만들고,
        # 그 방에서 승인해 남의 클러스터를 바꿀 수 있다 (자동 리뷰 P1). 실행할 때 자격증명이
        # 복호화되어 kubectl 로 가므로 조회로 끝나지 않는다.
        cluster = store.get_cluster(body.cluster_id)
        if cluster is None:
            raise _error(404, "NOT_FOUND", f"클러스터가 없다: {body.cluster_id}")
        # 판단 규칙은 /clusters 의 _readable 과 같아야 한다 (자동 리뷰 🔴). 팀 클러스터는 등록자여도
        # 지금 소속으로, 개인 클러스터는 등록한 사람만. 두 규칙이 어긋나면 양방향으로 샌다 —
        # 팀에서 나간 사람이 방을 통해 계속 바꾸거나, 팀원인데 방을 못 만들거나.
        if cluster.team_id and membership.available():
            if not await membership.is_member(user, cluster.team_id):
                raise _error(404, "NOT_FOUND", f"클러스터가 없다: {body.cluster_id}")
        elif cluster.registered_by != user.id:
            raise _error(404, "NOT_FOUND", f"클러스터가 없다: {body.cluster_id}")
        if body.team_id and body.team_id != cluster.team_id:
            # 조용히 덮으면 요청보다 **더 열린** 방이 된다 — 개인 클러스터를 고르고 team_id 를 같이
            # 보내면 team_id 가 None 으로 사라져 "팀으로 좁혀 달라" 가 "전원 공개" 가 됐다 (자동 리뷰).
            raise _error(400, "CLUSTER_TEAM_MISMATCH",
                         "고른 클러스터의 팀과 team_id 가 다릅니다 — 방의 팀은 클러스터가 정합니다")
        context = cluster.context_name
        namespace = namespace or cluster.default_namespace
        fingerprint = cluster.fingerprint      # 방이 지문을 복사해 "승인한 대상 = 실행 대상" 을 확인한다
        # 방의 team_id 는 클러스터가 정한다. 화면이 보낸 값을 그대로 믿으면 "승인 권한은 방의 팀으로
        # 판단하는데 실제로 바뀌는 대상은 다른 팀의 클러스터" 인 방이 만들어진다 (자동 리뷰 🔴)
        team_id = cluster.team_id
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
        installation_id=body.installation_id, cluster_fingerprint=fingerprint,
        team_id=team_id, cluster_id=body.cluster_id, shared=body.shared,
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
    # 목록과 상세의 답이 같아야 한다 — 열 수 없는 방이 목록에 뜨면 사용자가 막다른 길에 선다
    mine = list(await membership.team_roles(user)) if membership.available() else None
    rows = store.list_sessions(user.id, cluster_id=none_if_blank(cluster_id), team_ids=mine)
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
    row, conversation = await _load(conversation_id, user, store)
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
    row, conversation = await _load(conversation_id, user, store)
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

    # 권한 검사는 **잠금 검사보다 먼저** 한다. 이유가 둘이다 (자동 리뷰).
    #   - run 을 만들기 전이어야 한다. 뒤에서 던지면 그 run 이 running 인 채 남고, 같은 request_id 로
    #     재시도하면 403 대신 409 INTERRUPTED 라는 엉뚱한 안내가 나간다
    #   - `lock.locked()` 검사와 `async with lock` 사이에 await 가 있으면 안 된다. 그 창에서 루프를
    #     놓으면 두 요청이 나란히 통과해 같은 방에 run 이 둘 생긴다 (체크리스트 "잠금 틈")
    await _require_cluster_access(store, row, user)
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
        before = list(session.history)   # 저장에 실패해 이 턴을 버릴 때 되돌릴 자리 (자동 리뷰 지적)
        try:
            with _with_cluster(store, session, row):
                payload, result = await _server._chat_turn(session, body.text)
        except CLUSTER_FAILURES as exc:
            wrapped = _cluster_error(exc, row.cluster_id)
            store.update_run(run.id, status="failed", response_payload=_error_record(wrapped))
            raise wrapped from None
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
            session=session, history_before=before, status=status, response_payload=payload,
            agent_messages=_messages_json(result) if result is not None else None,
            usage_summary=_usage_json(result) if result is not None else None,
        )
        if status == "completed":
            _close_plans(store, run)
        store.update_session(conversation_id, current_mode=session.skill.name)
        # 첫 마디로 방 제목을 짓는다 (#59). 제목이 될 수 있는 마디인지, 첫 턴인지는 저장소가 본다 —
        # 판정이 두 곳에 갈라지면 한쪽만 고쳐진다 (자동 리뷰 지적). 라우팅 조건(`/mode ` 공백 포함)은
        # router.py·server.py 와 셋이 같아야 하므로 그쪽은 건드리지 않는다.
        if kind == "chat":
            try:
                store.name_from_first_message(conversation_id, body.text)
            except Exception:
                # 제목은 부가 정보다. 여기서 터지면 이미 completed 로 저장한 run 이 500 으로 뒤집힌다
                logger.exception("제목 저장 실패 (conversation=%s)", conversation_id)
        return payload


def _interrupt(store: ChatStore, conversation_id: str,
               message: str = "결과를 저장하지 못해 중단된 요청이다 — 새 요청으로 보내라") -> None:
    """활성으로 남은 run 을 닫는다. 함께 닫힌 계획의 .md 사본도 따라오게 한다 —
    계획을 DB 에서 직접 닫는 자리는 넷이고 규칙은 한 벌이어야 한다 (자동 리뷰 지적)."""
    from kukie.guardrail.action_plan import sync_markdown   # 순환 import 회피

    closed = store.interrupt_active_runs(conversation_id, message)
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
    session: Any = None, history_before: list[Any] | None = None, keep_payload: bool = False,
    **fields: Any,
) -> None:
    """실행 결과 저장. 실패하면 run 을 failure_status 로라도 남기고 on_failure 를 던진다 — 그것도 안 되면 다음 chat 이
    잠금을 쥔 채 활성 run 을 닫는다. 호출자가 상태·문구를 고른다: 모델 답변만 잃은 chat 은 failed, kubectl 이 이미 돈
    승인·재개는 recovery_required ("변경은 적용됐을 수 있다", 문서 4절). keep_payload 면 대체 쓰기가 payload 를
    건드리지 않는다 — "여기서 계속하라" 는 경로는 계속할 재료(남은 카드)를 기록에 남겨야 한다.

    **종료 상태로 닫으면 메모리의 승인 티켓도, 그 턴의 기록도 함께 버린다** (팀원 리뷰 + 자동 리뷰).
    DB 의 run 은 닫혔는데 session.pending 이 남으면 그 방은 이 프로세스가 사는 동안 아무것도 못
    한다 — /chat 은 PENDING_APPROVAL, /approve 는 활성 run 이 없어 NOT_PENDING, /resume 은 다시
    PENDING_APPROVAL 이고, 다음 chat 의 복구 로직은 pending 검사에 먼저 막혀 닿지 못한다.

    history 도 같이 되돌린다. _to_payload 는 카드 분기에서도 history 를 먼저 갱신하므로, 티켓만
    비우면 **결과 없는 tool call 로 끝난 기록**이 메모리에 남는다. 그걸 실은 채 다음 chat 을 돌리면
    모델이 대화를 거부해 500 이 나고, 그때는 _to_payload 가 안 돌아 history 가 그대로라 무한
    반복이다. 재시작 복원이 지키는 규칙(conversations.py 머리말)과 같은 규칙이다.

    활성 상태로 남기는 경로(awaiting_approval·recovery_required)는 티켓·기록이 있어야 이어갈 수
    있으므로 그대로 둔다.
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
        # 메모리의 승인 티켓과 그 턴의 기록도 함께 버린다(팀원 리뷰 + 자동 리뷰).
        # 셋 다 "DB 가 닫았으면 나머지도 닫는다" 하나다.
        # recovery_required·awaiting_approval 은 활성이라 나중에 interrupt 가 지나간다.
        if failure_status not in RUN_ACTIVE:
            _close_plans(store, run)
            if session is not None:
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
    row, conversation = await _load(conversation_id, user, store)
    session = conversation.session
    await _require_approver(row, user)
    await _require_cluster_access(store, row, user)   # 잠금 검사보다 먼저 — 그 사이에 await 가 있으면 안 된다
    if conversation.lock.locked():
        raise _busy()
    run = _open_run(store, conversation_id)
    async with conversation.lock:
        _bind_run(session, run, user)
        # 재개가 돌면 _to_payload 가 티켓을 지우므로, 저장 실패 안내에 실을 Plan id(승인한 카드만)는 여기서 미리 뽑는다
        plan_ids = _approved_plan_ids(session, body.call_id if body.approved else None)
        # 결정 검사·기록은 server._approve 가 한다 (문자열 detail). 여기서는 코드 객체로 감싼다.
        try:
            with _with_cluster(store, session, row):
                outcome, result = await _server._approve(session, body.call_id, body.approved)
        except CLUSTER_FAILURES as exc:
            raise _record_failure(store, run, _cluster_error(exc, row.cluster_id)) from None
        except HTTPException as exc:
            raise _record_failure(store, run, _wrap(exc))
        return _continue_run(store, conversation, run, outcome, result, plan_ids)


@router.post("/{conversation_id}/resume")
async def resume(
    conversation_id: str,
    user: User = Depends(current_user),
    store: ChatStore = Depends(get_store),
) -> dict[str, Any]:
    row, conversation = await _load(conversation_id, user, store)
    session = conversation.session
    await _require_approver(row, user)
    await _require_cluster_access(store, row, user)   # 잠금 검사보다 먼저 — 그 사이에 await 가 있으면 안 된다
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
            with _with_cluster(store, session, row):
                outcome, result = await _server._resume(session)
        except CLUSTER_FAILURES as exc:
            raise _record_failure(store, run, _cluster_error(exc, row.cluster_id)) from None
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
