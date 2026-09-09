"""실행 직전에 임시 kubeconfig 파일을 만든다 (기획 04 §8 "실행 방식").

원칙 셋.
  - 서버의 기본 kubeconfig 나 공용 환경변수는 건드리지 않는다. 요청마다 자기 파일을 쓴다
  - 파일은 소유자만 읽을 수 있게(0600) 만들고, 블록을 벗어나면 지운다
  - 자격증명은 이 파일 밖으로 나가지 않는다 — 로그·LLM 입력·승인 카드·오류 메시지에 싣지 않는다
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

CLUSTER_NAME = "kukie"
USER_NAME = "kukie"


def build(
    *,
    context_name: str,
    api_server: str,
    ca_data: str | None,
    namespace: str,
    credential: dict[str, str],
    insecure: bool = False,
) -> str:
    """등록된 값으로 kubeconfig 본문을 만든다. exec 도 파일 경로도 쓰지 않는다."""
    cluster: dict[str, object] = {"server": api_server}
    if ca_data:
        cluster["certificate-authority-data"] = ca_data
    if insecure:
        cluster["insecure-skip-tls-verify"] = True

    user: dict[str, object] = {}
    if "token" in credential:
        user["token"] = credential["token"]
    else:
        user["client-certificate-data"] = credential["client_certificate_data"]
        user["client-key-data"] = credential["client_key_data"]

    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": CLUSTER_NAME, "cluster": cluster}],
            "users": [{"name": USER_NAME, "user": user}],
            "contexts": [
                {
                    "name": context_name,
                    "context": {
                        "cluster": CLUSTER_NAME,
                        "user": USER_NAME,
                        "namespace": namespace,
                    },
                }
            ],
            "current-context": context_name,
        },
        sort_keys=False,
    )


@contextmanager
def temporary_kubeconfig(body: str) -> Iterator[Path]:
    """본문을 0600 임시 파일에 쓰고 경로를 준다. 블록을 벗어나면 반드시 지운다."""
    handle, name = tempfile.mkstemp(prefix="kukie-kubeconfig-", suffix=".yaml")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(body)
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)
