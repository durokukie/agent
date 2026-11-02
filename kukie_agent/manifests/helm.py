"""Helm 렌더링 — 전용 파드(helm-render)에 맡긴다 (DURO-137). 이 프로세스에서 helm 을 돌리지 않는다.

에이전트는 모든 고객이 함께 쓰는 한 대이고, 대화 잠금과 승인 대기가 프로세스 메모리에 있다. 나쁜 차트로 여기가 재시작되면 전체 대화가
끊긴다. 그래서 render() 는 예외를 내지 않는다 — 파드가 죽었거나 바빠도 "렌더링 실패" 결과로 돌려주고 대화는 이어진다.

조건은 Argo CD 가 쓰는 값 그대로다. reposerver/repository/repository.go(v3.5.3) 의 helmTemplate 을 따른다:
  릴리스 이름 helm.releaseName → Application 이름, 네임스페이스 helm.namespace → destination.namespace,
  Kubernetes 버전 helm.kubeVersion → 클러스터 버전, valueFiles 순서 그대로 + 인라인 values 를 마지막에.
같은 결과를 낼 수 없는 Application(--set 칸, 원격 values, glob)은 다른 결과를 내는 대신 UnsupportedApplication 으로 거절한다.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from kukie_agent.manifests import chart_fetch
from kukie_agent.manifests.render import resolve_inside

logger = logging.getLogger(__name__)

RENDER_URL_ENV = "KUKIE_HELM_RENDER_URL"
#: 연결은 금방 끝나야 한다. 읽기는 서비스의 제한 시간(5초)에 여유를 더했다
_TIMEOUT = httpx.Timeout(20.0, connect=2.0)
#: 서비스의 출력 제한(20MB)에 여유를 더했다. 읽는 도중 넘으면 끊는다 — 이 파드의 메모리를 지킨다
_MAX_RESPONSE_BYTES = 21 * 1024 * 1024
_MAX_CHART_BYTES = 16 * 1024 * 1024
_MAX_VALUES_BYTES = 4 * 1024 * 1024
_MAX_ALIASES = 100
_MAX_OBJECTS = 10000
_CHUNK = 64 * 1024
_KUBE_VERSION = re.compile(r"^\s*v?([0-9]+(?:\.[0-9]+)*)")
_GLOB = re.compile(r"[*?\[]")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
#: 서비스 탓인 실패 — 차트나 값을 고쳐도 소용없고, 조금 뒤 다시 하면 될 수 있다
SERVICE_REASONS = frozenset({"not_configured", "unavailable", "busy", "internal", "bad_response"})

#: 호출 끝까지 쓰는 전송 계층. 테스트가 바꿔 끼운다
_transport: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class ValueFile:
    """name 은 표시용이다. 서비스는 이것을 경로로 쓰지 않는다."""

    name: str
    content: str


@dataclass(frozen=True)
class RenderConditions:
    release_name: str
    kube_version: str
    namespace: str | None = None
    api_versions: tuple[str, ...] = ()
    skip_crds: bool = False
    skip_schema_validation: bool = False
    skip_tests: bool = False


@dataclass(frozen=True)
class HelmRendered:
    """성공이면 manifest, 실패면 reason 과 message. 실패해도 예외가 아니다."""

    manifest: str | None = None
    reason: str | None = None
    message: str = ""
    helm_version: str = ""

    @property
    def ok(self) -> bool:
        return self.reason is None

    @property
    def service_failed(self) -> bool:
        """차트 문제가 아니라 렌더링 서비스 쪽 실패인지. 이때는 "못 바꾼다"가 아니라 "렌더링 실패" 라고 말해야 한다."""
        return self.reason in SERVICE_REASONS


@dataclass(frozen=True)
class FetchedChart:
    archive: bytes | None = None
    reason: str | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.reason is None


class UnsupportedApplication(Exception):
    """Argo CD 와 같은 결과를 낼 수 없는 Application. 문구는 사용자에게 그대로 보여 준다."""


def _service_url() -> str | None:
    url = os.environ.get(RENDER_URL_ENV, "").strip().rstrip("/")
    return url or None


def _read_limited(response: httpx.Response, limit: int) -> bytes | None:
    data = bytearray()
    for chunk in response.iter_bytes(_CHUNK):
        data.extend(chunk)
        if len(data) > limit:
            return None
    return bytes(data)


def _post(path: str, payload: dict[str, Any], timeout: httpx.Timeout) -> tuple[int, dict[str, str], bytes] | str:
    """서비스에 보낸다. (상태, 머리, 본문) 또는 실패 이유 한 단어. 어떤 연결 실패도 예외로 나가지 않는다."""
    url = _service_url()
    if url is None:
        return "not_configured"
    try:
        with httpx.Client(timeout=timeout, transport=_transport, follow_redirects=False) as client:
            with client.stream("POST", f"{url}{path}", json=payload) as response:
                body = _read_limited(response, _MAX_RESPONSE_BYTES)
                if body is None:
                    return "bad_response"
                return response.status_code, dict(response.headers), body
    except httpx.HTTPError as exc:
        # 파드가 죽는 중이면 연결 거부 · 끊김 · 시간 초과로 온다. 값이 섞이지 않게 종류만 남긴다
        logger.warning("helm-render 호출 실패: %s", type(exc).__name__)
        return "unavailable"


_SERVICE_MESSAGES = {
    "not_configured": "렌더링 서비스 주소가 설정되지 않았어요",
    "unavailable": "렌더링 서비스에 닿지 못했어요. 잠시 뒤 다시 시도해 주세요",
    "busy": "렌더링 서비스가 바빠요. 잠시 뒤 다시 시도해 주세요",
    "internal": "렌더링 서비스 내부 오류예요",
    "bad_response": "렌더링 서비스의 답을 읽지 못했어요",
}


def _failure(status: int, body: bytes) -> tuple[str, str]:
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("reason"), str):
        reason = data["reason"]
        return reason, _SERVICE_MESSAGES.get(reason) or str(data.get("message") or "")
    # 파드 앞단이 낸 502 · 503 처럼 서비스가 답한 것이 아닐 때
    reason = "unavailable" if status >= 500 else "bad_response"
    return reason, _SERVICE_MESSAGES[reason]


def render(chart: bytes, values: list[ValueFile], conditions: RenderConditions) -> HelmRendered:
    """렌더링 파드를 불러 결과를 돌려준다. 예외를 내지 않는다. 블로킹이므로 스레드에서 부른다 (kubectl.thread_limiter)."""
    payload = {
        "chart_tgz": base64.b64encode(chart).decode("ascii"),
        "value_files": [{"name": f.name, "content": f.content} for f in values],
        "release_name": conditions.release_name,
        "namespace": conditions.namespace,
        "kube_version": conditions.kube_version,
        "api_versions": list(conditions.api_versions),
        "skip_crds": conditions.skip_crds,
        "skip_schema_validation": conditions.skip_schema_validation,
        "skip_tests": conditions.skip_tests,
    }
    result = _post("/render", payload, _TIMEOUT)
    if isinstance(result, str):
        return HelmRendered(reason=result, message=_SERVICE_MESSAGES[result])
    status, headers, body = result
    if status == 200:
        try:
            return HelmRendered(manifest=body.decode("utf-8"), helm_version=headers.get("x-helm-version", ""))
        except UnicodeDecodeError:
            return HelmRendered(reason="bad_response", message=_SERVICE_MESSAGES["bad_response"])
    reason, message = _failure(status, body)
    logger.info("helm-render 실패: %s", reason)        # message 에는 고객 값이 들어 있을 수 있다 — 로그에 남기지 않는다
    return HelmRendered(reason=reason, message=message)


def fetch_chart(repo_url: str, chart: str, version: str) -> FetchedChart:
    """저장소에서 차트를 받는다. 렌더링 파드는 나가는 길이 막혀 있어 이 파드가 받는다 — 막는 장치는 chart_fetch 에 있다.

    render() 처럼 예외를 내지 않는다. 블로킹이므로 스레드에서 부른다.
    """
    try:
        return FetchedChart(archive=chart_fetch.fetch_chart(repo_url, chart, version))
    except chart_fetch.FetchFailed as exc:
        return FetchedChart(reason=exc.reason, message=exc.message)


def pack_chart(folder: Path) -> bytes:
    """레포 안 차트 폴더를 tgz 로. 링크는 따라가지 않고 링크인 채 담는다 — 받는 쪽이 거절한다. .git 은 뺀다."""
    buffer = io.BytesIO()
    size = 0

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal size
        if ".git" in Path(info.name).parts:
            return None
        size += info.size
        if size > _MAX_CHART_BYTES * 4:
            raise UnsupportedApplication("차트 폴더가 너무 커요")
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(folder, arcname=folder.name or "chart", recursive=True, filter=keep)
    data = buffer.getvalue()
    if len(data) > _MAX_CHART_BYTES:
        raise UnsupportedApplication("차트 폴더가 너무 커요")
    return data


def helm_source(item: dict[str, Any]) -> dict[str, Any]:
    """Application 에서 차트를 렌더링하는 소스를 고른다. `ref` 만 있는 소스는 values 레포일 뿐이다."""
    spec = item.get("spec") or {}
    sources = spec.get("sources")
    if isinstance(sources, list) and sources:
        found = [s for s in sources if isinstance(s, dict) and (s.get("chart") or s.get("helm") or not s.get("ref"))]
        if len(found) != 1:
            raise UnsupportedApplication("차트를 렌더링하는 소스가 하나가 아니에요")
        return found[0]
    source = spec.get("source")
    if not isinstance(source, dict):
        raise UnsupportedApplication("Application 에 소스가 없어요")
    return source


def normalize_kube_version(version: str) -> str:
    """Argo CD 의 parseKubeVersion 과 같다 — `v1.36.5-eks-577a51e` 를 `1.36.5` 로."""
    match = _KUBE_VERSION.match(version)
    if match is None:
        raise UnsupportedApplication("Kubernetes 버전을 읽지 못했어요")
    return match.group(1)


def conditions_from_application(item: dict[str, Any], *, kube_version: str,
                                api_versions: tuple[str, ...] = ()) -> RenderConditions:
    """Application 의 렌더링 조건. kube_version · api_versions 는 대상 클러스터 값이고, helm 칸에 있으면 그것이 이긴다."""
    spec = item.get("spec") or {}
    helm = helm_source(item).get("helm") or {}
    if helm.get("parameters") or helm.get("fileParameters"):
        raise UnsupportedApplication("helm.parameters 를 쓰는 Application 은 아직 렌더링하지 못해요")
    name = helm.get("releaseName") or (item.get("metadata") or {}).get("name")
    if not name:
        raise UnsupportedApplication("Application 이름이 없어요")
    return RenderConditions(
        release_name=str(name),
        namespace=helm.get("namespace") or (spec.get("destination") or {}).get("namespace") or None,
        kube_version=normalize_kube_version(str(helm.get("kubeVersion") or kube_version)),
        api_versions=tuple(helm.get("apiVersions") or api_versions),
        skip_crds=bool(helm.get("skipCrds")),
        skip_schema_validation=bool(helm.get("skipSchemaValidation")),
        skip_tests=bool(helm.get("skipTests")),
    )


def _inline_values(helm: dict[str, Any]) -> str | None:
    """Argo CD 는 valuesObject 가 있으면 그것을, 없으면 values 를 쓴다."""
    if helm.get("valuesObject"):
        return json.dumps(helm["valuesObject"])
    values = helm.get("values")
    return values if isinstance(values, str) and values.strip() else None


def value_files_from_application(item: dict[str, Any], *, chart_dir: Path, repo_root: Path,
                                 refs: dict[str, Path] | None = None) -> list[ValueFile]:
    """valueFiles 를 Argo CD 와 같은 규칙으로 읽는다. 순서를 유지하고, 인라인 values 를 마지막에 붙인다.

    상대 경로는 차트 폴더, `/` 로 시작하면 레포 루트, `$이름/…` 은 그 ref 소스의 레포 기준이다. 레포 밖이면 거절한다 — 조작한 경로로
    이 파드의 파일을 읽지 못하게 한다 (CVE-2022-24348).
    """
    helm = helm_source(item).get("helm") or {}
    ignore_missing = bool(helm.get("ignoreMissingValueFiles"))
    loaded: list[ValueFile] = []
    seen: set[Path] = set()
    total = 0
    for raw in helm.get("valueFiles") or []:
        raw = str(raw)
        if _SCHEME.match(raw):
            raise UnsupportedApplication(f"원격 values({raw[:80]})는 아직 렌더링하지 못해요")
        if _GLOB.search(raw):
            raise UnsupportedApplication(f"glob values({raw[:80]})는 아직 렌더링하지 못해요")
        root, relative = _value_file_root(raw, chart_dir, repo_root, refs or {})
        path = resolve_inside(root, relative)
        if path is None:
            raise UnsupportedApplication(f"values 경로가 레포 밖을 가리켜요: {raw[:80]}")
        if not path.is_file():
            if ignore_missing:
                continue
            raise UnsupportedApplication(f"values 파일이 없어요: {raw[:80]}")
        if path in seen:                                # Argo CD 도 같은 파일은 처음 자리에 한 번만 쓴다
            continue
        seen.add(path)
        total += path.stat().st_size
        if total > _MAX_VALUES_BYTES:
            raise UnsupportedApplication("values 파일이 너무 커요")
        try:
            loaded.append(ValueFile(raw, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError as exc:
            raise UnsupportedApplication(f"values 파일을 읽지 못했어요: {raw[:80]}") from exc
    inline = _inline_values(helm)
    if inline is not None:
        loaded.append(ValueFile("(인라인 values)", inline))
    return loaded


def _value_file_root(raw: str, chart_dir: Path, repo_root: Path, refs: dict[str, Path]) -> tuple[Path, str]:
    """(경계로 삼을 폴더, 그 안의 상대 경로)."""
    if raw.startswith("$"):
        name, _, rest = raw[1:].partition("/")
        if name not in refs or not rest:
            raise UnsupportedApplication(f"values 경로의 소스를 찾지 못했어요: {raw[:80]}")
        return refs[name], rest
    if "$" in raw:
        raise UnsupportedApplication(f"환경변수가 든 values 경로는 아직 렌더링하지 못해요: {raw[:80]}")
    if raw.startswith("/"):
        return repo_root, raw.lstrip("/")
    return repo_root, (chart_dir.resolve() / raw).relative_to(repo_root.resolve(), walk_up=True).as_posix()


class _RenderedLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """렌더링 결과를 읽는 로더. `- =` 같은 값(kube-prometheus-stack)을 PyYAML 이 태그로 읽어 실패하므로 글자로 받는다."""


_RenderedLoader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: node.value)


def parse_rendered(manifest: str) -> list[dict[str, Any]]:
    """렌더링 결과를 객체 목록으로. 앨리어스를 먼저 세고 크기와 개수를 제한한다 — 이 파드에서 파싱하는 유일한 고객 출력이다.

    제한을 넘거나 못 읽으면 ValueError. 메시지에 값 조각을 싣지 않는다.
    """
    if len(manifest) > _MAX_RESPONSE_BYTES:
        raise ValueError("렌더링 결과가 너무 커요")
    try:
        aliases = sum(isinstance(event, yaml.AliasEvent) for event in yaml.parse(manifest, Loader=_RenderedLoader))
        if aliases > _MAX_ALIASES:
            raise ValueError("렌더링 결과의 앨리어스가 너무 많아요")
        objects = []
        for document in yaml.load_all(manifest, Loader=_RenderedLoader):
            if isinstance(document, dict) and document.get("apiVersion") and document.get("kind"):
                objects.append(document)
                if len(objects) > _MAX_OBJECTS:
                    raise ValueError("렌더링 결과의 객체가 너무 많아요")
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError("렌더링 결과를 YAML 로 읽지 못했어요") from exc
    return objects


__all__ = [
    "FetchedChart", "HelmRendered", "RenderConditions", "UnsupportedApplication", "ValueFile",
    "conditions_from_application", "fetch_chart", "helm_source", "pack_chart", "parse_rendered", "render",
    "value_files_from_application",
]
