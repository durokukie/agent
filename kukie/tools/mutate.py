"""변경 툴 4종 — 확장 스키마 (기능 2 §1.2).

intent/expected_effects/side_effects가 필수 인자다:
툴을 호출하는 행위 자체가 Action Plan 재료 제출이 되게 한다.
이 3개 필드는 kubectl 명령에 안 들어간다 — 검증(①)·계획서(④)·승인 화면(⑥) 전용.

실제 실행 흐름은 guardrail/hook.py의 파이프라인이 감싼다.
툴 본체는 훅이 조립·검증한 args를 실행만 한다 (재조립 금지).
"""
from __future__ import annotations

from pydantic_ai import RunContext

from kukie.deps import Deps
from kukie.kubectl import KubectlResult

MUTATING_TOOLS: frozenset[str] = frozenset({
    "apply_manifest", "scale_resource", "rollout_restart", "delete_resource",
})


def apply_manifest(ctx: RunContext[Deps], manifest_yaml: str,
                   intent: str, expected_effects: list[str],
                   side_effects: list[str]) -> KubectlResult:
    """매니페스트를 클러스터에 적용한다 (kubectl apply). caution 등급."""
    raise NotImplementedError  # TODO: 훅이 전달한 조립 args 실행

def scale_resource(ctx: RunContext[Deps], kind: str, name: str, replicas: int,
                   namespace: str, intent: str, expected_effects: list[str],
                   side_effects: list[str]) -> KubectlResult:
    """리소스의 레플리카 수를 조정한다 (kubectl scale). caution 등급."""
    raise NotImplementedError  # TODO

def rollout_restart(ctx: RunContext[Deps], kind: str, name: str, namespace: str,
                    intent: str, expected_effects: list[str],
                    side_effects: list[str]) -> KubectlResult:
    """Deployment 등을 재시작한다 (kubectl rollout restart). caution 등급."""
    raise NotImplementedError  # TODO

def delete_resource(ctx: RunContext[Deps], kind: str, name: str, namespace: str,
                    intent: str, expected_effects: list[str],
                    side_effects: list[str]) -> KubectlResult:
    """리소스를 삭제한다 (kubectl delete). destructive — 이중 승인 대상."""
    raise NotImplementedError  # TODO


MUTATE_TOOLS = [apply_manifest, scale_resource, rollout_restart, delete_resource]
