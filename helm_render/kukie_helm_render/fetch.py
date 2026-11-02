"""차트 내려받기 — 렌더링과 분리한다. helm 을 쓰지 않고 파이썬으로 받아, 내부망에 닿지 않는 것을 코드로 보장한다.

주소는 고객이 정한 값이다. 그래서:
  - https · 443 만, 사용자 정보가 든 주소는 받지 않는다
  - 이름을 풀어 나온 주소가 전부 공인 주소일 때만, 검사한 그 주소로 직접 연결한다 (검사 뒤 이름이 바뀌어도 소용없게)
  - 리다이렉트는 주소마다 같은 검사를 다시 하고 횟수를 제한한다
  - 크기와 전체 시간을 넘으면 읽는 도중 끊는다
인증이 필요한 저장소와 OCI 는 아직 지원하지 않는다. 받기만 하고 풀지 않는다 — 푸는 것은 /render 의 일이다.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import tempfile
import time
from collections.abc import Callable
from typing import IO
from urllib.parse import urljoin, urlsplit

import yaml

from kukie_helm_render.config import Settings

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
CHART_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CHART_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")


class FetchFailed(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def public_address(host: str) -> str:
    """이름을 풀어 공인 주소 하나를 고른다. 하나라도 공인이 아니면 거절한다 — 섞어 두고 내부로 보내는 길을 막는다."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise FetchFailed("fetch_failed", "차트 저장소 이름을 찾지 못했어요") from exc
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        mapped = getattr(address, "ipv4_mapped", None)
        checked = mapped if mapped is not None else address
        if not checked.is_global or checked.is_multicast:
            raise FetchFailed("address_blocked", "내부망 주소로는 차트를 받지 않아요")
        addresses.append(str(address))
    if not addresses:
        raise FetchFailed("fetch_failed", "차트 저장소 이름을 찾지 못했어요")
    return addresses[0]


def checked_url(url: str) -> str:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise FetchFailed("url_rejected", "차트 저장소 주소를 읽지 못했어요") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or port not in (None, 443):
        raise FetchFailed("url_rejected", "차트 저장소는 https 주소만 받아요")
    return url


class _PinnedConnection(http.client.HTTPSConnection):
    """검사한 공인 주소로 연결한다. 인증서는 원래 이름으로 확인한다."""

    def __init__(self, host: str, address: str, timeout: float):
        self._tls = ssl.create_default_context()
        super().__init__(host, 443, timeout=timeout, context=self._tls)
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


Opener = Callable[[str, str, float], tuple[int, dict[str, str], IO[bytes]]]


def _open(url: str, address: str, timeout: float) -> tuple[int, dict[str, str], IO[bytes]]:
    parts = urlsplit(url)
    connection = _PinnedConnection(parts.hostname, address, timeout)
    target = parts.path or "/"
    if parts.query:
        target += f"?{parts.query}"
    connection.request("GET", target, headers={
        "Accept-Encoding": "identity", "Connection": "close", "User-Agent": "kukie-helm-render",
    })
    response = connection.getresponse()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, response


def download(url: str, sink: IO[bytes], *, limit: int, deadline: float, settings: Settings,
             opener: Opener = _open) -> None:
    """url 을 sink 로 받는다. limit 이나 deadline 을 넘으면 읽는 도중 끊는다."""
    for _ in range(settings.max_redirects + 1):
        checked_url(url)
        left = deadline - time.monotonic()
        if left <= 0:
            raise FetchFailed("fetch_timeout", "차트를 받는 시간이 제한을 넘었어요")
        address = public_address(urlsplit(url).hostname)
        try:
            status, headers, body = opener(url, address, left)
            with body:
                if status in _REDIRECTS and headers.get("location"):
                    url = urljoin(url, headers["location"])
                    continue
                if status != 200:
                    raise FetchFailed("fetch_failed", f"차트 저장소가 {status} 로 답했어요")
                received = 0
                while chunk := body.read(_CHUNK):
                    received += len(chunk)
                    if received > limit:
                        raise FetchFailed("fetch_too_large", "받는 파일이 크기 제한을 넘었어요")
                    if time.monotonic() > deadline:
                        raise FetchFailed("fetch_timeout", "차트를 받는 시간이 제한을 넘었어요")
                    sink.write(chunk)
                return
        except (OSError, http.client.HTTPException) as exc:
            logger.info("fetch failed: %s", type(exc).__name__)
            raise FetchFailed("fetch_failed", "차트 저장소에 연결하지 못했어요") from exc
    raise FetchFailed("fetch_failed", "리다이렉트가 너무 많아요")


