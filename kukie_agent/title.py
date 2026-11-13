"""첫 마디로 대화 제목을 짓는다 — 목록에 "마지막 sync 실패..." 처럼 잘린 첫 문장 대신 짧은 이름이 보이게.

채팅 답과 **동시에** 작은 모델을 한 번 부르고, 답을 저장할 때 run 기록과 함께 `title` 로 보낸다.
쓸지 말지(첫 마디인지, 아직 "새 대화" 인지, 사용자가 바꾼 이름이 아닌지)는 server 가 정한다.
실패·시간 초과·빈 답이면 None — server 가 지금처럼 첫 마디를 줄여 제목으로 쓴다. 채팅은 막지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import os

from pydantic_ai import Agent

from kukie_agent import prompts
from kukie_agent.guardrail.decision_guidance import GUIDANCE_MODEL

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "새 대화"   # server ChatSession.DEFAULT_TITLE 과 같은 값

# 비우면 판단 가이드와 같은 모델 — 키를 한 벌만 쓰게. 빈 값은 미설정과 같게 (decision_guidance 와 같은 이유)
TITLE_MODEL = os.environ.get("KUKIE_TITLE_MODEL") or GUIDANCE_MODEL

# 답보다 늦게 오면 답 저장을 이만큼만 기다린다. 넘으면 첫 마디 제목으로 간다
TIMEOUT_SECONDS = 8.0

title_agent = Agent(
    TITLE_MODEL,
    name="conversation-title",
    output_type=str,
    instructions=prompts.load("conversation_title"),
    # 출력 토큰 상한은 두지 않는다 — 추론 모델은 상한을 추론에 다 써서 빈 답이 된다 (kukie-infra #271)
    model_settings={"openai_reasoning_effort": "low"},
    defer_model_check=True,
)


async def suggest(text: str) -> str | None:
    """제목 후보. 모델이 실패하면 None (로그만)."""
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            result = await title_agent.run(text)
    except Exception as exc:
        logger.warning("대화 제목을 짓지 못했다 — 첫 마디로 짓는다 (%s)", type(exc).__name__)
        return None
    return " ".join(result.output.split()) or None


def wanted(current_title: str, text: str) -> bool:
    """모델을 부를 만한가. 이미 이름이 있거나 옛 앱의 /mode 입력이면 부르지 않는다 (마지막 판단은 server)."""
    return current_title == DEFAULT_TITLE and bool(text.strip()) and not text.startswith("/mode")
