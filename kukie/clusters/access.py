"""등록된 클러스터로 kubectl 을 실행할 준비 (기획 04 §8 "실행 방식").

요청마다 자격증명을 풀어 임시 kubeconfig 파일을 만들고, 블록을 벗어나면 지운다.
푼 값은 이 모듈과 그 파일 밖으로 나가지 않는다 — 로그·LLM 입력·승인 카드에 실리지 않는다.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from kukie.clusters import crypto, runtime


class ClusterGone(LookupError):
    """방이 가리키는 클러스터가 없다 — 지워졌다."""


@contextmanager
def kubeconfig_for(store: Any, cluster_id: str) -> Iterator[Path]:
    row = store.get_cluster(cluster_id)
    encrypted = store.cluster_credential(cluster_id)
    if row is None or encrypted is None:
        raise ClusterGone(cluster_id)
    credential = crypto.decrypt(encrypted)      # CredentialUnreadable 은 부르는 쪽이 안내로 바꾼다
    body = runtime.build(
        context_name=row.context_name,
        api_server=row.api_server,
        ca_data=row.ca_data,
        namespace=row.default_namespace,
        credential=credential,
        insecure=row.insecure,
    )
    with runtime.temporary_kubeconfig(body) as path:
        yield path


def kubeconfig_or_none(store: Any, cluster_id: str | None):
    """방에 등록된 클러스터가 없으면(옛 방·로컬 개발) 아무것도 안 하는 블록을 준다."""
    return nullcontext(None) if cluster_id is None else kubeconfig_for(store, cluster_id)
