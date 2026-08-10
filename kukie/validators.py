"""출력 검증 — "배우면서" 강제 훅 (기능 1 §4.1). 서비스 정체성.

명령을 다뤘는데 설명이 비면 ModelRetry로 반려 → 모델이 스스로 고쳐 재작성.
학습 설명 OFF여도 필드는 항상 채움 — 표시 깊이만 조절 (검증기 단순화).
"""
from __future__ import annotations

from pydantic_ai import ModelRetry, RunContext

from kukie.deps import Deps
from kukie.skills.base import KukieResponse


def enforce_explanations(ctx: RunContext[Deps], output: KukieResponse) -> KukieResponse:
    """@agent.output_validator 로 등록.

    - steps에 명령이 있는데 explanations가 비면 → ModelRetry
    - 명령에 등장한 플래그(-, -- 토큰)가 설명에 다 있는지 점검 → 누락 시 ModelRetry
    """
    raise NotImplementedError  # TODO
