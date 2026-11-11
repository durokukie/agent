"""CI 의 test · e2e job 을 고르는 .github/scripts/ci_scope.py — 잘못 고르면 테스트가 소리 없이 빠진다."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "ci_scope.py"
spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
ci_scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_scope)

BOTH = {"test": True, "e2e": True}
UNIT_ONLY = {"test": True, "e2e": False}
NEITHER = {"test": False, "e2e": False}


@pytest.mark.parametrize("paths, expected", [
    (["README.md"], NEITHER),
    (["docs/review-checklist.md", "CLAUDE.md"], NEITHER),
    (["helm_render/kukie_helm_render/app.py"], NEITHER),
    ([".github/workflows/claude-review.yml"], NEITHER),
    (["kukie_agent/clusters/gpu.py", "tests/clusters/gpu/test_nodes.py"], UNIT_ONLY),
    (["kukie_agent/clusters/overview.py"], UNIT_ONLY),
    (["kukie_agent/clusters_api.py"], BOTH),
    (["tests/fixtures/overview/nodes.json", "README.md"], UNIT_ONLY),
    (["kukie_agent/guardrail/hook.py"], BOTH),
    (["kukie_agent/gitops/route.py"], BOTH),
    (["kukie_agent/manifests/change.py"], BOTH),
    (["kukie_agent/prompts/base.md"], BOTH),
    (["tests/conftest.py"], BOTH),
    (["tests/helpers.py"], BOTH),
    (["tests/e2e/test_guardrail_cluster.py"], BOTH),
    (["tests/e2e/fake-gpu/README.md"], BOTH),
    (["pyproject.toml"], BOTH),
    (["uv.lock"], BOTH),
    ([".github/workflows/ci.yml"], BOTH),
    ([".github/scripts/ci_scope.py"], BOTH),
    (["Dockerfile"], BOTH),
    (["새로_생긴_파일.txt"], BOTH),
    (["README.md", "kukie_agent/server.py"], BOTH),
    ([], BOTH),
])
def test_바뀐_경로로_돌릴_job을_고른다(paths, expected):
    assert ci_scope.scope(paths) == expected


def _output(monkeypatch, tmp_path, event: str) -> str:
    path = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    ci_scope.main()
    return path.read_text(encoding="utf-8")


def test_push는_바뀐_파일과_상관없이_둘_다_돌린다(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_scope, "changed_files", lambda: ["README.md"])
    assert _output(monkeypatch, tmp_path, "push") == "test=true\ne2e=true\n"


def test_PR은_바뀐_파일로_고른다(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_scope, "changed_files", lambda: ["README.md"])
    assert _output(monkeypatch, tmp_path, "pull_request") == "test=false\ne2e=false\n"


def test_바뀐_파일을_못_읽으면_둘_다_돌린다(monkeypatch, tmp_path):
    def broken():
        raise subprocess.CalledProcessError(128, "git")
    monkeypatch.setattr(ci_scope, "changed_files", broken)
    assert _output(monkeypatch, tmp_path, "pull_request") == "test=true\ne2e=true\n"
