"""API 끝에서 끝까지 — 진짜 helm 을 돌린다. 나쁜 차트에 이 서비스는 실패로 답하고, 계속 살아 있어야 한다."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from conftest import chart_files, make_tgz, render_body
from kukie_helm_render.app import create_app
from kukie_helm_render.chart import unpack_chart

SPIN = "{{ range until 100000 }}{{ range until 100000 }}{{ $x := 1 }}{{ end }}{{ end }}"


def run(settings, scenario):
    """lifespan 까지 돌린 앱에 scenario(client) 를 실행한다."""
    async def main():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://render", timeout=60) as client:
                return await scenario(client)
    return asyncio.run(main())


def post(settings, body):
    async def scenario(client):
        return await client.post("/render", json=body)
    return run(settings, scenario)


def direct_helm(helm_bin: str, chart: Path, *args: str) -> bytes:
    return subprocess.run([helm_bin, "template", str(chart), *args], capture_output=True, check=True).stdout


def test_normal_chart_matches_direct_helm(settings, helm_bin, tmp_path):
    archive = make_tgz(chart_files())
    values = tmp_path / "v.yaml"
    values.write_text("color: red\n")
    response = post(settings, render_body(archive, value_files=[{"name": "v.yaml", "content": "color: red\n"}]))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.headers["x-helm-version"]
    chart = unpack_chart(archive, tmp_path / "direct", settings)
    expected = direct_helm(helm_bin, chart, "--name-template", "rel", "--namespace", "team-a", "--kube-version", "1.33.0",
                           "--values", str(values), "--include-crds")
    assert response.content == expected
    assert b"rel-demo" in response.content and b"namespace: team-a" in response.content
    assert b'color: "red"' in response.content and b'kube: "v1.33.0"' in response.content


def test_value_files_merge_in_the_given_order(settings):
    files = [{"name": "a", "content": "color: first\n"}, {"name": "b", "content": "color: last\n"}]
    assert b'"last"' in post(settings, render_body(make_tgz(chart_files()), value_files=files)).content
    assert b'"first"' in post(settings, render_body(make_tgz(chart_files()), value_files=files[::-1])).content


def test_api_versions_reach_helm(settings):
    response = post(settings, render_body(make_tgz(chart_files()), api_versions=["example.com/v1"]))
    assert b'batch: "true"' in response.content


def test_skip_flags_match_argo(settings):
    files = {**chart_files(), "demo/crds/c.yaml": "apiVersion: example.com/v1\nkind: Thing\nmetadata: {name: crd-marker}\n",
             "demo/templates/tests/t.yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: test-marker\n  annotations:\n    helm.sh/hook: test\n"}
    archive = make_tgz(files)
    default = post(settings, render_body(archive)).content
    assert b"crd-marker" in default and b"test-marker" in default
    skipped = post(settings, render_body(archive, skip_crds=True, skip_tests=True)).content
    assert b"crd-marker" not in skipped and b"test-marker" not in skipped


def test_value_file_name_is_never_a_path(settings):
    """CVE-2022-24348 — 이름에 경로를 넣어도 그 파일을 읽지 않는다."""
    files = [{"name": "../../../../etc/passwd", "content": "color: safe\n"}]
    response = post(settings, render_body(make_tgz(chart_files()), value_files=files))
    assert response.status_code == 200 and b'"safe"' in response.content


def test_values_with_url_like_content_are_data(settings):
    files = [{"name": "https://169.254.169.254/x", "content": "color: https://example.com/x\n"}]
    assert post(settings, render_body(make_tgz(chart_files()), value_files=files)).status_code == 200


@pytest.mark.parametrize("field,value", [
    ("release_name", "--post-renderer=/bin/sh"),
    ("release_name", "{{ .Values.x }}"),
    ("namespace", "--kubeconfig"),
    ("namespace", "../x"),
    ("kube_version", "--enable-dns"),
    ("api_versions", ["--dry-run=server"]),
])
def test_flag_like_conditions_are_rejected(settings, field, value):
    response = post(settings, render_body(make_tgz(chart_files()), **{field: value}))
    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_request"
    assert str(value) not in response.text              # 받은 값을 되돌려 싣지 않는다


def test_unknown_field_is_rejected(settings):
    response = post(settings, render_body(make_tgz(chart_files()), post_renderer="/bin/sh"))
    assert response.json()["reason"] == "invalid_request"


def test_args_have_no_forbidden_options(tmp_path):
    from kukie_helm_render.helm import Conditions, template_args
    args = template_args(tmp_path, Conditions(release_name="r", kube_version="1.33.0", namespace="n"), [tmp_path / "v"])
    for forbidden in ("--enable-dns", "--dry-run", "--post-renderer", "--dependency-update", "--plugin"):
        assert not any(arg.startswith(forbidden) for arg in args)


def test_symlink_chart_fails_without_running_helm(settings):
    archive = make_tgz(chart_files(), links={"demo/templates/x.yaml": "/etc/passwd"})
    response = post(settings, render_body(archive))
    assert response.status_code == 422 and response.json()["reason"] == "chart_has_link"


def test_external_schema_ref_is_rejected(settings):
    archive = make_tgz({**chart_files(), "demo/values.schema.json": '{"$ref": "file:///etc/hosts"}'})
    assert post(settings, render_body(archive)).json()["reason"] == "schema_external_ref"


def test_local_schema_still_validates_values(settings):
    schema = '{"$schema": "http://json-schema.org/draft-07/schema#", "properties": {"color": {"enum": ["blue"]}}}'
    archive = make_tgz({**chart_files(), "demo/values.schema.json": schema})
    assert post(settings, render_body(archive)).status_code == 200
    bad = post(settings, render_body(archive, value_files=[{"name": "v", "content": "color: red\n"}]))
    assert bad.json()["reason"] == "schema_rejected"
    assert post(settings, render_body(archive, skip_schema_validation=True,
                                      value_files=[{"name": "v", "content": "color: red\n"}])).status_code == 200


def test_spinning_template_is_killed_at_the_deadline(settings):
    fast = replace(settings, timeout_seconds=1.0)
    archive = make_tgz(chart_files(spin=SPIN))
    response = post(fast, render_body(archive))
    assert response.status_code == 504 and response.json()["reason"] == "timeout"


def test_huge_output_is_cut_while_reading(settings):
    small = replace(settings, max_output_bytes=1024 * 1024)
    template = '{{ range until 30000 }}# {{ repeat 200 "x" }}\n{{ end }}'
    response = post(small, render_body(make_tgz(chart_files(big=template))))
    assert response.status_code == 422 and response.json()["reason"] == "output_too_large"


def test_output_at_the_limit_passes(settings):
    template = '{{ range until 100 }}# {{ repeat 100 "x" }}\n{{ end }}'
    assert post(settings, render_body(make_tgz(chart_files(big=template)))).status_code == 200


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc 로 메모리를 본다")
def test_memory_hog_is_killed_alone(settings):
    small = replace(settings, max_rss_bytes=100 * 1024 * 1024, timeout_seconds=30)
    hog = '{{ $s := "xxxxxxxxxxxxxxxx" }}{{ range until 26 }}{{ $s = printf "%s%s" $s $s }}{{ end }}{{ $s | len }}'
    response = post(small, render_body(make_tgz(chart_files(hog=hog))))
    assert response.json()["reason"] == "memory_exceeded"


def test_template_error_is_a_clean_failure(settings):
    archive = make_tgz(chart_files(bad='{{ fail "no good" }}'))
    response = post(settings, render_body(archive))
    assert response.status_code == 422 and response.json()["reason"] == "helm_failed"
    assert "no good" in response.json()["message"]


def test_missing_dependency_is_named(settings):
    chart = "apiVersion: v2\nname: demo\nversion: 0.1.0\ndependencies:\n- name: sub\n  version: 1.0.0\n  repository: https://example.com\n"
    archive = make_tgz({**chart_files(), "demo/Chart.yaml": chart})
    assert post(settings, render_body(archive)).json()["reason"] == "missing_dependency"


def test_oversized_body_is_rejected(settings):
    small = replace(settings, max_body_bytes=1024)
    response = post(small, {"chart_tgz": "x" * 4096})
    assert response.status_code == 413 and response.json()["reason"] == "body_too_large"


def test_not_base64_is_rejected(settings):
    response = post(settings, {**render_body(b""), "chart_tgz": "***"})
    assert response.json()["reason"] == "invalid_request"


def test_third_render_gets_busy_and_health_still_answers(settings):
    slow = replace(settings, timeout_seconds=2.0)
    body = render_body(make_tgz(chart_files(spin=SPIN)))

    async def scenario(client):
        running = [asyncio.create_task(client.post("/render", json=body)) for _ in range(2)]
        await asyncio.sleep(0.8)                       # 둘이 helm 을 돌리는 중이다
        busy = await client.post("/render", json=body)
        health = await client.get("/health")
        finished = await asyncio.gather(*running)
        after = await client.post("/render", json=render_body(make_tgz(chart_files())))
        return busy, health, finished, after

    busy, health, finished, after = run(slow, scenario)
    assert busy.status_code == 429 and busy.json()["reason"] == "busy" and busy.headers["retry-after"] == "1"
    assert health.status_code == 200 and health.json()["helmVersion"]
    assert [r.json()["reason"] for r in finished] == ["timeout", "timeout"]
    assert after.status_code == 200                    # 자리를 돌려받아 다음 요청이 돈다


def test_no_files_are_left_behind(settings):
    post(settings, render_body(make_tgz(chart_files())))
    post(settings, render_body(make_tgz(chart_files(), links={"demo/x": "/etc/passwd"})))
    assert list(settings.work_dir.iterdir()) == []


def test_leftovers_from_a_dead_container_are_cleared(settings):
    settings.work_dir.mkdir(parents=True)
    (settings.work_dir / "tmpOLD").mkdir()
    (settings.work_dir / "tmpOLD" / "values.yaml").write_text("secret: 1\n")
    post(settings, render_body(make_tgz(chart_files())))
    assert list(settings.work_dir.iterdir()) == []


def test_logs_never_hold_values_or_output(settings, caplog):
    secret = "s3cr3t-value-in-values"
    caplog.set_level("DEBUG")
    post(settings, render_body(make_tgz(chart_files()), value_files=[{"name": "v", "content": f"color: {secret}\n"}]))
    failing = make_tgz(chart_files(bad='{{ fail .Values.color }}'))
    response = post(settings, render_body(failing, value_files=[{"name": "v", "content": f"color: {secret}\n"}]))
    assert secret in response.json()["message"]        # 호출한 쪽에는 간다
    assert secret not in caplog.text                    # 로그에는 없다


def test_fetch_rejects_internal_urls_at_the_api(settings):
    async def scenario(client):
        return await client.post("/fetch", json={"repo_url": "http://169.254.169.254/", "chart": "x", "version": "1.0.0"})
    response = run(settings, scenario)
    assert response.status_code == 400 and response.json()["reason"] == "url_rejected"


def test_chart_with_vendored_subchart_renders(settings):
    sub_chart = "apiVersion: v2\nname: sub\nversion: 0.1.0\n"
    sub_template = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: sub-marker\n"
    inner = make_tgz({"sub/Chart.yaml": sub_chart, "sub/templates/c.yaml": sub_template})
    response = post(settings, render_body(make_tgz({**chart_files(), "demo/charts/sub-0.1.0.tgz": inner})))
    assert response.status_code == 200 and b"sub-marker" in response.content
