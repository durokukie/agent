"""세션 의존성 — 매 run에 주입되는 값들.

대상 클러스터는 대화가 아니라 여기(세션 시작 시 확정)에서 온다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kukie.skills.base import Skill


@dataclass
class Deps:
    context: str        # kubeconfig context (세션 시작 시 확정, 대화로 변경 금지)
    namespace: str      # 기본 대상 namespace
    skill: "Skill"      # 현재 활성 스킬 (프롬프트·툴·응답 스키마 결정)
    verbose_learning: bool = True   # 학습 설명 ON/OFF (설명 깊이만 조절, 필드는 항상 채움)
    # 아래 둘은 대화 단위 엔드포인트가 매 요청 채워 넣는다 (conversations_api). flat 엔드포인트는 None —
    # Action Plan 을 tbl_action_plan 에 넣으려면 어느 run 의 계획인지 알아야 한다 (#58).
    run_id: str | None = None       # 지금 처리 중인 tbl_chat_run.id
    user_id: str | None = None      # 요청·승인한 회원 id (plan.decision 에 남는다)
    # 등록된 클러스터로 실행할 때의 임시 kubeconfig 경로 (기획 04 §8). 요청이 끝나면 파일이 지워진다.
    # None 이면 서버 컴퓨터의 기본 kubeconfig 를 쓴다 — 로컬 개발 경로다.
    kubeconfig: Path | None = None
