"""Helm 렌더링 호출 — 파드가 죽거나 바빠도 예외 없이 "렌더링 실패"로 돌아오는지, 조건이 Argo CD 와 같은지 (DURO-137).

연결 실패는 흉내 내지 않고 진짜 소켓으로 만든다. 진짜 서비스와의 시험은 helm_render/tests 와 kind 수동 절차다.
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import socket
import tarfile
import threading

import httpx
import pytest

from kukie_agent.manifests import helm
from kukie_agent.manifests.helm import (
    RenderConditions, UnsupportedApplication, ValueFile, conditions_from_application, fetch_chart, pack_chart,
    parse_rendered, render, value_files_from_application,
)

CONDITIONS = RenderConditions(release_name="rel", namespace="ns", kube_version="1.33.0")


@pytest.fixture
def service(monkeypatch):
    """서비스 응답을 정한다. 받은 요청 본문을 calls 에 모은다."""
    calls = []

    def install(status=200, body=b"kind: X\n", headers=None, error=None):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.path, json.loads(request.content)))
            if error is not None:
                raise error
            return httpx.Response(status, content=body, headers=headers or {})
        monkeypatch.setattr(helm, "_transport", httpx.MockTransport(handler))
        monkeypatch.setenv(helm.RENDER_URL_ENV, "http://helm-render:8000")
        return calls
    return install


def test_success_returns_manifest_and_version(service):
    calls = service(body=b"kind: ConfigMap\n", headers={"X-Helm-Version": "v4.2.1"})
    result = render(b"tgz", [ValueFile("a", "x: 1\n"), ValueFile("b", "x: 2\n")], CONDITIONS)
    assert result.ok and result.manifest == "kind: ConfigMap\n" and result.helm_version == "v4.2.1"
    path, sent = calls[0]
    assert path == "/render"
    assert base64.b64decode(sent["chart_tgz"]) == b"tgz"
    assert [f["name"] for f in sent["value_files"]] == ["a", "b"]    # 순서가 곧 병합 순서다
    assert (sent["release_name"], sent["namespace"], sent["kube_version"]) == ("rel", "ns", "1.33.0")


def test_no_url_is_a_failure_not_an_exception(monkeypatch):
    monkeypatch.delenv(helm.RENDER_URL_ENV, raising=False)
    result = render(b"tgz", [], CONDITIONS)
    assert result.reason == "not_configured" and result.service_failed


@pytest.mark.parametrize("error", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut"), httpx.ReadError("reset"),
])
def test_dead_pod_is_a_render_failure(service, error):
    service(error=error)
    result = render(b"tgz", [], CONDITIONS)
    assert not result.ok and result.reason == "unavailable" and result.service_failed
    assert "렌더링 서비스" in result.message


def test_busy_is_a_render_failure(service):
    service(status=429, body=b'{"reason": "busy", "message": "x"}')
    result = render(b"tgz", [], CONDITIONS)
    assert result.reason == "busy" and result.service_failed


@pytest.mark.parametrize("reason", ["timeout", "output_too_large", "memory_exceeded", "chart_has_link", "helm_failed"])
def test_chart_failures_keep_their_reason(service, reason):
    service(status=422, body=json.dumps({"reason": reason, "message": "자세한 이유"}).encode())
    result = render(b"tgz", [], CONDITIONS)
    assert result.reason == reason and result.message == "자세한 이유" and not result.service_failed


@pytest.mark.parametrize("status,expected", [(502, "unavailable"), (503, "unavailable"), (404, "bad_response")])
def test_answers_that_are_not_from_the_service(service, status, expected):
    service(status=status, body=b"<html>bad gateway</html>")
    assert render(b"tgz", [], CONDITIONS).reason == expected


def test_oversized_response_is_cut(service, monkeypatch):
    monkeypatch.setattr(helm, "_MAX_RESPONSE_BYTES", 10)
    service(body=b"x" * 1000)
    assert render(b"tgz", [], CONDITIONS).reason == "bad_response"


def test_binary_response_is_a_failure(service):
    service(body=b"\xff\xfe\xfd")
    assert render(b"tgz", [], CONDITIONS).reason == "bad_response"


def test_failure_message_is_not_logged(service, caplog):
    caplog.set_level("DEBUG")
    service(status=422, body=json.dumps({"reason": "helm_failed", "message": "password=hunter2"}).encode())
    result = render(b"tgz", [], CONDITIONS)
    assert "hunter2" in result.message and "hunter2" not in caplog.text


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("url", ["http://helm-render:not-a-port", "http://[bad", "not a url"])
def test_bad_service_url_is_a_failure_not_an_exception(monkeypatch, url):
    monkeypatch.setenv(helm.RENDER_URL_ENV, url)
    assert not render(b"tgz", [], CONDITIONS).ok


def test_real_connection_refused(monkeypatch):
    monkeypatch.setenv(helm.RENDER_URL_ENV, f"http://127.0.0.1:{_closed_port()}")
    assert render(b"tgz", [], CONDITIONS).reason == "unavailable"


def test_real_pod_dying_mid_response(monkeypatch):
    """응답 중간에 파드가 죽는 경우 — 약속한 길이보다 적게 주고 연결을 끊는다."""
    class Dies(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("content-length", "100000")
            self.end_headers()
            self.wfile.write(b"kind: partial")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Dies)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(helm.RENDER_URL_ENV, f"http://127.0.0.1:{server.server_port}")
        assert render(b"tgz", [], CONDITIONS).reason == "unavailable"
    finally:
        server.shutdown()


def test_real_pod_that_never_answers(monkeypatch):
    monkeypatch.setattr(helm, "_TIMEOUT", httpx.Timeout(0.2, connect=0.2))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setenv(helm.RENDER_URL_ENV, f"http://127.0.0.1:{listener.getsockname()[1]}")
        assert render(b"tgz", [], CONDITIONS).reason == "unavailable"


@pytest.mark.parametrize("error", [OSError("no tmp"), ValueError("bad url")])
def test_fetch_turns_unexpected_errors_into_a_result(monkeypatch, error):
    def fails(*args):
        raise error
    monkeypatch.setattr(helm.chart_fetch, "fetch_chart", fails)
    assert fetch_chart("https://charts.example.com", "demo", "1.0.0").reason == "fetch_failed"


def test_fetch_never_raises(monkeypatch):
    monkeypatch.setattr(helm.chart_fetch, "fetch_chart", lambda *a: b"chart")
    assert fetch_chart("https://charts.example.com", "demo", "1.0.0").archive == b"chart"

    def blocked(*args):
        raise helm.chart_fetch.FetchFailed("address_blocked", "내부망")
    monkeypatch.setattr(helm.chart_fetch, "fetch_chart", blocked)
    fetched = fetch_chart("https://10.0.0.1", "demo", "1.0.0")
    assert not fetched.ok and fetched.reason == "address_blocked"


APPLICATION = {
    "metadata": {"name": "payments"},
    "spec": {
        "destination": {"namespace": "team-a"},
        "source": {"repoURL": "https://charts.example.com", "chart": "demo", "targetRevision": "1.0.0"},
    },
}


def app(**helm_fields) -> dict:
    return {**APPLICATION, "spec": {**APPLICATION["spec"], "source": {**APPLICATION["spec"]["source"], "helm": helm_fields}}}


def test_conditions_default_to_application_values():
    got = conditions_from_application(APPLICATION, kube_version="v1.36.5-eks-577a51e")
    assert got == RenderConditions(release_name="payments", namespace="team-a", kube_version="1.36.5")


def test_helm_fields_win():
    got = conditions_from_application(
        app(releaseName="pay", namespace="other", kubeVersion="1.30.0", apiVersions=["example.com/v1"],
            skipCrds=True, skipSchemaValidation=True, skipTests=True),
        kube_version="1.36.0", api_versions=("apps/v1",))
    assert got == RenderConditions(release_name="pay", namespace="other", kube_version="1.30.0",
                                   api_versions=("example.com/v1",), skip_crds=True, skip_schema_validation=True,
                                   skip_tests=True)


def test_cluster_api_versions_are_used_by_default():
    assert conditions_from_application(APPLICATION, kube_version="1.36.0", api_versions=("apps/v1",)).api_versions == ("apps/v1",)


@pytest.mark.parametrize("fields", [{"parameters": [{"name": "a", "value": "1"}]}, {"fileParameters": [{"name": "a", "path": "b"}]}])
def test_set_style_fields_are_refused_not_ignored(fields):
    with pytest.raises(UnsupportedApplication):
        conditions_from_application(app(**fields), kube_version="1.36.0")


def test_multi_source_picks_the_chart():
    item = {"metadata": {"name": "m"}, "spec": {"destination": {"namespace": "n"}, "sources": [
        {"repoURL": "https://git.example.com/values.git", "ref": "values"},
        {"repoURL": "https://charts.example.com", "chart": "demo", "helm": {"releaseName": "from-chart"}},
    ]}}
    assert conditions_from_application(item, kube_version="1.36.0").release_name == "from-chart"


def test_two_charts_are_refused():
    item = {"metadata": {"name": "m"}, "spec": {"sources": [{"chart": "a"}, {"chart": "b"}]}}
    with pytest.raises(UnsupportedApplication):
        conditions_from_application(item, kube_version="1.36.0")


@pytest.mark.parametrize("given,expected", [("v1.36.5-eks-577a51e", "1.36.5"), ("1.33.0", "1.33.0"), ("1.33", "1.33"), ("1.33+", "1.33")])
def test_kube_version_matches_argo_parsing(given, expected):
    assert helm.normalize_kube_version(given) == expected


def test_bad_kube_version_is_refused():
    with pytest.raises(UnsupportedApplication):
        helm.normalize_kube_version("--enable-dns")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "charts" / "demo" / "env").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "charts" / "demo" / "values-dev.yaml").write_text("a: dev\n")
    (root / "charts" / "demo" / "env" / "extra.yaml").write_text("a: extra\n")
    (root / "shared" / "common.yaml").write_text("a: common\n")
    return root


def read_values(item, repo, **kwargs):
    return value_files_from_application(item, chart_dir=repo / "charts" / "demo", repo_root=repo, **kwargs)


def test_value_files_keep_order_and_inline_goes_last(repo):
    item = app(valueFiles=["env/extra.yaml", "values-dev.yaml", "/shared/common.yaml", "../../shared/common.yaml"],
               values="a: inline\n")
    got = read_values(item, repo)
    assert [f.name for f in got] == ["env/extra.yaml", "values-dev.yaml", "/shared/common.yaml", "(인라인 values)"]
    assert got[0].content == "a: extra\n" and got[-1].content == "a: inline\n"


def test_values_object_wins_over_values(repo):
    got = read_values(app(values="a: text\n", valuesObject={"a": "object"}), repo)
    assert json.loads(got[0].content) == {"a": "object"}


def test_empty_values_object_still_wins_over_values(repo):
    got = read_values(app(values="a: text\n", valuesObject={}), repo)
    assert [f.content for f in got] == ["{}"]


def test_ref_source_is_read_from_its_own_repo(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "prod.yaml").write_text("a: prod\n")
    got = read_values(app(valueFiles=["$values/prod.yaml"]), repo, refs={"values": other})
    assert got[0].content == "a: prod\n"


def test_value_file_paths_cannot_leave_the_repo(repo, tmp_path):
    """CVE-2022-24348 — 이 파드의 파일을 values 로 읽지 못한다."""
    (tmp_path / "secret.yaml").write_text("token: x\n")
    (repo / "link.yaml").symlink_to(tmp_path / "secret.yaml")
    other = tmp_path / "other"
    other.mkdir()
    for path in ("../../../secret.yaml", "/../secret.yaml", "/link.yaml", "../../link.yaml"):
        with pytest.raises(UnsupportedApplication, match="레포 밖"):
            read_values(app(valueFiles=[path]), repo)
    with pytest.raises(UnsupportedApplication, match="레포 밖"):
        read_values(app(valueFiles=["$values/../secret.yaml"]), repo, refs={"values": other})


@pytest.mark.parametrize("path", ["https://example.com/v.yaml", "file:///etc/passwd", "*.yaml", "env/?.yaml",
                                  "$unknown/v.yaml", "env/$ARGOCD_APP_NAME.yaml"])
def test_paths_we_cannot_match_are_refused(repo, path):
    with pytest.raises(UnsupportedApplication):
        read_values(app(valueFiles=[path]), repo)


def test_missing_value_file(repo):
    with pytest.raises(UnsupportedApplication, match="없어요"):
        read_values(app(valueFiles=["nope.yaml"]), repo)
    assert read_values(app(valueFiles=["nope.yaml"], ignoreMissingValueFiles=True), repo) == []


def test_value_file_named_twice_keeps_first_place_like_argo(repo):
    """Argo CD v3.5.3 getResolvedValueFiles 의 appendUnique: [a, b, a] 는 -f a -f b 다."""
    got = read_values(app(valueFiles=["values-dev.yaml", "env/extra.yaml", "values-dev.yaml"]), repo)
    assert [f.name for f in got] == ["values-dev.yaml", "env/extra.yaml"]


def test_big_values_are_refused(repo, monkeypatch):
    monkeypatch.setattr(helm, "_MAX_VALUES_BYTES", 3)
    with pytest.raises(UnsupportedApplication):
        read_values(app(valueFiles=["values-dev.yaml"]), repo)


def test_pack_keeps_links_as_links(tmp_path):
    chart = tmp_path / "demo"
    (chart / "templates").mkdir(parents=True)
    (chart / ".git").mkdir()
    (chart / ".git" / "config").write_text("secret\n")
    (chart / "Chart.yaml").write_text("name: demo\n")
    (chart / "templates" / "x.yaml").symlink_to("/etc/passwd")
    with tarfile.open(fileobj=io.BytesIO(pack_chart(chart)), mode="r:gz") as tar:
        members = {m.name: m for m in tar}
    assert "demo/Chart.yaml" in members
    assert members["demo/templates/x.yaml"].issym()    # 따라가서 내용을 담지 않는다
    assert not any(".git" in name for name in members)


def test_parse_rendered_reads_objects():
    text = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n---\n# comment only\n---\nnot: an object\n"
    assert [o["metadata"]["name"] for o in parse_rendered(text)] == ["a"]


def test_parse_rendered_handles_equals_scalar():
    """kube-prometheus-stack 에 있는 값. 보통 PyYAML 은 여기서 실패한다."""
    text = "apiVersion: v1\nkind: X\nmetadata: {name: a}\nargs:\n- =\n"
    assert parse_rendered(text)[0]["args"] == ["="]


def test_parse_rendered_limits_aliases():
    bomb = "apiVersion: v1\nkind: X\na: &a [1]\nb: [" + ", ".join(["*a"] * 200) + "]\n"
    with pytest.raises(ValueError, match="앨리어스"):
        parse_rendered(bomb)


def test_parse_rendered_rejects_circular_alias():
    with pytest.raises(ValueError, match="자기 자신"):
        parse_rendered("apiVersion: v1\nkind: X\na: &x {b: *x}\n")


def test_parse_rendered_limits_expanded_size(monkeypatch):
    """앨리어스 스무 개로 2의 스무 제곱. 개수 제한은 통과한다."""
    monkeypatch.setattr(helm, "_MAX_EXPANDED_NODES", 100000)
    levels = ["a0: &a0 [1, 2]"] + [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 25)]
    with pytest.raises(ValueError, match="펼치면"):
        parse_rendered("apiVersion: v1\nkind: X\n" + "\n".join(levels) + "\n")


def test_parse_rendered_allows_shared_values():
    text = "apiVersion: v1\nkind: X\na: &x {b: 1}\nc: *x\nd: *x\n"
    assert parse_rendered(text)[0]["d"] == {"b": 1}


def test_parse_rendered_limits_size(monkeypatch):
    monkeypatch.setattr(helm, "_MAX_RESPONSE_BYTES", 5)
    with pytest.raises(ValueError):
        parse_rendered("apiVersion: v1\n")


def test_parse_rendered_error_has_no_values():
    with pytest.raises(ValueError) as caught:
        parse_rendered("password: hunter2\n  bad: [")
    assert "hunter2" not in str(caught.value)
