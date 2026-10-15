"""등록된 클러스터로 kubectl 을 실행할 준비 (기획 04 §8 "실행 방식").

자격증명은 Kukie API 서버(kukie-server)가 암호화해 보관한다 (DURO-113). agent 는 실행 직전에 내부 API
`GET /internal/clusters/{id}/connection` 으로 평문을 받아 임시 kubeconfig 를 만들고, 블록을 벗어나면 지운다.
받은 값은 이 모듈과 그 파일 밖으로 나가지 않는다 — 로그·LLM 입력·승인 카드에 실리지 않는다.

"이 사용자가 이 클러스터를 써도 되나" 와 "승인한 대상 = 실행 대상"(방이 복사해 둔 지문 대조)은 server 가
그 호출에서 판단한다 — 못 보는 클러스터는 404 NOT_FOUND, 지문이 다르면 409 CLUSTER_CHANGED (ServerError).

받기(connection)와 파일 만들기(kubeconfig)를 나눠 둔 이유: 대화 경로는 권한 판단을 **방 잠금을 잡기 전에**
끝내야 하고(잠금 검사와 획득 사이에 await 가 있으면 안 된다), 파일은 실행하는 동안만 있어야 한다.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kukie_agent import server_api
from kukie_agent.auth import User
from kukie_agent.clusters import runtime


async def connection(
    cluster_id: str, user: User, *, expected_fingerprint: str | None = None,
) -> server_api.ClusterConnection:
    """사용자 토큰으로 연결 정보를 받는다. 권한·지문 검사는 server 가 한다."""
    return await server_api.cluster_connection(cluster_id, user, expected_fingerprint=expected_fingerprint)


@contextmanager
def kubeconfig(conn: server_api.ClusterConnection) -> Iterator[Path]:
    """받은 연결 정보로 임시 kubeconfig 를 만들고 경로를 준다. 블록을 벗어나면 지운다."""
    body = runtime.build(
        context_name=conn.context_name,
        api_server=conn.api_server,
        ca_data=conn.ca_data,
        namespace=conn.namespace,
        credential=conn.credential,
        insecure=conn.insecure,
    )
    with runtime.temporary_kubeconfig(body) as path:
        yield path
