"""새 대화에서 고를 namespace 목록 (DURO-228). `GET /clusters/{id}/namespaces` 가 쓴다.

`kubectl get namespaces -o json` 한 번이다. 클러스터 전체를 읽으므로 full 권한(클러스터 관리자·대부분의 kubeconfig)에서만 읽힌다 —
limited(namespace 범위 Edit)면 Forbidden 이고, 그때 화면은 등록한 namespace 하나만 고르게 한다.
시스템 namespace(kube-*)와 지우는 중(Terminating)인 것은 고를 수 없으므로 뺀다.
"""
from __future__ import annotations

import json
import re
from typing import Any

#: 부르는 쪽이 `-n` 을 붙이지 않는다 (클러스터 범위)
LIST_COMMAND: list[str] = ["get", "namespaces", "-o", "json"]
SYSTEM_PREFIX = "kube-"
#: namespace 이름(DNS label). URL 에서 받은 값을 kubectl 인자로 넘기기 전에 본다
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def valid(name: str) -> bool:
    """kubectl `-n` 에 넘겨도 되는 이름인가. 읽기만 하므로 kube-* 도 된다(개요에서 kube-system 을 볼 수 있다)."""
    return bool(_NAME.fullmatch(name))


def names(stdout: str) -> list[str]:
    """NamespaceList 의 고를 수 있는 이름들 (이름순). 모양이 다르면 ValueError."""
    body: Any = json.loads(stdout)
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ValueError("items 가 목록이 아니다")
    picked: set[str] = set()
    for item in items:
        if not isinstance(item, dict):   # 다른 목록 파서(gpu·overview._items)처럼 건너뛴다 — 한 줄 때문에 목록 전체를 막지 않게
            continue
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        name = metadata.get("name") if isinstance(metadata, dict) else None
        phase = status.get("phase") if isinstance(status, dict) else None
        if not isinstance(name, str) or not name or name.startswith(SYSTEM_PREFIX) or phase == "Terminating":
            continue
        picked.add(name)
    return sorted(picked)
