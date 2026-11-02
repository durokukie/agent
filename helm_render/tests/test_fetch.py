"""차트 내려받기 — 네트워크 없이 가짜 응답으로 막는 길을 본다. 진짜 저장소 확인은 README 의 수동 절차다."""
from __future__ import annotations

import hashlib
import io
from dataclasses import replace

import pytest

from kukie_helm_render import fetch as fetching
from kukie_helm_render.fetch import FetchFailed, chart_block, checked_url, fetch_chart, public_address, parse_block


def resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(fetching.socket, "getaddrinfo",
                        lambda host, port, **_: [(2, 1, 6, "", (a, port)) for a in addresses])


@pytest.mark.parametrize("address", [
    "10.0.0.1", "172.20.0.10", "192.168.1.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "fd00::1", "fe80::1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "224.0.0.1",
])
def test_internal_addresses_are_blocked(monkeypatch, address):
    resolves_to(monkeypatch, address)
    with pytest.raises(FetchFailed) as caught:
        public_address("charts.example.com")
    assert caught.value.reason == "address_blocked"


def test_one_internal_address_among_public_ones_is_blocked(monkeypatch):
    resolves_to(monkeypatch, "93.184.216.34", "10.0.0.1")
    with pytest.raises(FetchFailed):
        public_address("charts.example.com")


def test_public_address_passes(monkeypatch):
    resolves_to(monkeypatch, "93.184.216.34")
    assert public_address("charts.example.com") == "93.184.216.34"


@pytest.mark.parametrize("url", [
    "http://charts.example.com", "file:///etc/passwd", "gopher://x", "https://user:pw@charts.example.com",
    "https://charts.example.com:8443", "https://", "oci://registry.example.com/x", "https://x:notaport",
])
def test_only_plain_https_urls_are_accepted(url):
    with pytest.raises(FetchFailed) as caught:
        checked_url(url)
    assert caught.value.reason == "url_rejected"


INDEX = b"""apiVersion: v1
entries:
  other:
  - name: other
    version: 1.0.0
    urls: [other.tgz]
  demo:
  - name: demo
    version: 2.0.0
    urls:
    - https://cdn.example.com/demo-2.0.0.tgz
    digest: DIGEST
  - name: demo
    version: 1.0.0
    urls:
    - charts/demo-1.0.0.tgz
  zeta:
    - name: zeta
      version: 3.0.0
      urls: [zeta.tgz]
generated: "2026-01-01T00:00:00Z"
"""


def test_block_holds_only_the_wanted_chart():
    block = chart_block(io.BytesIO(INDEX), "demo", 1024 * 1024)
    assert b"other" not in block and b"zeta" not in block and b"generated" not in block
    assert block.count(b"version:") == 2


def test_block_for_last_chart_with_deeper_list():
    block = chart_block(io.BytesIO(INDEX), "zeta", 1024 * 1024)
    assert b"3.0.0" in block and b"generated" not in block


def test_missing_chart_has_no_block():
    assert chart_block(io.BytesIO(INDEX), "nothing", 1024 * 1024) is None


def test_block_limit_applies():
    with pytest.raises(FetchFailed) as caught:
        chart_block(io.BytesIO(INDEX), "demo", 10)
    assert caught.value.reason == "index_too_large"


def test_alias_bomb_is_rejected(settings):
    block = b"entries:\n  demo:\n  - &a {x: 1}\n" + b"  - *a\n" * 200
    with pytest.raises(FetchFailed) as caught:
        parse_block(block, "demo", settings)
    assert caught.value.reason == "index_unreadable"


class FakeWeb:
    """주소별 (상태, 머리, 내용). 어느 주소로 연결하려 했는지 기록한다."""

    def __init__(self, pages):
        self.pages = pages
        self.opened = []

    def __call__(self, url, address, timeout):
        self.opened.append((url, address))
        status, headers, body = self.pages[url]
        return status, headers, io.BytesIO(body)


