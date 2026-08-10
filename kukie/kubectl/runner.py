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


def run_kubectl(args: list[str], *, context: str, dry_run: bool = False,
                timeout: int = 30) -> KubectlResult:
    """조립된 args를 실행한다.

    - shell=False (기본값) + 리스트 인자 — 명령어 인젝션 차단.
    - context를 매 호출 명시 — 엉뚱한 클러스터 접근 방지.
    - dry_run=True면 --dry-run=server 부착 (가드레일 ⑤단계).
    """
    full = ["kubectl", "--context", context, *args]
    if dry_run:
        full += ["--dry-run=server", "-o", "yaml"]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return KubectlResult(
        command=shlex.join(full),
        stdout=proc.stdout,
        stderr=proc.stderr,
        success=proc.returncode == 0,
    )
