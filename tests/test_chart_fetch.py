"""차트 내려받기 — 네트워크 없이 가짜 응답으로 막는 길을 본다 (DURO-137)."""
from __future__ import annotations

import hashlib
import io

import pytest

from kukie_agent.manifests import chart_fetch
from kukie_agent.manifests.chart_fetch import FetchFailed, chart_entries, checked_url, fetch_chart, find_entry, public_address


def resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(chart_fetch.socket, "getaddrinfo",
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
    "https://[bad",
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
    dependencies:
    - name: sub
      version: 1.0.0
    urls:
    - https://cdn.example.com/demo-2.0.0.tgz
    digest: DIGEST
  - name: demo
    version: 1.0.0
    urls:
    - charts/demo-1.0.0.tgz
  zeta:
    - name: zeta
      version: "3.0.0"
      urls: [zeta.tgz]
generated: "2026-01-01T00:00:00Z"
"""


def test_entries_hold_only_the_wanted_chart():
    items = list(chart_entries(io.BytesIO(INDEX), "demo"))
    assert len(items) == 2 and all(b"other" not in i and b"generated" not in i and b"zeta" not in i for i in items)


def test_exact_version_is_found_not_a_dependency_version():
    assert find_entry(io.BytesIO(INDEX), "demo", "1.0.0")["urls"] == ["charts/demo-1.0.0.tgz"]


def test_last_chart_with_deeper_list_and_quoted_version():
    assert find_entry(io.BytesIO(INDEX), "zeta", "3.0.0")["name"] == "zeta"


def test_missing_chart_or_version():
    assert find_entry(io.BytesIO(INDEX), "nothing", "1.0.0") is None
    assert find_entry(io.BytesIO(INDEX), "demo", "9.9.9") is None


def test_leading_v_is_ignored_like_helm():
    index = b"entries:\n  demo:\n  - version: v1.2.3\n    urls: [a.tgz]\n  - version: 1.2.4\n    urls: [b.tgz]\n"
    assert find_entry(io.BytesIO(index), "demo", "1.2.3")["urls"] == ["a.tgz"]
    assert find_entry(io.BytesIO(index), "demo", "v1.2.4")["urls"] == ["b.tgz"]


def test_version_is_matched_literally():
    assert find_entry(io.BytesIO(INDEX), "demo", "1.0.0".replace(".", "x")) is None


def test_big_entry_is_rejected(monkeypatch):
    monkeypatch.setattr(chart_fetch, "MAX_ENTRY_BYTES", 10)
    with pytest.raises(FetchFailed) as caught:
        list(chart_entries(io.BytesIO(INDEX), "demo"))
    assert caught.value.reason == "index_too_large"


def test_alias_bomb_is_rejected():
    index = b"entries:\n  demo:\n  - version: 1.0.0\n    a: &a {x: 1}\n    b: [" + b", ".join([b"*a"] * 200) + b"]\n"
    with pytest.raises(FetchFailed) as caught:
        find_entry(io.BytesIO(index), "demo", "1.0.0")
    assert caught.value.reason == "index_unreadable"


def test_repeated_decoys_stop_parsing(monkeypatch):
    decoy = b"  - version: 2.0.0\n    dependencies:\n    - version: 1.0.0\n"
    with pytest.raises(FetchFailed):
        find_entry(io.BytesIO(b"entries:\n  demo:\n" + decoy * 50), "demo", "1.0.0")


def test_unknown_yaml_tag_is_rejected_not_executed():
    index = b"entries:\n  demo:\n  - version: 1.0.0\n    a: !!python/object/apply:os.system [id]\n"
    with pytest.raises(FetchFailed):
        find_entry(io.BytesIO(index), "demo", "1.0.0")


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


def test_fetch_finds_version_checks_digest_and_connects_to_the_checked_address(public_dns):
    web = web_with_chart(b"chart-bytes")
    assert fetch_chart("https://charts.example.com/repo", "demo", "2.0.0", opener=web) == b"chart-bytes"
    assert {address for _, address in web.opened} == {"93.184.216.34"}


def test_fetch_resolves_relative_chart_url(public_dns):
    web = web_with_chart(b"chart-bytes")
    assert fetch_chart("https://charts.example.com/repo/", "demo", "1.0.0", opener=web) == b"chart-bytes"


@pytest.mark.parametrize("digest", ["0" * 64, "a" * 64])
def test_fetch_rejects_digest_mismatch(public_dns, digest):
    """숫자로만 된 digest 는 YAML 이 숫자로 읽는다. 그래도 검사를 비켜 가지 못한다."""
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com/repo", "demo", "2.0.0", opener=web_with_chart(b"x", digest=digest))
    assert caught.value.reason == "digest_mismatch"


def test_oversized_download_stops_while_reading(public_dns, monkeypatch):
    monkeypatch.setattr(chart_fetch, "MAX_CHART_BYTES", 100)
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com/repo", "demo", "1.0.0", opener=web_with_chart(b"x" * 1000))
    assert caught.value.reason == "fetch_too_large"


def test_trickling_server_hits_the_total_deadline(public_dns, monkeypatch):
    """응답이 조금씩 계속 와도 전체 시간에서 끊는다."""
    class Trickle(io.BytesIO):
        def read1(self, size=-1):
            clock["now"] += 11
            return b"x"
    clock = {"now": 0.0}
    monkeypatch.setattr(chart_fetch.time, "monotonic", lambda: clock["now"])
    web = lambda url, address, timeout: (200, {}, Trickle())
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com", "demo", "1.0.0", opener=web)
    assert caught.value.reason == "fetch_timeout"


def test_redirect_to_internal_address_is_blocked(monkeypatch):
    def resolve(host, port, **_):
        return [(2, 1, 6, "", ("10.0.0.5" if host == "inner.example.com" else "93.184.216.34", port))]
    monkeypatch.setattr(chart_fetch.socket, "getaddrinfo", resolve)
    web = FakeWeb({"https://charts.example.com/index.yaml": (302, {"location": "https://inner.example.com/x"}, b"")})
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com", "demo", "1.0.0", opener=web)
    assert caught.value.reason == "address_blocked"
    assert len(web.opened) == 1                        # 내부 주소로는 연결하지 않았다


def test_redirect_to_http_is_blocked(public_dns):
    web = FakeWeb({"https://charts.example.com/index.yaml": (302, {"location": "http://charts.example.com/x"}, b"")})
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com", "demo", "1.0.0", opener=web)
    assert caught.value.reason == "url_rejected"


def test_redirect_loop_stops(public_dns):
    web = FakeWeb({"https://charts.example.com/index.yaml": (302, {"location": "/index.yaml"}, b"")})
    with pytest.raises(FetchFailed):
        fetch_chart("https://charts.example.com", "demo", "1.0.0", opener=web)
    assert len(web.opened) == chart_fetch.MAX_REDIRECTS + 1


def test_chart_url_in_index_goes_through_the_same_check(public_dns):
    index = b"entries:\n  demo:\n  - version: 1.0.0\n    urls: [\"http://169.254.169.254/x\"]\n"
    web = FakeWeb({"https://charts.example.com/index.yaml": (200, {}, index)})
    with pytest.raises(FetchFailed) as caught:
        fetch_chart("https://charts.example.com", "demo", "1.0.0", opener=web)
    assert caught.value.reason == "url_rejected"


@pytest.mark.parametrize("chart,version", [("../x", "1.0.0"), ("x", "--flag"), ("x", "1.0.0/../y")])
def test_chart_name_and_version_format(chart, version):
    with pytest.raises(FetchFailed):
        fetch_chart("https://charts.example.com", chart, version, opener=FakeWeb({}))
