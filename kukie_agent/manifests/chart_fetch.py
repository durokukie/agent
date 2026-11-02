"""차트 내려받기 (DURO-137). 렌더링 파드는 나가는 길이 막혀 있어 이 파드가 받아 넘긴다. 받기만 하고 풀지 않는다.

주소는 고객이 정한 값인데 이 파드에는 접속 정보가 있고 내부망에 닿는다. 그래서 helm 을 쓰지 않고 코드로 막는다:
  - https · 443 만, 사용자 정보가 든 주소는 받지 않는다. 인증 헤더를 보내지 않고 환경의 프록시도 쓰지 않는다
  - 이름을 풀어 나온 주소가 전부 공인 주소일 때만, 검사한 그 주소로 직접 연결한다 (검사 뒤 이름이 바뀌어도 소용없게)
  - 리다이렉트는 주소마다 같은 검사를 다시 하고 횟수를 제한한다
  - 크기와 전체 시간을 넘으면 읽는 도중 끊는다
  - index.yaml 을 YAML 파서에 넣지 않는다. 필요한 칸(version · digest · urls)만 줄 단위로 읽는다. 앨리어스와 깊은 중첩이 뜻이 없어진다 —
    libyaml 은 깊은 중첩에 시간이 제곱으로 늘어(5만 단계 4.8초) 줄 하나로 전체 시간 제한을 넘고 스레드를 붙잡는다. `helm repo index` 가 쓰는 모양만 읽고, 다른 모양은 못 찾은 것으로 끝난다
인증이 필요한 저장소와 OCI 는 아직 지원하지 않는다. 블로킹이므로 스레드에서 부른다 (kubectl.thread_limiter).
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
from collections.abc import Callable, Iterator
from typing import IO
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

MAX_CHART_BYTES = 16 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 30.0
#: 응답 없이 기다리는 한 번의 시간. 전체 시간은 읽을 때마다 본다
_SOCKET_TIMEOUT_SECONDS = 10.0

_CHUNK = 64 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}
_ENTRY_FIELD = re.compile(rb"^(version|digest|urls):(?:\s+(.*))?$")
_ENTRY_KEY = re.compile(r"""^\s+(?:"([^"]+)"|'([^']+)'|([^\s:#'"]+)):\s*$""")
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
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchFailed("url_rejected", "차트 저장소 주소를 읽지 못했어요") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or port not in (None, 443):
        raise FetchFailed("url_rejected", "차트 저장소는 https 주소만 받아요")
    return url


def _joined(base: str, reference: str) -> str:
    """index 나 리다이렉트가 준 주소를 합친다. 고객 값이라 깨진 주소일 수 있다."""
    try:
        return urljoin(base, reference)
    except ValueError as exc:
        raise FetchFailed("url_rejected", "차트 저장소 주소를 읽지 못했어요") from exc


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
        "Accept-Encoding": "identity", "Connection": "close", "User-Agent": "kukie-agent",
    })
    response = connection.getresponse()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, response


def download(url: str, sink: IO[bytes], *, limit: int, deadline: float, opener: Opener = _open) -> None:
    """url 을 sink 로 받는다. limit 이나 deadline 을 넘으면 읽는 도중 끊는다."""
    for _ in range(MAX_REDIRECTS + 1):
        checked_url(url)
        left = deadline - time.monotonic()
        if left <= 0:
            raise FetchFailed("fetch_timeout", "차트를 받는 시간이 제한을 넘었어요")
        address = public_address(urlsplit(url).hostname)
        try:
            status, headers, body = opener(url, address, min(left, _SOCKET_TIMEOUT_SECONDS))
            with body:
                if status in _REDIRECTS and headers.get("location"):
                    url = _joined(url, headers["location"])
                    continue
                if status != 200:
                    raise FetchFailed("fetch_failed", f"차트 저장소가 {status} 로 답했어요")
                received = 0
                # read1: 한 번 받은 만큼만 돌려준다. read 는 64KiB 가 찰 때까지 기다려, 조금씩 보내는 서버가 전체 시간 검사를 비켜 간다
                while chunk := body.read1(_CHUNK):
                    received += len(chunk)
                    if received > limit:
                        raise FetchFailed("fetch_too_large", "받는 파일이 크기 제한을 넘었어요")
                    if time.monotonic() > deadline:
                        raise FetchFailed("fetch_timeout", "차트를 받는 시간이 제한을 넘었어요")
                    sink.write(chunk)
                return
        except (OSError, http.client.HTTPException) as exc:
            # 문구에 주소가 섞일 수 있어 종류만 남긴다
            logger.info("차트 받기 실패: %s", type(exc).__name__)
            raise FetchFailed("fetch_failed", "차트 저장소에 연결하지 못했어요") from exc
    raise FetchFailed("fetch_failed", "리다이렉트가 너무 많아요")


def chart_entries(index: IO[bytes], chart: str) -> Iterator[bytes]:
    """index.yaml 의 entries 에서 그 차트의 목록 항목을 하나씩 내놓는다. 줄 단위로 읽어 메모리를 쓰지 않는다."""
    in_entries = capturing = False
    child_indent: int | None = None
    item_indent: int | None = None
    item: list[bytes] = []
    size = 0
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
        if indent <= child_indent and not stripped.startswith(b"- "):
            if capturing:
                break                                   # 다음 차트가 시작했다
            match = _ENTRY_KEY.match(line.decode("utf-8", "replace"))
            capturing = bool(match) and chart in match.groups()
            continue
        if not capturing:
            continue
        if item_indent is None:
            item_indent = indent
        if indent == item_indent and stripped.startswith(b"- "):
            if item:
                yield b"".join(item)
            item, size = [], 0
        size += len(raw)
        if size > MAX_ENTRY_BYTES:
            raise FetchFailed("index_too_large", "차트 저장소 목록의 항목이 크기 제한을 넘어요")
        item.append(raw)
    if item:
        yield b"".join(item)


def _scalar(raw: bytes) -> str:
    text = raw.split(b" #", 1)[0].strip().decode("utf-8", "replace")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_entry(item: bytes) -> dict:
    """목록 항목에서 그 항목 바로 아래의 version · digest · urls 만 줄 단위로 꺼낸다. dependencies 안의 version 은 깊이가 달라 보지 않는다."""
    entry: dict = {}
    field_indent: int | None = None
    in_urls = False
    for line in item.splitlines():
        text = line.lstrip(b" ")
        indent = len(line) - len(text)
        if field_indent is None:                       # 첫 줄 `- key: value` 의 key 자리가 칸의 깊이다
            field_indent = indent + 2
            text = text[2:].lstrip(b" ")
            indent = field_indent
        if in_urls and indent >= field_indent and text.startswith(b"- "):
            entry["urls"].append(_scalar(text[2:]))
            continue
        if indent != field_indent:
            continue
        in_urls = False
        match = _ENTRY_FIELD.match(text)
        if match is None:
            continue
        key, value = match.group(1).decode(), (match.group(2) or b"").strip()
        if key != "urls":
            entry[key] = _scalar(value)
        elif value.startswith(b"[") and value.endswith(b"]"):
            entry["urls"] = [_scalar(part) for part in value[1:-1].split(b",") if part.strip()]
        else:
            entry["urls"] = []
            in_urls = True
    return entry


def find_entry(index: IO[bytes], chart: str, version: str) -> dict | None:
    """버전이 같은 항목. 앞의 v 는 helm 처럼 없는 셈 친다(`1.21.3` 은 `v1.21.3`)."""
    bare = version.removeprefix("v")
    for item in chart_entries(index, chart):
        entry = parse_entry(item)
        if str(entry.get("version", "")).removeprefix("v") == bare:
            return entry
    return None


def fetch_chart(repo_url: str, chart: str, version: str, *, opener: Opener = _open) -> bytes:
    """저장소에서 그 차트 그 버전의 tgz 를 받아 돌려준다. 범위(`1.x`)는 찾지 않는다. 실패는 FetchFailed."""
    if not CHART_NAME.match(chart) or not CHART_VERSION.match(version) or len(repo_url) > 2048:
        raise FetchFailed("url_rejected", "차트 이름이나 버전 형식이 맞지 않아요")
    deadline = time.monotonic() + TIMEOUT_SECONDS
    base = checked_url(repo_url.rstrip("/") + "/")
    with tempfile.TemporaryFile() as index:
        download(urljoin(base, "index.yaml"), index, limit=MAX_INDEX_BYTES, deadline=deadline, opener=opener)
        index.seek(0)
        entry = find_entry(index, chart, version)
    if entry is None:
        raise FetchFailed("version_not_found", "저장소에서 그 차트 그 버전을 찾지 못했어요")
    urls = entry.get("urls")
    if not urls:
        raise FetchFailed("index_unreadable", "차트 저장소 목록에 내려받을 주소가 없어요")
    with tempfile.SpooledTemporaryFile(max_size=MAX_CHART_BYTES) as sink:
        download(_joined(base, urls[0]), sink, limit=MAX_CHART_BYTES, deadline=deadline, opener=opener)
        sink.seek(0)
        data = sink.read()
    digest = entry.get("digest")
    if digest and digest != hashlib.sha256(data).hexdigest():
        raise FetchFailed("digest_mismatch", "받은 차트의 digest 가 저장소 목록과 달라요")
    return data