_ENTRY_KEY = re.compile(r"""^(\s+)(?:"([^"]+)"|'([^']+)'|([^\s:#'"]+)):\s*$""")


def chart_block(index: IO[bytes], chart: str, limit: int) -> bytes | None:
    """index.yaml 의 entries 에서 그 차트 한 덩어리만 줄 단위로 꺼낸다.

    큰 저장소의 index 는 수 MB 이상이라 통째로 파싱하면 메모리를 많이 쓴다(prometheus-community 6.6MB). 없으면 None.
    """
    in_entries = False
    child_indent: int | None = None
    block: list[bytes] = []
    size = 0
    capturing = False
    for raw in index:
        line = raw.rstrip(b"\r\n")
        if not in_entries:
            in_entries = line.rstrip() == b"entries:"
            continue
        stripped = line.lstrip(b" ")
        if not stripped or stripped.startswith(b"#"):
            continue
        indent = len(line) - len(stripped)
        if indent == 0:
            break                                       # entries 가 끝났다
        if child_indent is None:
            child_indent = indent
        is_key = indent == child_indent and not stripped.startswith(b"- ")
        if is_key or indent < child_indent:
            if capturing:
                break                                   # 다음 차트가 시작했다
            match = _ENTRY_KEY.match(line.decode("utf-8", "replace"))
            capturing = bool(match) and chart in match.groups()[1:]
            if capturing:
                block = [b"entries:\n"]
                size = 0
        if capturing:
            size += len(raw)
            if size > limit:
                raise FetchFailed("index_too_large", "차트 저장소 목록이 크기 제한을 넘어요")
            block.append(raw)
    return b"".join(block) if capturing else None


def parse_block(block: bytes, chart: str, settings: Settings) -> list[dict]:
    """앨리어스 수를 먼저 세고 읽는다. 앨리어스를 펼쳐 메모리를 채우는 YAML 을 막는다."""
    try:
        aliases = sum(isinstance(event, yaml.AliasEvent) for event in yaml.parse(block, Loader=_YAML_LOADER))
        if aliases > settings.max_index_aliases:
            raise FetchFailed("index_unreadable", "차트 저장소 목록의 앨리어스가 너무 많아요")
        document = yaml.load(block, Loader=_YAML_LOADER)
    except (yaml.YAMLError, RecursionError) as exc:
        raise FetchFailed("index_unreadable", "차트 저장소 목록을 읽지 못했어요") from exc
    entries = document.get("entries", {}).get(chart) if isinstance(document, dict) else None
    return [entry for entry in entries or [] if isinstance(entry, dict)]


def fetch_chart(repo_url: str, chart: str, version: str, settings: Settings, *, opener: Opener = _open) -> bytes:
    """저장소에서 그 차트 그 버전의 tgz 를 받아 돌려준다. 버전은 정확히 같은 것만 찾는다."""
    deadline = time.monotonic() + settings.fetch_timeout_seconds
    base = checked_url(repo_url.rstrip("/") + "/")
    # index 는 메모리에 올리지 않고 줄 단위로 읽는다
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=settings.work_dir) as index:
        download(urljoin(base, "index.yaml"), index, limit=settings.max_index_bytes, deadline=deadline,
                 settings=settings, opener=opener)
        index.seek(0)
        block = chart_block(index, chart, settings.max_index_block_bytes)
    if block is None:
        raise FetchFailed("chart_not_found", "저장소에서 그 차트를 찾지 못했어요")
    for entry in parse_block(block, chart, settings):
        if str(entry.get("version")) != version:
            continue
        urls = entry.get("urls")
        if not (isinstance(urls, list) and urls and isinstance(urls[0], str)):
            raise FetchFailed("index_unreadable", "차트 저장소 목록에 내려받을 주소가 없어요")
        target = urljoin(base, urls[0])
        with tempfile.SpooledTemporaryFile(max_size=settings.max_chart_bytes, dir=settings.work_dir) as sink:
            download(target, sink, limit=settings.max_chart_bytes, deadline=deadline, settings=settings, opener=opener)
            sink.seek(0)
            data = sink.read()
        digest = entry.get("digest")
        # 숫자로만 된 값은 YAML 이 숫자로 읽는다 — 문자열로 견줘 검사를 비켜 가지 못하게 한다
        if digest not in (None, "") and str(digest) != hashlib.sha256(data).hexdigest():
            raise FetchFailed("digest_mismatch", "받은 차트의 digest 가 저장소 목록과 달라요")
        return data
    raise FetchFailed("version_not_found", "저장소에서 그 버전을 찾지 못했어요")
