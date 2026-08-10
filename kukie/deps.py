"""세션 의존성 — 매 run에 주입되는 값들.

대상 클러스터는 대화가 아니라 여기(세션 시작 시 확정)에서 온다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kukie.skills.base import Skill


@dataclass
class Deps:
    context: str        # kubeconfig context (세션 시작 시 확정, 대화로 변경 금지)
    namespace: str      # 기본 대상 namespace
    skill: "Skill"      # 현재 활성 스킬 (프롬프트·툴·응답 스키마 결정)
    verbose_learning: bool = True   # 학습 설명 ON/OFF (설명 깊이만 조절, 필드는 항상 채움)
