"""채팅방별 세션 레지스트리 — server.py 의 "프로세스당 세션 하나" 를 채팅방 단위로 넓힌다.

메모리에 있는 것: 대화 기록(history), 승인 대기 티켓(pending), 결정(decisions), 잠금(lock).
DB 에 있는 것: 채팅방 행, run 행(입력·응답·이 run 의 ModelMessage 들).
서버가 다시 뜨면 완료된 run 들의 agent_messages 를 이어 붙여 history 를 복원한다. pending 은 복원하지
않는다 (승인 카드는 만료 — MVP 결정, api-spec 1절). 그래서 복원 시점에 아직 활성인 run(실행 중이었거나
승인 카드를 기다리던 것)은 interrupted 로 닫고 그 메시지는 버린다 — 승인 카드 run 의 마지막 메시지는
결과 없는 tool call 이라, 그대로 이어 붙이면 다음 chat 에서 모델이 대화를 거부한다.

잠금은 채팅방마다 하나다: 같은 방에 run 은 하나, 다른 방은 동시에 돈다 (product-spec "동시 작업").
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter

from kukie.deps import Deps
from kukie.skills import DEFAULT_SKILL, SKILLS
from kukie.store import ChatStore
from kukie.store.chat_store import SessionRow


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
        store.settle_active_runs(
            conversation_id, status="interrupted",
            error={"code": "INTERRUPTED", "message": "서버가 다시 떠서 이 요청은 중단됐다. 승인 카드는 만료됐다"},
        )
        row = store.get_session(conversation_id)
        assert row is not None
        history: list[Any] = []
        for run in store.list_runs(conversation_id):
            if run.status == "completed" and run.agent_messages:
                history.extend(ModelMessagesTypeAdapter.validate_python(run.agent_messages))
        return self.register(row, history)

    def forget(self, conversation_id: str) -> None:
        self._live.pop(conversation_id, None)

    def clear(self) -> None:
        self._live.clear()


registry = ConversationRegistry()
