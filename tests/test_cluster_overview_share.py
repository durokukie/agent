"""같은 연결의 개요를 잠깐 나눠 쓴다 — 보는 사람 수만큼 kubectl 이 돌지 않게 (clusters_api._shared_overview)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from helpers import OTHER, USER
from kukie_agent import clusters_api, server
from kukie_agent.kubectl import KubectlResult


@pytest.fixture(autouse=True)
def _share_on(monkeypatch):
    monkeypatch.setattr(clusters_api, "OVERVIEW_SHARE_TTL", 10)


@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)


def _counter():
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        await asyncio.sleep(0)
        return {"n": calls["n"]}

    return calls, compute


def test_진행_중인_개요는_같이_기다린다():
    calls, compute = _counter()

    async def main():
        return await asyncio.gather(*(clusters_api._shared_overview("k", compute) for _ in range(5)))

    assert asyncio.run(main()) == [{"n": 1}] * 5 and calls["n"] == 1


def test_막_끝난_개요는_TTL_안에서만_다시_쓴다():
    calls, compute = _counter()
    now = [0.0]

    async def main():
        first = await clusters_api._shared_overview("k", compute, clock=lambda: now[0])
        await asyncio.sleep(0)                                   # 끝난 시각을 적는 콜백이 돈다
        now[0] = clusters_api.OVERVIEW_SHARE_TTL - 1
        again = await clusters_api._shared_overview("k", compute, clock=lambda: now[0])
        now[0] = 2 * clusters_api.OVERVIEW_SHARE_TTL
        fresh = await clusters_api._shared_overview("k", compute, clock=lambda: now[0])
        return first, again, fresh

    assert asyncio.run(main()) == ({"n": 1}, {"n": 1}, {"n": 2})
    assert calls["n"] == 2


def test_실패는_나눠_쓰지_않고_다음_요청은_새로_읽는다():
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        await asyncio.sleep(0)
        if calls["n"] == 1:
            raise HTTPException(502, {"code": "CLUSTER_UNREACHABLE"})
        return {"ok": True}

    async def main():
        results = await asyncio.gather(clusters_api._shared_overview("k", compute),
                                       clusters_api._shared_overview("k", compute), return_exceptions=True)
        await asyncio.sleep(0)
        return results, await clusters_api._shared_overview("k", compute)

    results, after = asyncio.run(main())
    assert all(isinstance(r, HTTPException) and r.status_code == 502 for r in results)   # 같이 기다린 요청은 같은 실패
    assert after == {"ok": True} and calls["n"] == 2


def test_연결이_다르거나_지문이_없으면_나눠_쓰지_않는다():
    calls, compute = _counter()

    async def main():
        a = await clusters_api._shared_overview("c:fp1:ns", compute)
        b = await clusters_api._shared_overview("c:fp2:ns", compute)   # kubeconfig 를 바꿨다
        c = await clusters_api._shared_overview(None, compute)
        return a, b, c

    assert asyncio.run(main()) == ({"n": 1}, {"n": 2}, {"n": 3})


def test_기다리던_요청이_끊겨도_한_번은_끝까지_돌아_다음_요청이_쓴다():
    calls, compute = _counter()

    async def main():
        waiter = asyncio.ensure_future(clusters_api._shared_overview("k", compute))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return await clusters_api._shared_overview("k", compute)

    assert asyncio.run(main()) == {"n": 1} and calls["n"] == 1


def test_개요_엔드포인트는_권한을_요청마다_보고_kubectl_결과만_나눠_쓴다(client, fake_server, monkeypatch):
    fake_server.add_member("t-1", "u-1", "ADMIN")
    fake_server.add_member("t-1", "u-2", "MEMBER")
    shared = fake_server.add_cluster(owner="u-1", team_id="t-1")
    private = fake_server.add_cluster(owner="u-1")
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(args)
        return KubectlResult(command="kubectl …", stdout='{"items": []}', stderr="", success=True, exit_code=0)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    first = client.get(f"/clusters/{shared}/overview", headers=USER)
    runs = len(seen)
    second = client.get(f"/clusters/{shared}/overview", headers=OTHER)   # 같은 팀 다른 사람 — 같은 결과
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len(seen) == runs

    assert client.get(f"/clusters/{private}/overview", headers=USER).status_code == 200
    assert client.get(f"/clusters/{private}/overview", headers=OTHER).status_code == 404   # 나눠 쓴 결과가 있어도 권한이 먼저
