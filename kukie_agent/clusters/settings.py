"""클러스터 기능의 설정 스위치."""
from __future__ import annotations

import os

ALLOW_LOCAL_ENV = "KUKIE_ALLOW_LOCAL_CLUSTER"


def allow_local_clusters() -> bool:
    """사설·로컬 주소(kind 의 127.0.0.1 등)를 등록할 수 있게 할지.

    기본은 막는다 — 서버가 노트북 밖으로 나가면 그런 주소에는 닿지 않아서 등록해 봐야 실패한다.
    로컬 개발에서만 켠다 (기획 04 §8 "사설 IP 나 localhost 는 MVP 에서 허용 여부를 정한다").
    """
    return os.environ.get(ALLOW_LOCAL_ENV, "").strip() == "1"
