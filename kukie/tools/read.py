"""읽기 툴 6종 — 자동 실행, 승인 불필요 (기능 1 §1.1).

docstring이 모델의 툴 선택 근거다. 사람에게 설명하듯 명확하게 쓴다.
"""
from __future__ import annotations

from pydantic_ai import RunContext

from kukie.deps import Deps
from kukie.kubectl import KubectlResult, run_kubectl

# 탈출구 화이트리스트 — verb+서브커맨드 단위 (fail-closed).
# verb 단위로 하면 "rollout status"(읽기)와 "rollout restart"(변경)를 못 가른다.
READONLY_ALLOWED: frozenset[str] = frozenset({
    "get", "describe", "logs", "top", "explain", "api-resources",
    "rollout status", "config view",
})


def list_resources(ctx: RunContext[Deps], kind: str,
                   namespace: str | None = None,
                   all_namespaces: bool = False) -> KubectlResult:
    """지정한 종류의 리소스 목록을 조회한다. 예: "파드 뭐 떠있어?" → kind="pods"."""
    args = ["get", kind]
    if all_namespaces:
        args.append("--all-namespaces")
    else:
        args += ["-n", namespace or ctx.deps.namespace]
    args += ["-o", "wide"]
    return run_kubectl(args, context=ctx.deps.context)


def describe_resource(ctx: RunContext[Deps], kind: str, name: str,
                      namespace: str | None = None) -> KubectlResult:
    """리소스의 상세 상태를 조회한다. 파드가 왜 안 뜨는지 1차 확인에 사용."""
    raise NotImplementedError  # TODO


def get_events(ctx: RunContext[Deps], namespace: str | None = None) -> KubectlResult:
    """네임스페이스 이벤트를 조회한다. 스케줄링 실패 등 클러스터 차원 원인 파악용."""
    raise NotImplementedError  # TODO


def get_logs(ctx: RunContext[Deps], pod: str, namespace: str | None = None,
             tail: int = 100) -> KubectlResult:
    """파드의 애플리케이션 로그를 조회한다. 앱 자체 에러 확인용."""
    raise NotImplementedError  # TODO


def explain_command(ctx: RunContext[Deps], resource_or_field: str) -> KubectlResult:
    """리소스/필드의 의미를 설명한다. 순수 학습 툴 — 클러스터 상태를 바꾸지 않는다."""
    raise NotImplementedError  # TODO: kubectl explain + 자체 해설


def run_readonly_kubectl(ctx: RunContext[Deps], args: list[str]) -> KubectlResult:
    """전용 툴로 안 되는 조회의 탈출구. 반드시 읽기 전용.

    전용 툴(get_logs 등)이 있으면 그것을 우선 사용하고, 없는 조회만 이걸 쓴다.
    """
    # fail-closed: verb(+서브커맨드)가 화이트리스트에 없으면 즉시 거부
    head2 = " ".join(args[:2])
    if not args or (args[0] not in READONLY_ALLOWED and head2 not in READONLY_ALLOWED):
        return KubectlResult(command=" ".join(args), stdout="",
                             stderr=f"읽기 전용 탈출구에서 허용되지 않음: {args[:2]}",
                             success=False)
    return run_kubectl(args, context=ctx.deps.context)


READ_TOOLS = [list_resources, describe_resource, get_events, get_logs,
              explain_command, run_readonly_kubectl]
