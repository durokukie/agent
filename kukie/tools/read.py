"""읽기 툴 5종 — 자동 실행, 승인 불필요.

팀 결정 (가드레일 v2): 자유형 조회 탈출구(run_readonly_kubectl)는 제거 —
LLM이 kubectl args를 직접 조립하는 경로는 0개다. 전용 툴로 안 되는 조회는
"직접 실행할 명령 안내 + 플래그 설명"으로 폴백한다 (프롬프트 규칙).

docstring이 모델의 툴 선택 근거다. 사람에게 설명하듯 명확하게 쓴다.
"""
from __future__ import annotations

from pydantic_ai import RunContext

from kukie.deps import Deps
from kukie.kubectl import KubectlResult, run_kubectl


def _ns(ctx: RunContext[Deps], namespace: str | None) -> list[str]:
    """namespace 인자 조립 — 명시값 > 세션 기본값. 대화만으로 대상을 바꾸지 않는다."""
    return ["-n", namespace or ctx.deps.namespace]


def list_resources(ctx: RunContext[Deps], kind: str,
                   namespace: str | None = None,
                   all_namespaces: bool = False) -> KubectlResult:
    """지정한 종류의 리소스 목록을 조회한다.

    예: "파드 뭐 떠있어?" → kind="pods", "디플로이먼트 보여줘" → kind="deployments".
    전체 현황을 볼 때 가장 먼저 쓰는 툴이다.
    """
    args = ["get", kind]
    if all_namespaces:
        args.append("--all-namespaces")
    else:
        args += _ns(ctx, namespace)
    args += ["-o", "wide"]
    return run_kubectl(args, context=ctx.deps.context)


def describe_resource(ctx: RunContext[Deps], kind: str, name: str,
                      namespace: str | None = None) -> KubectlResult:
    """특정 리소스 하나의 상세 상태를 조회한다 (조건, 이벤트, 설정값 포함).

    "이 파드 왜 안 떠?" 처럼 특정 리소스의 문제를 볼 때 1차로 쓴다.
    쿠버네티스가 보는 리소스의 상태(재시작 횟수, Pending 이유 등)를 보여준다 —
    앱이 찍은 로그가 필요하면 get_logs를 쓴다.
    """
    args = ["describe", kind, name, *_ns(ctx, namespace)]
    return run_kubectl(args, context=ctx.deps.context)


def get_events(ctx: RunContext[Deps], namespace: str | None = None,
               all_namespaces: bool = False) -> KubectlResult:
    """네임스페이스의 이벤트를 시간순으로 조회한다.

    스케줄링 실패, 이미지 풀 실패, 리소스 부족 등 클러스터 차원의 원인을
    찾을 때 쓴다. 파드 하나가 아니라 "이 네임스페이스에서 무슨 일이 있었나"를 본다.
    """
    args = ["get", "events", "--sort-by=.lastTimestamp"]
    if all_namespaces:
        args.append("--all-namespaces")
    else:
        args += _ns(ctx, namespace)
    return run_kubectl(args, context=ctx.deps.context)


def get_logs(ctx: RunContext[Deps], pod: str, namespace: str | None = None,
             container: str | None = None, tail: int = 100,
             previous: bool = False) -> KubectlResult:
    """파드 안의 애플리케이션이 출력한 로그를 조회한다.

    앱 자체 에러(설정 파일 누락, 예외 발생 등)를 확인할 때 쓴다.
    CrashLoopBackOff처럼 컨테이너가 이미 죽어 재시작 중이면 previous=True로
    직전 컨테이너의 로그를 본다. 컨테이너가 여러 개면 container를 지정한다.
    """
    args = ["logs", pod, *_ns(ctx, namespace), f"--tail={tail}"]
    if container:
        args += ["-c", container]
    if previous:
        args.append("--previous")
    return run_kubectl(args, context=ctx.deps.context)


def explain_command(ctx: RunContext[Deps], resource_or_field: str) -> KubectlResult:
    """리소스나 필드의 공식 스키마 설명을 조회한다. 순수 학습 툴 — 클러스터 상태를 바꾸지 않는다.

    "replicas가 뭐예요?", "Deployment의 spec에 뭐가 들어가요?" 같은 개념 질문에 쓴다.
    예: "deployment", "deployment.spec.replicas", "pod.spec.containers".
    이 결과를 연수생 눈높이로 다시 풀어서 설명해라.
    """
    args = ["explain", resource_or_field]
    return run_kubectl(args, context=ctx.deps.context)


READ_TOOLS = [list_resources, describe_resource, get_events, get_logs,
              explain_command]
