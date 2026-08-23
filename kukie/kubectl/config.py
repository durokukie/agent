"""kubeconfig 조회 — 세션 시작 시 "어느 클러스터·네임스페이스인가"를 읽는다.

Kukie 는 클러스터에 직접 접속하지 않는다. 접속은 kubectl 이 kubeconfig 로 한다.
여기서 읽는 건 현재 context 이름과 namespace 뿐이다 — 주소·인증서·토큰은 읽지 않는다.
읽은 값은 Deps 에 고정되고, 이후 대화로는 바뀌지 않는다.
"""
from __future__ import annotations

import subprocess


class KubeconfigError(RuntimeError):
    """kubectl 이 없거나 kubeconfig 에 현재 context 가 없을 때."""


def _kubectl_config(*args: str) -> str:
    proc = subprocess.run(["kubectl", "config", *args], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise KubeconfigError(proc.stderr.strip() or "kubectl config 실패")
    return proc.stdout.strip()


def read_kubeconfig() -> tuple[str, str]:
    """(현재 context, 현재 namespace). namespace 가 비어 있으면 kubectl 기본값인 'default'."""
    context = _kubectl_config("current-context")
    if not context:
        raise KubeconfigError("kubeconfig 에 current-context 가 없다")
    namespace = _kubectl_config("view", "--minify", "-o", "jsonpath={..namespace}") or "default"
    return context, namespace
