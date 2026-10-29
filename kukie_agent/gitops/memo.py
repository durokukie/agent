"""옛 답변 끌림 막기 — 레포 연결이 바뀐 때만 기록 맨 끝에 메모 한 줄 (DURO-135).

설계: Linear 문서 "[GitOps] 옛 답변 끌림 막기 — ModelRetry · 숨은 칸 · 메모" 3절.

  숨은 칸   kukie-server 의 대화별 "마지막으로 본 레포 연결 상태" — 코드만 읽는 스위치. 모양 {"repos": ["owner/repo", …]}
  메모      AI 에게만 보이는 쪽지 한 줄. 채팅 화면에는 안 나온다 (앱은 run 의 응답만 그린다)

ProcessHistory 로 AI 를 부를 때마다(툴 호출마다) 돈다. 숨은 칸과 지금 연결 상태가 **다를 때만** 메모를 넣고 숨은 칸을 바꾼다 —
ProcessHistory 가 고친 기록은 ctx.messages 를 덮어써 그 run 의 new_messages() 와 함께 server 의 agent_messages 로 저장되므로,
"끊겨 있으면 메모" 로 짜면 질문마다 쌓인다. 같으면 아무것도 안 한다 (예전 메모는 이미 기록에 있다).

메모는 맨 끝 요청(새 질문이나 툴 결과가 든 ModelRequest)의 맨 앞에 넣는다.
  - 프롬프트 캐시: instructions·툴 목록·앞의 기록은 그대로다. 연결 상태를 instructions 에 넣지도, 툴을 숨기지도 않는다
  - 저장: 맨 끝 요청은 이번 run 에서 생긴 것이라 new_messages() 에 잡힌다 (기록 중간에 넣으면 저장에서 빠진다)
SystemPromptPart 가 아니라 UserPromptPart 다 — 공급자 어댑터가 system 조각을 시스템 프롬프트로 모으면 캐시가 깨진다.
화면 블록이 "이번 턴" 을 찾을 때 메모를 새 질문으로 보지 않게 MARK 로 시작한다 (response._current_turn).

숨은 칸은 **run 의 기록이 server 에 저장된 뒤에** 쓴다 (settle — conversations_api._save_or_fail). 처리기에서 바로 쓰면 run 이 실패해
메모가 든 기록이 버려졌을 때 칸만 앞서 가서, 다음 run 이 "같다" 고 보고 메모를 영영 넣지 않는다 (agent#127 리뷰).
그래서 처리기는 쓸 값을 run 별로 맡겨 두고(_pending), 같은 run 의 다음 요청은 기록에 이미 든 같은 메모를 보고 다시 넣지 않는다.
저장이 실패하면 맡긴 값을 버린다 — 다음 run 이 다시 비교해 메모를 다시 넣는다.

어떤 실패도 올리지 않는다 — 메모는 길 강제(ModelRetry)를 돕는 것이지 대신하지 않는다.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from kukie_agent import server_api
from kukie_agent.deps import Deps

logger = logging.getLogger(__name__)

#: 메모 표식. 맨 앞의 사용 영역 문자(U+E000)는 사용자가 보낸 글에서 지운다(server._chat_turn → strip_mark) — 사용자가
#: "[Kukie 알림]" 으로 시작하는 글을 보내도 메모로 읽혀 턴 경계가 흔들리지 않게 (agent#127 리뷰)
MARK_CHAR = "\ue000"
MARK = f"{MARK_CHAR}[Kukie 알림]"
#: run id → 저장이 끝나면 숨은 칸에 쓸 값
_pending: dict[str, dict[str, Any]] = {}


def strip_mark(text: str) -> str:
    """사용자 입력에서 표식 문자를 지운다 — 메모를 흉내 내지 못하게."""
    return text.replace(MARK_CHAR, "")


def is_memo(part: Any) -> bool:
    return isinstance(part, UserPromptPart) and isinstance(part.content, str) and part.content.startswith(MARK)


def memo_text(before: list[str], now: list[str]) -> str:
    """바뀐 레포마다 한 문장. 앞의 답변은 그 전 기준이라는 것을 같이 말한다."""
    lines: list[str] = []
    for repo in sorted(set(before) - set(now)):
        lines.append(f"레포 {repo} 연결이 끊겼다 — 그 레포의 ArgoCD 관리 리소스는 이제 PR 이 아니라 kubectl 길이다.")
    for repo in sorted(set(now) - set(before)):
        lines.append(f"레포 {repo} 가 새로 연결됐다 — 그 레포의 ArgoCD 관리 리소스는 이제 propose_git_change 로 PR 을 올리는 길이다.")
    return f"{MARK} {' '.join(lines)} 앞의 답변은 그 전 기준이다. 길은 툴이 지금 상태로 다시 판단한다."


def _last_memo(messages: list[ModelMessage]) -> str | None:
    for message in reversed(messages):
        for part in getattr(message, "parts", ()):
            if is_memo(part):
                return part.content
    return None


async def repo_link_memo(ctx: RunContext[Deps], messages: list[ModelMessage]) -> list[ModelMessage]:
    deps = ctx.deps
    if deps.conversation_id is None or deps.cluster_id is None or deps.run_id is None or not messages:
        return messages                          # flat 엔드포인트 — 레포 연결이 없다
    last = messages[-1]
    if not isinstance(last, ModelRequest):
        return messages
    try:
        now = sorted({repo.repository for repo in await server_api.manifest_repos(deps.cluster_id)})
        seen = await server_api.repo_link_state(deps.conversation_id)
    except Exception:
        logger.exception("레포 연결 상태를 읽지 못해 메모를 건너뛴다: conversation=%s", deps.conversation_id)
        return messages
    state = {"repos": now}
    if seen == state or _pending.get(deps.run_id) == state:
        return messages        # 같다, 또는 이 run 의 앞 요청이 이미 판단했다(칸은 저장 뒤에 바뀐다)
    _pending[deps.run_id] = state
    # 처음 보는 대화(칸이 빔)는 칸만 채운다 — 새 대화든, 이 기능 전부터 있던 대화든 "그 전" 을 모르니 바뀌었다고 말할 수 없다
    before = seen.get("repos") if seen is not None else None
    if not isinstance(before, list) or set(before) == set(now):
        return messages        # 순서·중복만 다르면 알릴 것이 없다
    text = memo_text(before, now)
    if _last_memo(messages) == text:
        return messages        # 이 run 의 앞 요청이 넣었거나, 지난 run 이 넣고 칸 쓰기만 실패했다 — 한 줄이면 된다
    memo = UserPromptPart(content=text)
    return [*messages[:-1], dataclasses.replace(last, parts=[memo, *last.parts])]


async def settle(deps: Deps, *, saved: bool) -> None:
    """run 의 기록 저장이 끝났다(saved) — 맡겨 둔 값을 숨은 칸에 쓴다. 저장이 실패했으면 버린다. 실패해도 올리지 않는다."""
    if deps.run_id is None:
        return
    state = _pending.pop(deps.run_id, None)
    if not saved or state is None or deps.conversation_id is None:
        return
    try:
        await server_api.update_repo_link_state(deps.conversation_id, state)
    except Exception:
        # 다음 run 이 다시 비교한다. 메모는 기록에 이미 있으니 같은 메모를 또 넣지 않는다 (_last_memo)
        logger.exception("숨은 칸을 쓰지 못했다: conversation=%s", deps.conversation_id)


capability = ProcessHistory(repo_link_memo)
