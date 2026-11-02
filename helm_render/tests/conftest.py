"""진짜 helm 으로 시험한다. 차트는 코드 안에서 만들어 네트워크를 쓰지 않는다."""
from __future__ import annotations

import base64
import gzip
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from kukie_helm_render.config import Settings


@pytest.fixture(scope="session")
def helm_bin() -> str:
    found = shutil.which("helm")
    if found is None:
        pytest.fail("helm 이 없다 — CI 는 Dockerfile 과 같은 버전을 깐다")
    return found


@pytest.fixture
def settings(tmp_path: Path, helm_bin: str) -> Settings:
    return Settings(helm_bin=helm_bin, work_dir=tmp_path / "work")


CHART_YAML = "apiVersion: v2\nname: demo\nversion: 0.1.0\n"
CONFIGMAP = """apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-demo
  namespace: {{ .Release.Namespace }}
data:
  color: {{ .Values.color | quote }}
  kube: {{ .Capabilities.KubeVersion.Version | quote }}
  batch: {{ .Capabilities.APIVersions.Has "example.com/v1" | quote }}
"""


def chart_files(**templates: str) -> dict[str, str]:
    files = {"demo/Chart.yaml": CHART_YAML, "demo/values.yaml": "color: blue\n", "demo/templates/cm.yaml": CONFIGMAP}
    files.update({f"demo/templates/{name}.yaml": text for name, text in templates.items()})
    return files


def make_tgz(files: dict[str, str | bytes], *, links: dict[str, str] | None = None,
             hardlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
        for name, target in (hardlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.LNKTYPE
            info.linkname = target
            tar.addfile(info)
    return buffer.getvalue()


def render_body(archive: bytes, **overrides) -> dict:
    body = {
        "chart_tgz": base64.b64encode(archive).decode(),
        "value_files": [],
        "release_name": "rel",
        "namespace": "team-a",
        "kube_version": "1.33.0",
    }
    body.update(overrides)
    return body
