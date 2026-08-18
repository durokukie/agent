"""출력 검증 — "배우면서" 강제 훅 (기능 1 §4.1). 서비스 정체성.

명령을 다뤘는데 설명이 비면 ModelRetry로 반려 → 모델이 스스로 고쳐 재작성.
학습 설명 OFF여도 필드는 항상 채움 — 표시 깊이만 조절 (검증기 단순화).

검사는 기계적 최저선이다 (빈 값·플래그 누락). 설명의 "정확성"은 코드가 판정할 수 없고,
그건 사용자 검증(1-1 시나리오, 2-2 후기)의 몫이다.
"""
from __future__ import annotations

import shlex

from pydantic_ai import ModelRetry, RunContext

from kukie.deps import Deps
from kukie.skills.base import KukieResponse

# 설명 대상에서 제외하는 토큰 — 명령의 뼈대라 매번 설명하면 잡음이 된다.
_SKIP_TOKENS = frozenset({"kubectl", "--context"})


def _flags_in(command: str) -> set[str]:
    """명령 문자열에서 설명 대상 플래그를 뽑는다. ('-n', '--tail=100' → '-n', '--tail')

    --context 값(클러스터 이름)은 세션 정보라 설명 대상이 아니므로 그 값도 건너뛴다.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    flags: set[str] = set()
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok == "--context" or tok.startswith("--context="):
            skip_next = tok == "--context"        # 띄어쓰기 형식이면 다음 토큰(값)도 건너뜀
            continue
        if tok in _SKIP_TOKENS or not tok.startswith("-"):
            continue
        flags.add(tok.split("=", 1)[0])
    return flags


def _flags_explained(explanations) -> set[str]:
    """explanations의 field들에서 플래그를 뽑아 집합으로. 부분 문자열 비교를 피하기 위해
    field 하나하나를 토큰화한다 ('-c'가 '--context' 안에 들어 있다고 통과되지 않게)."""
    found: set[str] = set()
    for e in explanations:
        for tok in e.field.split():
            if tok.startswith("-"):
                found.add(tok.split("=", 1)[0])
    return found


def enforce_explanations(ctx: RunContext[Deps], output: KukieResponse) -> KukieResponse:
    """@agent.output_validator 로 등록.

    규칙 1: 명령을 실행한 step인데 explanations가 비면 → ModelRetry
    규칙 2: 명령에 등장한 플래그(-x, --xx)가 explanations의 field 어디에도 없으면 → ModelRetry
    """
    for i, step in enumerate(output.steps, start=1):
        if not step.command.strip():
            continue

        if not step.explanations:
            raise ModelRetry(
                f"step {i} ('{step.command}')에 explanations가 비어 있다. "
                "명령의 각 플래그·필드가 무엇을 하는지 연수생이 이해할 수 있게 채워라."
            )

        missing = sorted(_flags_in(step.command) - _flags_explained(step.explanations))
        if missing:
            raise ModelRetry(
                f"step {i} ('{step.command}')의 explanations에 다음 플래그 설명이 빠졌다: "
                f"{', '.join(missing)}. 각 플래그의 의미를 explanations에 추가해라."
            )

    return output
