"""CLI 진입점 — kukie chat.

세션 흐름 (Architecture.md §8):
1. kubeconfig에서 현재 context/namespace 읽기
2. 사용자에게 대상 확인 (운영 추정 시 경고)
3. 대화 루프: 입력 → 라우팅 → agent.run(스킬 장착) → 응답 렌더링
"""
from __future__ import annotations

from kukie.agent import agent
from kukie.deps import Deps
from kukie.router import pick_skill


def main() -> None:
    # TODO: context/namespace 확인 + 대상 승인
    # TODO: 대화 루프
    #   skill = pick_skill(user_input, current_skill)
    #   result = agent.run_sync(
    #       user_input,
    #       deps=Deps(context=..., namespace=..., skill=skill),
    #       output_type=skill.output_type,        # 스킬별 응답 스키마
    #       message_history=history,              # 턴 간 연결
    #   )
    #   render(result.output)                     # [모드 라벨] + steps 블록 렌더링
    raise NotImplementedError


if __name__ == "__main__":
    main()