@pytest.fixture
def public_dns(monkeypatch):
    resolves_to(monkeypatch, "93.184.216.34")


def web_with_chart(archive: bytes, digest: str | None = None) -> FakeWeb:
    index = INDEX.replace(b"DIGEST", (digest or hashlib.sha256(archive).hexdigest()).encode())
    return FakeWeb({
        "https://charts.example.com/repo/index.yaml": (200, {}, index),
        "https://cdn.example.com/demo-2.0.0.tgz": (200, {}, archive),
        "https://charts.example.com/repo/charts/demo-1.0.0.tgz": (200, {}, archive),
    })


def test_fetch_finds_exact_version_and_checks_digest(public_dns, settings):
    web = web_with_chart(b"chart-bytes")
    assert fetch_chart("https://charts.example.com/repo", "demo", "2.0.0", settings, opener=web) == b"chart-bytes"


def test_fetch_resolves_relative_chart_url(public_dns, settings):
    web = web_with_chart(b"chart-bytes")
    assert fetch_chart("https://charts.example.com/repo/", "demo", "1.0.0", settings, opener=web) == b"chart-bytes"


def test_fetch_rejects_digest_mismatch(public_dns, settings):
    web = web_with_chart(b"chart-bytes", digest="0" * 64)
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com/repo", "demo", "2.0.0", settings, opener=web)
    assert caught.value.reason == "digest_mismatch"


def test_fetch_reports_missing_version_and_chart(public_dns, settings):
    web = web_with_chart(b"x")
    for chart, version, reason in (("demo", "9.9.9", "version_not_found"), ("nope", "1.0.0", "chart_not_found")):
        with pytest.raises(FetchFailed) as caught:
            fetch_chart("https://charts.example.com/repo", chart, version, settings, opener=web)
        assert caught.value.reason == reason


def test_oversized_download_stops_while_reading(public_dns, settings):
    web = web_with_chart(b"x" * 1000)
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com/repo", "demo", "1.0.0", replace(settings, max_chart_bytes=100), opener=web)
    assert caught.value.reason == "fetch_too_large"


def test_redirect_to_internal_address_is_blocked(monkeypatch, settings):
    def resolve(host, port, **_):
        address = "10.0.0.5" if host == "inner.example.com" else "93.184.216.34"
        return [(2, 1, 6, "", (address, port))]
    monkeypatch.setattr(fetching.socket, "getaddrinfo", resolve)
    web = FakeWeb({"https://charts.example.com/index.yaml": (302, {"location": "https://inner.example.com/x"}, b"")})
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com", "demo", "1.0.0", settings, opener=web)
    assert caught.value.reason == "address_blocked"
    assert len(web.opened) == 1                        # 내부 주소로는 연결하지 않았다


def test_redirect_to_http_is_blocked(public_dns, settings):
    web = FakeWeb({"https://charts.example.com/index.yaml": (302, {"location": "http://charts.example.com/x"}, b"")})
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com", "demo", "1.0.0", settings, opener=web)
    assert caught.value.reason == "url_rejected"


def test_redirect_loop_stops(public_dns, settings):
    web = FakeWeb({"https://charts.example.com/index.yaml": (302, {"location": "/index.yaml"}, b"")})
    with pytest.raises(FetchFailed):
        fetch_chart("https://charts.example.com", "demo", "1.0.0", settings, opener=web)
    assert len(web.opened) == settings.max_redirects + 1


def test_chart_url_in_index_goes_through_the_same_check(public_dns, settings):
    index = b"entries:\n  demo:\n  - version: 1.0.0\n    urls: [\"http://169.254.169.254/x\"]\n"
    web = FakeWeb({"https://charts.example.com/index.yaml": (200, {}, index)})
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com", "demo", "1.0.0", settings, opener=web)
    assert caught.value.reason == "url_rejected"
