"""변경 툴 4종 — kubectl 변경은 이 함수들을 통해서만 실행된다.

팀 결정 (가드레일 v2):
- 위험도 판별 시스템(룰 엔진/판별 툴) 없음. 위험도는 함수에 미리 붙인 등급이 전부.
- LLM이 호출하는 Action Plan 툴(조회/평가)은 MVP 제외 — Action Plan은
  훅이 내부적으로 생성·기록한다.
- intent/expected_effects/side_effects는 필수 인자 — kubectl 명령에는 안 들어가고,
  승인 화면과 Action Plan 본문("왜")에 실린다. LLM이 툴을 호출하는 행위 자체가
  결재 문서 제출이 되게 하는 설계.
- 실제 실행 흐름은 guardrail/hook.py 파이프라인이 감싼다.
  툴 본체는 훅이 조립한 args를 실행만 한다 (재조립 금지).
"""
from __future__ import annotations

from enum import IntEnum

from pydantic_ai import RunContext

from kukie.deps import Deps
from kukie.kubectl import KubectlResult


class Risk(IntEnum):
    """위험도 등급 — 승인 횟수를 결정한다."""
    SAFE = 0         # 승인 없음 (읽기 툴)
    CAUTION = 1      # 1회 확인
    DESTRUCTIVE = 2  # 이중 확인


def apply_manifest(ctx: RunContext[Deps], manifest_yaml: str,
                   intent: str, expected_effects: list[str],
                   side_effects: list[str]) -> KubectlResult:
    """매니페스트를 클러스터에 적용한다 (kubectl apply).

    리소스 생성·수정은 대부분 이 툴로 한다 (선언형 — create/expose/label 등은
    매니페스트를 만들어 apply하는 것으로 대체).
    intent(왜)/expected_effects(예상 영향)/side_effects(부작용)는 사용자가
    이해할 수 있는 말로 구체적으로 채운다 — 승인 화면과 기록에 표시된다.
    """
    raise NotImplementedError  # TODO: 훅이 전달한 조립 args 실행

def scale_resource(ctx: RunContext[Deps], kind: str, name: str, replicas: int,
                   namespace: str, intent: str, expected_effects: list[str],
                   side_effects: list[str]) -> KubectlResult:
    """리소스의 레플리카 수를 조정한다 (kubectl scale)."""
    raise NotImplementedError  # TODO

def rollout_restart(ctx: RunContext[Deps], kind: str, name: str, namespace: str,
                    intent: str, expected_effects: list[str],
                    side_effects: list[str]) -> KubectlResult:
    """Deployment 등을 재시작한다 (kubectl rollout restart)."""
    raise NotImplementedError  # TODO

def delete_resource(ctx: RunContext[Deps], kind: str, name: str, namespace: str,
                    intent: str, expected_effects: list[str],
                    side_effects: list[str]) -> KubectlResult:
    """리소스를 삭제한다 (kubectl delete). 이중 승인 대상."""
    raise NotImplementedError  # TODO


MUTATE_TOOLS = [apply_manifest, scale_resource, rollout_restart, delete_resource]

# 위험도 스티커 — 함수를 만들 때 여기 등급을 함께 등록한다.
# 등록 안 된 변경 툴은 훅에서 최고 등급으로 취급 (fail-closed).
RISK_STICKERS: dict[str, Risk] = {
    "apply_manifest": Risk.CAUTION,
    "scale_resource": Risk.CAUTION,
    "rollout_restart": Risk.CAUTION,
    "delete_resource": Risk.DESTRUCTIVE,
}

# 훅이 가로챌 대상 — 함수 리스트에서 파생 (손 관리 금지: 목록 불일치 사고 방지)
MUTATING_TOOLS: frozenset[str] = frozenset(t.__name__ for t in MUTATE_TOOLS)
