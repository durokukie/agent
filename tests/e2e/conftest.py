import os
import subprocess
import uuid
import warnings

import pytest
import yaml


def _run(
    command: list[str],
    *,
    stdin: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def kubectl(
    context: str,
    *args: str,
    stdin: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["kubectl", "--context", context, *args],
        stdin=stdin,
        check=check,
    )


@pytest.fixture
def kubectl_cli():
    return kubectl


def _cluster_identity(kubeconfig: str) -> tuple[str, str]:
    try:
        cluster = yaml.safe_load(kubeconfig)["clusters"][0]["cluster"]
        return cluster["server"], cluster["certificate-authority-data"]
    except (IndexError, KeyError, TypeError, yaml.YAMLError):
        pytest.fail("kind context의 kubeconfig에서 API server와 CA를 읽을 수 없습니다.")


@pytest.fixture
def e2e_context() -> str:
    context = os.environ.get("KUKIE_E2E_CONTEXT")
    if not context:
        pytest.skip("KUKIE_E2E_CONTEXT가 없어 kind E2E를 건너뜁니다.")
    if not context.startswith("kind-"):
        pytest.fail("KUKIE_E2E_CONTEXT는 kind- context여야 합니다.")

    cluster_name = context.removeprefix("kind-")
    managed_clusters = _run(["kind", "get", "clusters"]).stdout.splitlines()
    if cluster_name not in managed_clusters:
        pytest.fail(f"{context}는 kind가 관리하는 cluster가 아닙니다.")

    selected = _run([
        "kubectl",
        "--context",
        context,
        "config",
        "view",
        "--raw",
        "--minify",
        "--output=yaml",
    ]).stdout
    expected = _run([
        "kind",
        "get",
        "kubeconfig",
        "--name",
        cluster_name,
    ]).stdout
    if _cluster_identity(selected) != _cluster_identity(expected):
        pytest.fail(f"{context}의 API server 또는 CA가 다릅니다.")

    kubectl(context, "cluster-info")
    return context


@pytest.fixture
def e2e_namespace(e2e_context: str):
    namespace = f"kukie-e2e-{uuid.uuid4().hex[:8]}"
    kubectl(e2e_context, "create", "namespace", namespace)
    try:
        yield namespace
    finally:
        deleted = kubectl(
            e2e_context,
            "delete",
            "namespace",
            namespace,
            "--wait=false",
            check=False,
        )
        if deleted.returncode != 0:
            warnings.warn(
                f"namespace cleanup failed: {namespace}: {deleted.stderr.strip()}",
                pytest.PytestWarning,
            )
