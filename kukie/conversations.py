"""채팅방별 세션 레지스트리 — server.py 의 "프로세스당 세션 하나" 를 채팅방 단위로 넓힌다.

메모리에 있는 것: 대화 기록(history), 승인 대기 티켓(pending), 결정(decisions), 잠금(lock).
DB 에 있는 것: 채팅방 행, run 행(입력·응답·이 run 의 ModelMessage 들).

복원 (DB 문서 4절): 서버가 다시 뜨면 완료된 run 들의 agent_messages 를 순서대로 이어 붙인다. 범위는
최근 HISTORY_MAX_RUNS 개, 직렬화 크기 HISTORY_MAX_BYTES 까지 (오래된 것부터 버린다).
pending 은 복원하지 않는다 (승인 카드는 만료 — 문서 5절 "재시작 후 자동 재개하는 구조는 아니다"). 그래서
복원 시점에 아직 활성인 run 은 interrupted 로 닫고 그 메시지는 버린다 — 승인 카드 run 의 마지막
메시지는 결과 없는 tool call 이라, 그대로 이어 붙이면 다음 chat 에서 모델이 대화를 거부한다.

잠금은 채팅방마다 하나다: 같은 방에 run 은 하나, 다른 방은 동시에 돈다 (product-spec "동시 작업").
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter

from kukie.deps import Deps
from kukie.skills import DEFAULT_SKILL, SKILLS
from kukie.store import ChatStore
from kukie.store.chat_store import SessionRow

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
        """메모리에 없으면 DB 에서 채팅방과 run 들을 읽어 history 를 복원한다."""
        live = self._live.get(conversation_id)
        if live is not None:
            return live
        if store.get_session(conversation_id) is None:
            return None
        store.interrupt_active_runs(conversation_id, INTERRUPTED_MESSAGE)
        row = store.get_session(conversation_id)
        assert row is not None
        return self.register(row, _restore_history(store, conversation_id))

    def forget(self, conversation_id: str) -> None:
        self._live.pop(conversation_id, None)

    def clear(self) -> None:
        self._live.clear()


def _restore_history(store: ChatStore, conversation_id: str) -> list[Any]:
    completed = [r for r in store.list_runs(conversation_id) if r.status == "completed" and r.agent_messages]
    chunks: list[list[Any]] = []
    total = 0
    for run in reversed(completed[-HISTORY_MAX_RUNS:]):          # 최신부터 채우고 한도를 넘기면 멈춘다
        size = len(json.dumps(run.agent_messages, ensure_ascii=False))
        if chunks and total + size > HISTORY_MAX_BYTES:
            break
        chunks.append(run.agent_messages)
        total += size
    history: list[Any] = []
    for messages in reversed(chunks):
        history.extend(ModelMessagesTypeAdapter.validate_python(messages))
    return history


registry = ConversationRegistry()
