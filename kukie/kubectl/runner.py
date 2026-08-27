"""subprocess 기반 kubectl 실행기."""
from __future__ import annotations

import shlex
import subprocess

from pydantic import BaseModel


class KubectlResult(BaseModel):
    command: str      # 실제 실행된 명령 (표시·기록용 문자열)
    stdout: str
    stderr: str
    success: bool
    exit_code: int | None = None


def run_kubectl(args: list[str], *, context: str, dry_run: bool = False,
                stdin: str | None = None, timeout: int = 30) -> KubectlResult:
    """조립된 args를 실행한다.

    - shell=False (기본값) + 리스트 인자 — 명령어 인젝션 차단.
    - context를 매 호출 명시 — 엉뚱한 클러스터 접근 방지.
    - dry_run=True면 --dry-run=server만 부착해 객체 YAML이 아닌 안전한 요약을 받는다.
    - stdin: `apply -f -` 처럼 표준입력으로 본문(매니페스트)을 넘길 때. 임시파일을 만들지 않아
      조립(assemble)이 순수 함수로 유지된다 — 승인 화면의 명령과 실행 명령이 항상 동일.
    """
    full = ["kubectl", "--context", context, *args]
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
