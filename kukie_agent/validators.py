"""출력 검증 — 사전 미등록 플래그 로그 (DURO-44).

explanations 는 코드가 FLAG_GLOSSARY 사전으로 채우므로(response.py) "LLM이 설명을 빠뜨렸나"를
감시하던 검증기는 필요 없다. 남는 역할은 하나 — 실제 실행된 명령에 사전에 없는 토큰이
있었는지 기록하는 것. 반려하지 않는다 (사용자 응답은 그대로 나간다). 이 로그가 사전 보강
목록이 된다.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_unregistered_flags(tool_name: str, command: str, unknown: list[str]) -> None:
    """사전에 없던 플래그를 경고 로그로 남긴다. 비어 있으면 아무것도 하지 않는다."""
    if not unknown:
        return
    logger.warning(
        "FLAG_GLOSSARY 미등록 플래그 — tool=%s flags=%s command=%s",
        tool_name, unknown, command,
    )
