"""세션 의존성 — 매 run에 주입되는 값들.

대상 클러스터는 대화가 아니라 여기(세션 시작 시 확정)에서 온다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kukie_agent.skills.base import Skill


@dataclass
class Deps:
    context: str        # kubeconfig context (세션 시작 시 확정, 대화로 변경 금지)
    namespace: str      # 기본 대상 namespace
    skill: "Skill"      # 대화의 공개 범위로 정한 스킬 Private/Shared (프롬프트·툴 결정, 대화 안에서 고정)
    # 아래 둘은 대화 단위 엔드포인트가 매 요청 채워 넣는다 (conversations_api). flat 엔드포인트는 None —
    # Action Plan 을 Kukie API 서버에 넣으려면 어느 run 의 계획인지 알아야 한다 (#58, DURO-113).
    run_id: str | None = None       # 지금 처리 중인 run id (kukie-server tbl_chat_run.id)
    user_id: str | None = None      # 요청·승인한 회원 id (plan.decision 에 남는다)
    # 등록된 클러스터로 실행할 때의 임시 kubeconfig 경로 (기획 04 §8). 요청이 끝나면 파일이 지워진다.
    # None 이면 서버 컴퓨터의 기본 kubeconfig 를 쓴다 — flat 엔드포인트(로컬 개발) 경로다.
    kubeconfig: Path | None = None
