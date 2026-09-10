"""채팅방별 세션 레지스트리 — server.py 의 "프로세스당 세션 하나" 를 채팅방 단위로 넓힌다.

메모리에 있는 것: 대화 기록(history), 승인 대기 티켓(pending), 결정(decisions), 잠금(lock).
DB 에 있는 것: 채팅방 행, run 행(입력·응답·이 run 의 ModelMessage 들).

복원 (DB 문서 4절): 서버가 다시 뜨면 완료된 run 들의 agent_messages 를 순서대로 이어 붙인다. 범위는
최근 HISTORY_MAX_RUNS 개, 직렬화 크기 HISTORY_MAX_BYTES 까지 (오래된 것부터 버린다).
승인 카드(pending)도 복원한다 (#58). 카드가 DB 표 tbl_action_plan 에 남으므로, 아직 WAITING_APPROVAL 인
계획이 있는 run 은 그 tool call 을 되살려 사용자가 이어서 결정할 수 있다. 자동 재개는 아니다 — 결정은
사람이 다시 누른다 (문서 5절 "재시작 후 자동 재개하는 구조는 아니다").

승인 결정은 메모리에만 있어 여러 장 중 일부만 승인한 상태였다면 전부 다시 묻는다 (아직 아무것도
실행되지 않았으므로 안전하다). **거절은 다르다** — 표에 남으므로 다시 묻지 않고, 거절이 섞인 run 은
아예 되살리지 않고 중단으로 닫는다 (아래 _restore_pending).

되살릴 수 없는 활성 run 은 예전대로 interrupted 로 닫고 그 메시지는 버린다 — 승인 카드 run 의 마지막
메시지는 결과 없는 tool call 이라, 그대로 이어 붙이면 다음 chat 에서 모델이 대화를 거부한다.

잠금은 채팅방마다 하나다: 같은 방에 run 은 하나, 다른 방은 동시에 돈다 (product-spec "동시 작업").
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.tools import DeferredToolRequests

from kukie.deps import Deps
from kukie.skills import DEFAULT_SKILL, SKILLS
from kukie.store import ChatStore
from kukie.store.chat_store import RunRow, SessionRow
from kukie.tools.mutate import MUTATING_TOOLS

logger = logging.getLogger(__name__)

HISTORY_MAX_RUNS = 100
HISTORY_MAX_BYTES = 4 * 1024 * 1024

INTERRUPTED_MESSAGE = "서버가 다시 떠서 이 요청은 중단됐다. 승인 카드는 만료됐다"


@dataclass
class Conversation:
    id: str
    session: Any                                   # server.Session — 순환 import 을 피하려 Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def running(self) -> bool:
        return self.lock.locked()


class ConversationRegistry:
    def __init__(self) -> None:
        self._live: dict[str, Conversation] = {}

    def register(self, row: SessionRow, history: list[Any] | None = None) -> Conversation:
        from kukie.server import Session   # 순환 import (server → conversations_api → 여기) 회피

        skill = SKILLS.get(row.current_mode, DEFAULT_SKILL)
        if not row.shared and skill.allowed_tools & MUTATING_TOOLS:
            # private 방은 변경 불가 (기획 05 §3). 게이트가 생기기 전 DB 에 남은 current_mode=실습 행을 그대로 복원하면
            # 카드는 뜨는데 승인은 403 이라 방이 막힌다 — 판정을 입력이 아니라 세션 상태에 건다.
            skill = DEFAULT_SKILL
        session = Session(
            deps=Deps(context=row.context_name, namespace=row.namespace, skill=skill),
            history=list(history or []),
        )
        conversation = Conversation(id=row.id, session=session)
        self._live[row.id] = conversation
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        return self._live.get(conversation_id)

    def get_or_load(self, conversation_id: str, store: ChatStore) -> Conversation | None:
        """메모리에 없으면 DB 에서 채팅방과 run 들을 읽어 history 와 승인 카드를 복원한다."""
        live = self._live.get(conversation_id)
        if live is not None:
            return live
        if store.get_session(conversation_id) is None:
            return None
        active = store.active_run(conversation_id)
        pending = _restore_pending(store, active) if active is not None else None
        if pending is None:
            _sync_plan_files(store.interrupt_active_runs(conversation_id, INTERRUPTED_MESSAGE).plans)
        row = store.get_session(conversation_id)
        assert row is not None
        conversation = self.register(
            row, _restore_history(store, conversation_id, carry=active if pending is not None else None)
        )
        if pending is not None:
            # 카드를 되살렸으면 그 run 의 메시지까지 history 에 있어야 재개가 이어붙일 자리를 찾는다
            conversation.session.pending = pending
        return conversation

    def forget(self, conversation_id: str) -> None:
        self._live.pop(conversation_id, None)

    def clear(self) -> None:
        self._live.clear()


def _unanswered_calls(messages: list[Any]) -> dict[str, ToolCallPart]:
    """결과가 아직 안 붙은 tool call 들. 승인 카드가 그대로 남아 있다는 뜻이다.

    ToolReturnPart(정상 결과)와 RetryPromptPart(모델에게 다시 시키는 응답) 둘 다 "답" 으로 센다.
    """
    calls: dict[str, ToolCallPart] = {}
    answered: set[str] = set()
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolCallPart) and part.tool_call_id:
                calls[part.tool_call_id] = part
            elif isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id:
                answered.add(part.tool_call_id)
    return {cid: call for cid, call in calls.items() if cid not in answered}


def _sync_plan_files(closed: list[Any]) -> None:
    """방금 닫힌 계획의 .md 사본을 따라오게 한다. 표가 원본이고 사본은 사람이 읽는 용도다.

    **방금 닫힌 행만** 받는다. 그 run 의 닫힌 계획을 전부 다시 쓰면 이미 끝난 계획(APPLIED)의
    사본까지 덧써 applied_at 같은 값이 다시 만들어진다 (자동 리뷰 지적).
    """
    from kukie.guardrail.action_plan import sync_markdown   # 순환 import 회피

    try:
        sync_markdown(closed)
    except Exception:
        logger.exception("만료된 계획의 .md 갱신 실패")


def _restore_pending(store: ChatStore, run: RunRow) -> DeferredToolRequests | None:
    """아직 결정되지 않은 승인 카드를 DB 에서 되살린다 (#58). 되살릴 수 없으면 None — 부르는 쪽이 run 을 닫는다.

    되살리는 조건은 하나다: **답 없는 tool call 집합이 대기 카드(WAITING_APPROVAL) 집합과 정확히 같아야 한다.**

    그래서 다음이 전부 걸러진다.
      - 승인까지 갔던 계획(APPROVED/EXECUTING) — kubectl 이 돌았는지 모른다
      - **이미 거절한 계획(REJECTED)** — 표에서는 닫혔지만 그 tool call 은 기록에 답 없이 남아,
        남은 카드만 되살려 재개하면 그 call 이 답 없이 모델에 가서 재개가 영원히 실패한다
    걸러지면 예전대로 run 을 중단으로 닫고 계획은 STALE / UNKNOWN 이 된다.
    """
    if run.status != "awaiting_approval" or not run.agent_messages:
        return None
    try:
        plans = store.list_plans_for_run(run.id)
        waiting = {p.tool_call_id: p for p in plans if p.status == "WAITING_APPROVAL"}
        if not waiting:
            return None
        messages = ModelMessagesTypeAdapter.validate_python(run.agent_messages)
        unanswered = _unanswered_calls(messages)

        # 되살릴 수 있는 건 **답 없는 tool call 집합이 대기 카드와 정확히 같을 때뿐**이다.
        # 한 장을 이미 거절했다면 그 계획은 표에서 닫히지만(REJECTED) 그 tool call 은 기록에 답 없이
        # 남는다. 남은 한 장만 되살려 재개하면 답 없는 call 이 모델에 그대로 가서 재개가 영원히
        # 실패하고, 그 방은 새 대화 말고는 빠져나갈 길이 없다 (자동 리뷰 지적).
        if set(unanswered) != set(waiting):
            return None            # 예전대로 interrupt → 계획은 STALE / UNKNOWN 으로 닫힌다
        return DeferredToolRequests(
            approvals=list(unanswered.values()),
            metadata={cid: {"plan_id": waiting[cid].id} for cid in unanswered},
        )
    except Exception:
        logger.exception("승인 카드 복원 실패 — 중단으로 닫는다 (run=%s)", run.id)
        return None


def _restore_history(store: ChatStore, conversation_id: str, carry: RunRow | None = None) -> list[Any]:
    """완료된 run 들의 메시지를 순서대로 이어 붙인다.

    carry 는 승인 카드를 되살린 대기 중 run 이다. 재개가 이어붙일 자리를 찾으려면 history 가 그 run 의
    tool call 로 끝나야 하므로 **한도와 무관하게 항상 맨 뒤에 붙이고**, 남은 예산으로 과거 run 을 채운다.
    """
    completed = [r for r in store.list_runs(conversation_id) if r.status == "completed" and r.agent_messages]
    tail = carry.agent_messages if carry is not None and carry.agent_messages else None
    chunks: list[list[Any]] = []
    total = _size(tail) if tail is not None else 0
    for run in reversed(completed[-HISTORY_MAX_RUNS:]):          # 최신부터 채우고 한도를 넘기면 멈춘다
        size = _size(run.agent_messages)
        if total + size > HISTORY_MAX_BYTES:
            break                                                 # 최신 run 하나가 한도를 넘어도 복원하지 않는다 — 부분 복원은 없다
        chunks.append(run.agent_messages)
        total += size
    history: list[Any] = []
    for messages in reversed(chunks):
        history.extend(ModelMessagesTypeAdapter.validate_python(messages))
    if tail is not None:
        history.extend(ModelMessagesTypeAdapter.validate_python(tail))
    return history


def _size(messages: Any) -> int:
    """직렬화 바이트 수 — 문자 수가 아니라 바이트로 센다 (한글은 한 글자가 3바이트)."""
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


registry = ConversationRegistry()
