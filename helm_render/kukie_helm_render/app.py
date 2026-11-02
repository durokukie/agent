"""렌더링 API. 저장하는 것은 없다 — 요청마다 임시 폴더를 만들고 끝나면 지운다.

  POST /render  차트(tgz) + values + 렌더링 조건 → YAML (성공) 또는 {reason, message} (실패)
  GET  /health

차트를 내려받지 않는다. 이 파드는 나가는 길이 전부 막혀 있고(NetworkPolicy), 차트는 에이전트가 받아 넘긴다.

바쁘면 기다리지 않고 바로 429 로 답한다. 요청 본문을 읽기 전에 자리를 먼저 잡아, 넘치는 요청이 메모리를 쓰지 않게 한다.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from kukie_helm_render.chart import ChartRejected, unpack_chart
from kukie_helm_render.config import Settings
from kukie_helm_render.helm import Conditions, helm_env, helm_version, run_template, template_args

logger = logging.getLogger("kukie_helm_render")

_RELEASE = r"^[a-z0-9]([-a-z0-9.]{0,51}[a-z0-9])?$"
_NAMESPACE = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
_KUBE_VERSION = r"^v?[0-9]+\.[0-9]+(\.[0-9]+)?([-+][0-9A-Za-z.+-]+)?$"
_API_VERSION = r"^[A-Za-z0-9][A-Za-z0-9./-]{0,253}$"
_HELM_VERSION_HEADER = "X-Helm-Version"
#: 작은 항목 수백만 개로 파싱 중에 메모리를 채우지 못하게 모델에서 먼저 막는다. value_files 의 실제 제한은 설정이 정한다
_MAX_VALUE_FILE_ITEMS = 256
_MAX_API_VERSIONS = 10000
_STATUS = {
    "busy": 429, "invalid_request": 400, "body_too_large": 413, "chart_too_large": 413, "schema_too_large": 413,
    "output_too_large": 422, "timeout": 504, "internal": 500,
}


class ValueFile(BaseModel):
    """이름은 실패 문구용이다 — 경로로 쓰지 않는다. 그래서 values 경로 조작(CVE-2022-24348)이 여기서는 생기지 않는다."""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(max_length=512)]
    content: str


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_tgz: str
    value_files: Annotated[list[ValueFile], Field(max_length=_MAX_VALUE_FILE_ITEMS)] = []
    release_name: Annotated[str, StringConstraints(pattern=_RELEASE)]
    namespace: Annotated[str, StringConstraints(pattern=_NAMESPACE)] | None = None
    kube_version: Annotated[str, StringConstraints(pattern=_KUBE_VERSION)]
    api_versions: Annotated[list[Annotated[str, StringConstraints(pattern=_API_VERSION)]],
                            Field(max_length=_MAX_API_VERSIONS)] = []
    skip_crds: bool = False
    skip_schema_validation: bool = False
    skip_tests: bool = False


class _Slots:
    """기다리지 않는 자리표. 이벤트 루프 하나에서만 쓰므로 잠금이 필요 없다."""

    def __init__(self, size: int):
        self.left = size

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True

    def give_back(self) -> None:
        self.left += 1


class _BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _BodyTooLarge
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise _BodyTooLarge
    return bytes(chunks)


def _failure(reason: str, message: str) -> JSONResponse:
    return JSONResponse({"reason": reason, "message": message}, status_code=_STATUS.get(reason, 422))


def _invalid(exc: ValidationError) -> JSONResponse:
    # 어느 칸인지만 알려 준다. 입력값은 고객 값일 수 있어 응답과 로그에 싣지 않는다
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors(include_input=False)})
    return _failure("invalid_request", f"요청 값이 맞지 않아요: {', '.join(fields)[:300]}")


def _prepare(body: bytes, settings: Settings, workspace: Path) -> tuple[Path, list[Path], Conditions]:
    """요청을 읽고 차트를 푼다. CPU 를 쓰는 일이라 스레드에서 돈다 — 그동안에도 /health 가 답한다."""
    request = RenderRequest.model_validate_json(body)
    try:
        archive = base64.b64decode(request.chart_tgz, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChartRejected("invalid_request", "차트가 base64 가 아니에요") from exc
    if len(request.value_files) > settings.max_value_files:
        raise ChartRejected("invalid_request", "values 파일이 너무 많아요")
    if sum(len(f.content.encode("utf-8")) for f in request.value_files) > settings.max_values_bytes:
        raise ChartRejected("body_too_large", "values 가 크기 제한을 넘어요")
    chart = unpack_chart(archive, workspace / "chart", settings)
    value_dir = workspace / "values"
    value_dir.mkdir(mode=0o700)
    paths = []
    # 받은 순서가 곧 helm 의 병합 순서다. 이름은 우리가 짓는다
    for index, file in enumerate(request.value_files):
        path = value_dir / f"{index:03d}.yaml"
        path.write_text(file.content, encoding="utf-8")
        paths.append(path)
    conditions = Conditions(
        release_name=request.release_name, namespace=request.namespace, kube_version=request.kube_version,
        api_versions=tuple(request.api_versions), skip_crds=request.skip_crds,
        skip_schema_validation=request.skip_schema_validation, skip_tests=request.skip_tests,
    )
    return chart, paths, conditions


def _clear(work_dir: Path) -> None:
    """앞 컨테이너가 죽으며 남긴 파일을 지운다. emptyDir 은 컨테이너 재시작에도 남고, 고객 values 가 들어 있을 수 있다."""
    for child in work_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    # helm 에는 PATH 를 주지 않는다 (helm_env) — 여기서 절대 경로로 정해 둔다
    settings = replace(settings, helm_bin=shutil.which(settings.helm_bin) or settings.helm_bin)
    render_slots = _Slots(settings.max_concurrent)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        _clear(settings.work_dir)
        home = Path(tempfile.mkdtemp(dir=settings.work_dir))
        app.state.helm_version = await helm_version(settings, home)
        shutil.rmtree(home, ignore_errors=True)
        logger.info("started helm=%s", app.state.helm_version)
        yield

    app = FastAPI(title="kukie-helm-render", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "helmVersion": app.state.helm_version}

    async def _render(request: Request) -> Response:
        started = time.monotonic()
        try:
            body = await _read_body(request, settings.max_body_bytes)
        except _BodyTooLarge:
            return _failure("body_too_large", "요청이 크기 제한을 넘어요")
        workspace = Path(await asyncio.to_thread(tempfile.mkdtemp, dir=settings.work_dir))
        try:
            chart, values, conditions = await asyncio.to_thread(_prepare, body, settings, workspace)
            del body
            rendered = await run_template(template_args(chart, conditions, values),
                                          env=helm_env(workspace / "home", settings), settings=settings)
        except ValidationError as exc:
            logger.info("render rejected reason=invalid_request")
            return _invalid(exc)
        except ChartRejected as exc:
            logger.info("render rejected reason=%s", exc.reason)
            return _failure(exc.reason, exc.message)
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)
        millis = int((time.monotonic() - started) * 1000)
        if not rendered.ok:
            logger.info("render failed reason=%s ms=%d", rendered.reason, millis)
            return _failure(rendered.reason, rendered.message)
        logger.info("render ok ms=%d bytes=%d", millis, len(rendered.output))
        return Response(rendered.output, media_type="application/yaml",
                        headers={_HELM_VERSION_HEADER: app.state.helm_version})

    @app.post("/render")
    async def render(request: Request) -> Response:
        if not render_slots.take():
            logger.info("render rejected reason=busy")
            return JSONResponse({"reason": "busy", "message": "렌더링이 바빠요. 잠시 뒤 다시 시도해 주세요"},
                                status_code=429, headers={"Retry-After": "1"})
        try:
            return await _render(request)
        except Exception as exc:
            # 오류 문구와 트레이스에는 고객 값이 섞일 수 있다 — 종류만 남긴다
            logger.error("render error type=%s", type(exc).__name__)
            return _failure("internal", "렌더링 서비스 내부 오류예요")
        finally:
            render_slots.give_back()

    return app


def _configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("HELM_RENDER_LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")


_configure_logging()
app = create_app()
