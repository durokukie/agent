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


class ClusterChanged(LookupError):
    """방이 승인한 대상과 지금 등록된 클러스터가 다르다 (지문 불일치)."""


@contextmanager
def kubeconfig_for(store: Any, cluster_id: str, *, expect_fingerprint: str | None = None) -> Iterator[Path]:
    """expect_fingerprint 를 주면 방이 복사해 둔 지문과 지금 행을 대조한다 (기획 04 §8).

    "승인한 대상 = 실행 대상" 을 지키는 검사다. 지금은 PATCH 가 지문 바뀜을 막고 있지만,
    방어가 한 곳뿐이면 그 한 곳이 뚫릴 때 조용히 다른 클러스터를 건드리게 된다 (자동 리뷰 지적).
    """
    row = store.get_cluster(cluster_id)
    encrypted = store.cluster_credential(cluster_id)
    if row is None or encrypted is None:
        raise ClusterGone(cluster_id)
    if expect_fingerprint and row.fingerprint != expect_fingerprint:
        raise ClusterChanged(cluster_id)
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


def kubeconfig_or_none(store: Any, cluster_id: str | None, *, expect_fingerprint: str | None = None):
    """방에 등록된 클러스터가 없으면(옛 방·로컬 개발) 아무것도 안 하는 블록을 준다."""
    if cluster_id is None:
        return nullcontext(None)
    return kubeconfig_for(store, cluster_id, expect_fingerprint=expect_fingerprint)
