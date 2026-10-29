"""subprocess 기반 kubectl 실행기."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from anyio import CapacityLimiter
from pydantic import BaseModel, Field

_limiters: dict[str, CapacityLimiter] = {}


def thread_limiter(name: str, workers: int) -> CapacityLimiter:
    """블로킹 일(kubectl·이름 해석)을 스레드로 넘길 때 쓰는 이름별 스레드 상한.

    AnyIO 공용 풀(기본 40)을 통째로 물지 않게 쓰임새마다 따로 묶는다 — kubectl 은 30초씩 잡고, 여기서 다 물면
    채팅의 동기 툴(pydantic-ai 가 같은 풀로 돌린다)까지 함께 멈춘다. asyncio.to_thread 를 쓰지 않는 이유는
    그 기본 풀을 DNS 조회(loop.getaddrinfo)와 나눠 써서, 풀이 차면 새 바깥 연결의 이름 풀이가 줄 서기 때문이다 (PR #97 리뷰).
    처음 쓸 때 만든다. anyio 3.x 는 이벤트 루프 밖에서 만들면 터지므로 import 시점에 두지 않는다.
    """
    found = _limiters.get(name)
    if found is None:
        found = _limiters[name] = CapacityLimiter(workers)
    return found


class KubectlResult(BaseModel):
    command: str      # 실제 실행된 명령 (표시·기록용 문자열)
    stdout: str
    stderr: str
    success: bool
    exit_code: int | None = None
    # kubectl 출력 밖에서 모델에게 같이 알릴 것 (예: ArgoCD 가 관리하는 리소스 — DURO-129). 화면의 출력 칸에는 실리지 않는다.
    # 없으면 칸 자체가 빠진다 — 툴 결과가 예전과 같은 모양이다
    note: str | None = Field(default=None, exclude_if=lambda value: value is None)


def run_kubectl(args: list[str], *, context: str, dry_run: bool = False,
                stdin: str | None = None, timeout: float = 30,
                kubeconfig: "Path | None" = None) -> KubectlResult:
    """조립된 args를 실행한다.

    - shell=False (기본값) + 리스트 인자 — 명령어 인젝션 차단.
    - context를 매 호출 명시 — 엉뚱한 클러스터 접근 방지.
    - dry_run=True면 --dry-run=server만 부착해 객체 YAML이 아닌 안전한 요약을 받는다.
    - stdin: `apply -f -` 처럼 표준입력으로 본문(매니페스트)을 넘길 때. 임시파일을 만들지 않아
      조립(assemble)이 순수 함수로 유지된다 — 승인 화면의 명령과 실행 명령이 항상 동일.
    - kubeconfig: 등록된 클러스터로 실행할 때의 임시 파일 (기획 04 §8). 주지 않으면 예전처럼
      서버 컴퓨터의 기본 kubeconfig 를 쓴다 — 로컬 개발 경로다.
    """
    location = ["--kubeconfig", str(kubeconfig)] if kubeconfig is not None else []
    full = ["kubectl", *location, "--context", context, *args]
    if dry_run:
        full.append("--dry-run=server")
    try:
        proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout,
                              input=stdin)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return KubectlResult(
            command=shlex.join(full),
            stdout="",
            stderr=str(exc),
            success=False,
            exit_code=None,
        )
    return KubectlResult(
        command=shlex.join(full),
        stdout=proc.stdout,
        stderr=proc.stderr,
        success=proc.returncode == 0,
        exit_code=proc.returncode,
    )
