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
        if tok == "--context":
            skip_next = True
            continue
        if tok in _SKIP_TOKENS or not tok.startswith("-"):
            continue
        flags.add(tok.split("=", 1)[0])
    return flags


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

        explained = " ".join(e.field for e in step.explanations)
        missing = sorted(f for f in _flags_in(step.command) if f not in explained)
        if missing:
            raise ModelRetry(
                f"step {i} ('{step.command}')의 explanations에 다음 플래그 설명이 빠졌다: "
                f"{', '.join(missing)}. 각 플래그의 의미를 explanations에 추가해라."
            )

    return output
