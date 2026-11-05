"""`helm template` 한 번 실행 — 시간 · 출력 · 메모리를 넘으면 그 프로세스를 끝낸다.

인자는 Argo CD 의 util/helm/cmd.go(template) 와 같은 순서 · 같은 옵션이다. 렌더링 결과가 Argo CD 와 어긋나지 않게 고치지 않는다.
쓰지 않는 것: --enable-dns, --dry-run=server, --post-renderer, --dependency-update, 플러그인.

출력과 stderr 는 로그에 남기지 않는다 — 고객 values 가 들어 있다. 호출한 쪽에만 돌려준다.
"""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from kukie_helm_render.config import Settings

#: 아무도 듣지 않는 주소. Go 의 http 가 이쪽으로 간다 — 스키마 검사(chart.py)의 다음 겹이고, 그 뒤에 NetworkPolicy 가 있다
OFFLINE_PROXY = "http://127.0.0.1:9"
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
_MEMORY_CHECK_SECONDS = 0.05
_READ_CHUNK = 64 * 1024
_REAP_SECONDS = 5
_MISSING_DEPENDENCY = "found in Chart.yaml, but missing in charts/ directory"
_SCHEMA_REJECTED = "values don't meet the specifications of the schema"


@dataclass(frozen=True)
class Conditions:
    """Argo CD Application 에서 온 렌더링 조건."""

    release_name: str
    kube_version: str
    namespace: str | None = None
    api_versions: tuple[str, ...] = ()
    skip_crds: bool = False
    skip_schema_validation: bool = False
    skip_tests: bool = False


@dataclass(frozen=True)
class Rendered:
    output: bytes | None = None
    reason: str | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.reason is None


def template_args(chart: Path, conditions: Conditions, value_paths: list[Path]) -> list[str]:
    args = ["template", str(chart), "--name-template", conditions.release_name]
    if conditions.namespace:
        args += ["--namespace", conditions.namespace]
    if conditions.kube_version:
        args += ["--kube-version", conditions.kube_version]
    for path in value_paths:
        args += ["--values", str(path)]
    for version in conditions.api_versions:
        args += ["--api-versions", version]
    if not conditions.skip_crds:
        args.append("--include-crds")
    if conditions.skip_schema_validation:
        args.append("--skip-schema-validation")
    if conditions.skip_tests:
        args.append("--skip-tests")
    return args


def helm_env(home: Path, settings: Settings) -> dict[str, str]:
    """이 프로세스의 환경을 물려주지 않는다. 플러그인 폴더는 비어 있고 kubeconfig 는 없다."""
    return {
        "HOME": str(home),
        "HELM_CONFIG_HOME": str(home / "config"),
        "HELM_CACHE_HOME": str(home / "cache"),
        "HELM_DATA_HOME": str(home / "data"),
        "HELM_PLUGINS": str(home / "plugins"),
        "KUBECONFIG": os.devnull,
        "HTTP_PROXY": OFFLINE_PROXY,
        "HTTPS_PROXY": OFFLINE_PROXY,
        "NO_PROXY": "",
        "GOMAXPROCS": "1",
        "GOMEMLIMIT": str(settings.max_rss_bytes * 9 // 10),
    }


def _rss_bytes(pid: int) -> int:
    """리눅스에서 그 프로세스의 실제 메모리. /proc 이 없으면(맥) 0 — 파드 메모리 제한만 남는다."""
    try:
        with open(f"/proc/{pid}/statm", "rb") as file:
            return int(file.read().split()[1]) * _PAGE_SIZE
    except (OSError, IndexError, ValueError):
        return 0


class _OutputTooLarge(Exception):
    pass


async def run_template(args: list[str], *, env: dict[str, str], settings: Settings) -> Rendered:
    process = await asyncio.create_subprocess_exec(
        settings.helm_bin, *args, env=env, process_group=0,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = bytearray()
    errors = bytearray()

    async def read_output() -> None:
        while chunk := await process.stdout.read(_READ_CHUNK):
            output.extend(chunk)
            if len(output) > settings.max_output_bytes:
                raise _OutputTooLarge

    async def read_errors() -> None:
        # 다 읽기는 하되 끝부분만 둔다 — 실제 Error 줄은 끝에 온다. 읽지 않으면 helm 이 파이프에 막혀 시간 초과로 보인다
        while chunk := await process.stderr.read(_READ_CHUNK):
            errors.extend(chunk)
            del errors[:-settings.max_failure_text * 4]

    async def watch_memory() -> None:
        while _rss_bytes(process.pid) <= settings.max_rss_bytes:
            await asyncio.sleep(_MEMORY_CHECK_SECONDS)

    readers = [asyncio.ensure_future(read_output()), asyncio.ensure_future(read_errors()),
               asyncio.ensure_future(process.wait())]
    finished = asyncio.gather(*readers)
    memory = asyncio.ensure_future(watch_memory())
    try:
        done, _ = await asyncio.wait({finished, memory}, timeout=settings.timeout_seconds,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            return Rendered(reason="timeout", message=f"렌더링이 {settings.timeout_seconds:g}초 안에 끝나지 않았어요")
        if finished not in done:
            return Rendered(reason="memory_exceeded", message="렌더링이 메모리 제한을 넘었어요")
        if isinstance(finished.exception(), _OutputTooLarge):
            return Rendered(reason="output_too_large", message="렌더링 결과가 크기 제한을 넘었어요")
        if finished.exception() is not None:
            raise finished.exception()
        if process.returncode != 0:
            return _failed(process.returncode, errors, settings)
        return Rendered(output=bytes(output))
    finally:
        # 어떤 길로 나가든 프로세스를 남기지 않는다 (호출이 취소돼도)
        _kill(process)
        for task in (*readers, memory):
            task.cancel()
        await asyncio.gather(*readers, finished, memory, return_exceptions=True)
        # 파이프를 끝까지 비워야 process.wait() 가 돌아온다 — 읽기를 멈춘 채 기다리면 asyncio 가 영영 끝나지 않는다
        await asyncio.wait_for(asyncio.gather(_drain(process.stdout), _drain(process.stderr), process.wait()), _REAP_SECONDS)


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(_READ_CHUNK):
        pass


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _failed(returncode: int, errors: bytearray, settings: Settings) -> Rendered:
    if returncode < 0:
        return Rendered(reason="helm_killed", message="렌더링 프로세스가 강제로 끝났어요")
    text = errors.decode("utf-8", "replace")
    message = text[-settings.max_failure_text:].strip()
    if _MISSING_DEPENDENCY in text:
        return Rendered(reason="missing_dependency", message=message)
    if _SCHEMA_REJECTED in text:
        return Rendered(reason="schema_rejected", message=message)
    return Rendered(reason="helm_failed", message=message)


async def helm_version(settings: Settings, home: Path) -> str:
    """helm 버전. 이미지에 고정한 값을 응답에 함께 돌려준다 — 어느 버전 기준의 결과인지 호출한 쪽이 알 수 있게."""
    process = await asyncio.create_subprocess_exec(
        settings.helm_bin, "version", "--short", env=helm_env(home, settings),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    return out.decode("utf-8", "replace").strip()
