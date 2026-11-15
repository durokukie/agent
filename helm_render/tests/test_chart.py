"""차트 풀기와 스키마 검사 — helm 을 돌리기 전에 막는 것."""
from __future__ import annotations

import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import chart_files, make_tgz
from kukie_helm_render.chart import ChartRejected, check_schema, unpack_chart


def rejected(archive: bytes, tmp_path: Path, settings, reason: str) -> None:
    with pytest.raises(ChartRejected) as caught:
        unpack_chart(archive, tmp_path / "out", settings)
    assert caught.value.reason == reason


def test_unpacks_a_normal_chart(tmp_path, settings):
    root = unpack_chart(make_tgz(chart_files()), tmp_path / "out", settings)
    assert (root / "Chart.yaml").is_file()
    assert (root / "templates" / "cm.yaml").is_file()


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside", "values.yaml"])
def test_symlink_is_rejected_wherever_it_points(tmp_path, settings, target):
    archive = make_tgz(chart_files(), links={"demo/templates/link.yaml": target})
    rejected(archive, tmp_path, settings, "chart_has_link")
    assert not (tmp_path / "out" / "demo" / "templates" / "link.yaml").exists()


def test_hardlink_is_rejected(tmp_path, settings):
    rejected(make_tgz(chart_files(), hardlinks={"demo/x": "/etc/passwd"}), tmp_path, settings, "chart_has_link")


@pytest.mark.parametrize("name", ["demo/../../evil.yaml", "/etc/evil.yaml", "demo/templates/..\\evil.yaml"])
def test_paths_outside_the_chart_are_rejected(tmp_path, settings, name):
    rejected(make_tgz({**chart_files(), name: "x"}), tmp_path, settings, "chart_path_outside")
    assert not (tmp_path / "evil.yaml").exists()


def test_two_top_level_folders_are_rejected(tmp_path, settings):
    rejected(make_tgz({**chart_files(), "other/x": "x"}), tmp_path, settings, "not_a_chart")


def test_missing_chart_yaml_is_rejected(tmp_path, settings):
    rejected(make_tgz({"demo/values.yaml": "a: 1\n"}), tmp_path, settings, "not_a_chart")


def test_duplicate_entry_does_not_overwrite(tmp_path, settings):
    import io
    import tarfile
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for content in (b"apiVersion: v2\n", b"second\n"):
            info = tarfile.TarInfo("demo/Chart.yaml")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    rejected(buffer.getvalue(), tmp_path, settings, "chart_unreadable")


def test_gzip_bomb_stops_while_reading(tmp_path, settings):
    small = replace(settings, max_unpacked_bytes=1024 * 1024)
    archive = make_tgz({**chart_files(), "demo/big.bin": b"\0" * (8 * 1024 * 1024)})
    assert len(archive) < 64 * 1024                    # 압축하면 작다 — 받은 크기로는 못 잡는다
    rejected(archive, tmp_path, small, "chart_too_large")


def test_too_many_files_is_rejected(tmp_path, settings):
    small = replace(settings, max_chart_files=5)
    files = {**chart_files(), **{f"demo/f{i}": "x" for i in range(10)}}
    rejected(make_tgz(files), tmp_path, small, "chart_too_large")


def test_large_tgz_is_rejected_before_reading(tmp_path, settings):
    rejected(b"x" * 100, tmp_path, replace(settings, max_chart_bytes=10), "chart_too_large")


def test_garbage_is_rejected(tmp_path, settings):
    rejected(b"not a gzip", tmp_path, settings, "chart_unreadable")


def test_truncated_gzip_is_rejected(tmp_path, settings):
    archive = make_tgz({**chart_files(), "demo/x": "y" * 10000})
    rejected(archive[:len(archive) // 2], tmp_path, settings, "chart_unreadable")


@pytest.mark.parametrize("schema", [
    {"$ref": "file:///etc/hosts"},
    {"$ref": "https://example.com/x.json"},
    {"$ref": "other.json"},
    {"properties": {"a": {"$ref": "http://169.254.169.254/latest/meta-data"}}},
    {"allOf": [{"$dynamicRef": "https://example.com/x"}]},
    {"$recursiveRef": "x.json"},
    {"$schema": "http://127.0.0.1:9/meta.json"},
])
def test_external_schema_reference_is_rejected(schema):
    with pytest.raises(ChartRejected) as caught:
        check_schema(json.dumps(schema).encode(), "values.schema.json")
    assert caught.value.reason == "schema_external_ref"


def test_duplicate_key_cannot_hide_a_reference():
    text = b'{"$ref": "file:///etc/hosts", "$ref": "#/definitions/a"}'
    with pytest.raises(ChartRejected):
        check_schema(text, "s")


def test_escaped_key_cannot_hide_a_reference():
    with pytest.raises(ChartRejected):
        check_schema(b'{"\\u0024ref": "file:///etc/hosts"}', "s")


@pytest.mark.parametrize("schema", [
    {"$schema": "http://json-schema.org/draft-07/schema#", "$ref": "#/definitions/a", "definitions": {"a": {}}},
    {"$schema": "https://json-schema.org/draft/2020-12/schema", "$dynamicRef": "#node"},
    {"properties": {"$ref": {"type": "string"}}},      # 값 이름이 $ref 인 칸은 참조가 아니다
])
def test_local_references_are_allowed(schema):
    check_schema(json.dumps(schema).encode(), "values.schema.json")


def test_too_deep_schema_is_rejected():
    with pytest.raises(ChartRejected) as caught:
        check_schema(b"[" * 200000 + b"]" * 200000, "s")
    assert caught.value.reason == "schema_unreadable"


def test_schema_in_chart_is_checked(tmp_path, settings):
    archive = make_tgz({**chart_files(), "demo/values.schema.json": '{"$ref": "file:///etc/hosts"}'})
    rejected(archive, tmp_path, settings, "schema_external_ref")


def test_schema_in_nested_subchart_is_checked(tmp_path, settings):
    inner = make_tgz({"sub/Chart.yaml": "apiVersion: v2\nname: sub\nversion: 0.1.0\n",
                      "sub/values.schema.json": '{"$ref": "https://example.com/x.json"}'})
    outer = make_tgz({**chart_files(), "demo/charts/sub-0.1.0.tgz": inner})
    rejected(outer, tmp_path, settings, "schema_external_ref")


def test_symlink_in_nested_subchart_is_rejected(tmp_path, settings):
    inner = make_tgz({"sub/Chart.yaml": "apiVersion: v2\nname: sub\nversion: 0.1.0\n"},
                     links={"sub/templates/x.yaml": "/etc/passwd"})
    rejected(make_tgz({**chart_files(), "demo/charts/sub-0.1.0.tgz": inner}), tmp_path, settings, "chart_has_link")


def test_subchart_nesting_is_limited(tmp_path, settings):
    archive = make_tgz({"a/Chart.yaml": "apiVersion: v2\nname: a\nversion: 0.1.0\n"})
    for depth in range(6):
        archive = make_tgz({"a/Chart.yaml": "apiVersion: v2\nname: a\nversion: 0.1.0\n", "a/charts/a.tgz": archive})
    rejected(archive, tmp_path, settings, "chart_too_deep")


def test_nested_gzip_bomb_counts_against_the_budget(tmp_path, settings):
    small = replace(settings, max_unpacked_bytes=2 * 1024 * 1024)
    inner = make_tgz({"sub/big.bin": b"\0" * (8 * 1024 * 1024)})
    rejected(make_tgz({**chart_files(), "demo/charts/sub.tgz": inner}), tmp_path, small, "chart_too_large")
