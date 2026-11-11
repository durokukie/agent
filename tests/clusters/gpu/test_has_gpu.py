"""GPU 여부를 server 에 남기기"""
from __future__ import annotations

import pytest

from gpu_support import FORBIDDEN, _gpu_calls, _gpu_cluster, _result, fixture
from helpers import USER
from kukie_agent.clusters import gpu


@pytest.mark.parametrize(("nodes", "stored", "expected"), [
    ("gpu_nodes.json", None, [True]),            # 처음 알아냈다
    ("gpu_nodes.json", False, [True]),           # GPU 노드가 새로 생겼다
    ("gpu_nodes.json", True, []),                # 그대로면 부르지 않는다 — 대시보드가 띄울 때마다 부른다
    ("nodes.json", None, [False]),
    ("nodes.json", True, [False]),               # GPU 노드가 사라졌다
    ("nodes.json", False, []),
])
def test_개요는_노드를_읽으면_GPU_여부가_바뀐_때만_server에_남긴다(client, fake_server, monkeypatch, nodes, stored, expected):
    cluster = fake_server.add_cluster(has_gpu=stored)
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: _result(fixture(nodes)) if args == gpu.NODES_COMMAND else _gpu_cluster(args))
    assert client.get(f"/clusters/{cluster}/overview", headers=USER).status_code == 200
    assert [body["hasGpu"] for body in _gpu_calls(fake_server)] == expected
    assert fake_server.clusters[cluster]["hasGpu"] == (expected[-1] if expected else stored)


@pytest.mark.parametrize("nodes", [_result(stderr=FORBIDDEN, success=False), _result("not json")])
def test_노드를_못_읽으면_GPU_여부를_남기지_않는다(client, fake_server, monkeypatch, nodes):
    """권한이 없어진 클러스터의 마지막 값을 지우지 않는다."""
    cluster = fake_server.add_cluster(has_gpu=True)
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: nodes if args == gpu.NODES_COMMAND else _gpu_cluster(args))
    assert client.get(f"/clusters/{cluster}/overview", headers=USER).status_code == 200
    assert _gpu_calls(fake_server) == [] and fake_server.clusters[cluster]["hasGpu"] is True


def test_GPU_여부를_남기지_못해도_개요는_200(client, fake_server, monkeypatch, caplog):
    from kukie_agent.server_api import ServerError

    cluster = fake_server.add_cluster()

    async def down(*args, **kwargs):
        raise ServerError(503, "SERVER_UNAVAILABLE", "kukie-server 에 닿지 못했다")

    monkeypatch.setattr("kukie_agent.server_api.update_cluster_gpu", down)
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", lambda args, **k: _gpu_cluster(args))
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"] is not None
    assert f"클러스터 {cluster} 의 GPU 여부를 남기지 못했다: SERVER_UNAVAILABLE" in caplog.text


def test_연결_확인도_노드_목록으로_GPU_여부를_남긴다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(namespace="web")
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(args)
        if args == gpu.NODES_COMMAND:
            return _result(fixture("gpu_nodes.json"))
        return _result("{}" if args[0] == "version" else "yes")

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.post(f"/clusters/{cluster}/test", headers=USER)
    assert r.status_code == 200 and r.json()["status"] == "connected" and r.json()["has_gpu"] is True
    assert seen[-1] == gpu.NODES_COMMAND                                     # 지표는 읽지 않는다 — 개요의 일
    assert [body["hasGpu"] for body in _gpu_calls(fake_server)] == [True]


@pytest.mark.parametrize(("stored", "expected"), [(None, None), (True, None), (False, False)])
def test_연결_확인은_GPU_노드가_없으면_꺼진_GPU_클러스터일_수_있어_개요에_맡긴다(client, fake_server, monkeypatch, stored, expected):
    cluster = fake_server.add_cluster(has_gpu=stored)

    def fake(args, **kwargs):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        return _result("{}" if args[0] == "version" else "yes")

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.post(f"/clusters/{cluster}/test", headers=USER)
    assert r.json()["has_gpu"] is expected and _gpu_calls(fake_server) == []
    assert fake_server.clusters[cluster]["hasGpu"] is stored


def test_연결에_실패하면_노드를_읽지_않고_GPU_여부도_남기지_않는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster()
    seen: list[list[str]] = []

    def fail(args, **kwargs):
        seen.append(args)
        return _result(stderr="dial tcp: i/o timeout", success=False)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fail)
    assert client.post(f"/clusters/{cluster}/test", headers=USER).json()["status"] == "disconnected"
    assert gpu.NODES_COMMAND not in seen and _gpu_calls(fake_server) == []


@pytest.mark.parametrize("stdout", ['{"kind": "List"}', '{"items": ["x"]}', '{"items": null}', "[]"])
def test_노드_목록_모양이_아니면_GPU_여부를_모른다(client, fake_server, monkeypatch, stdout):
    """빈 목록으로 읽으면 server 의 true 를 false 로 덮는다 (PR #119 리뷰). 개요·연결 확인 둘 다."""
    cluster = fake_server.add_cluster(has_gpu=True)

    def fake(args, **kwargs):
        if args == gpu.NODES_COMMAND:
            return _result(stdout)
        return _result("{}" if args[0] == "version" else "yes") if args[0] in ("version", "auth") else _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    assert client.get(f"/clusters/{cluster}/overview", headers=USER).json()["gpu"] is None
    assert client.post(f"/clusters/{cluster}/test", headers=USER).json()["has_gpu"] is None
    assert _gpu_calls(fake_server) == [] and fake_server.clusters[cluster]["hasGpu"] is True


def test_연결_확인은_GPU_여부를_남기지_못했으면_has_gpu를_null로_준다(client, fake_server, monkeypatch):
    """앱은 이 값이 목록과 다를 때 목록을 다시 받는다 — 못 남긴 값을 주면 다시 받아도 아이콘이 그대로다 (PR #119 리뷰)."""
    from kukie_agent.server_api import ServerError

    cluster = fake_server.add_cluster()

    async def missing(*args, **kwargs):
        raise ServerError(404, "NOT_FOUND", "없는 경로")                      # server#82 배포 전

    monkeypatch.setattr("kukie_agent.server_api.update_cluster_gpu", missing)
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: _result(fixture("gpu_nodes.json")) if args == gpu.NODES_COMMAND
                        else _result("{}" if args[0] == "version" else "yes"))
    r = client.post(f"/clusters/{cluster}/test", headers=USER)
    assert r.json()["status"] == "connected" and r.json()["has_gpu"] is None


def test_연결_확인은_server_값과_같으면_부르지_않고_그_값을_준다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(has_gpu=False)
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: _result(fixture("nodes.json")) if args == gpu.NODES_COMMAND
                        else _result("{}" if args[0] == "version" else "yes"))
    assert client.post(f"/clusters/{cluster}/test", headers=USER).json()["has_gpu"] is False
    assert _gpu_calls(fake_server) == []


def test_읽는_사이_kubeconfig가_바뀌었으면_GPU_여부를_남기지_않는다(client, fake_server, monkeypatch):
    """노드를 읽은 연결의 지문을 같이 보낸다 — 그 사이 바뀌었으면 server 가 409 (kukie-server#82 리뷰)."""
    cluster = fake_server.add_cluster(namespace="web")

    def fake(args, **kwargs):
        if args == gpu.NODES_COMMAND:
            fake_server.clusters[cluster]["fingerprint"] = "새-지문"           # 읽는 사이 kubeconfig 를 바꿨다
            return _result(fixture("gpu_nodes.json"))
        return _result("{}" if args[0] == "version" else "yes")

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.post(f"/clusters/{cluster}/test", headers=USER)
    assert r.json()["status"] == "connected" and r.json()["has_gpu"] is None
    assert [body["fingerprint"] for body in _gpu_calls(fake_server)] == [f"fp-{cluster}"]
    assert fake_server.clusters[cluster]["hasGpu"] is None


def test_연결_정보에_지문이_없으면_GPU_여부를_남기지_않는다(client, fake_server, monkeypatch, caplog):
    """server 가 늘 409 로 거절할 호출이다 (PR #119 리뷰)."""
    cluster = fake_server.add_cluster(fingerprint="")
    fake_server.clusters[cluster]["fingerprint"] = ""
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: _result(fixture("gpu_nodes.json")) if args == gpu.NODES_COMMAND
                        else _result("{}" if args[0] == "version" else "yes"))
    assert client.post(f"/clusters/{cluster}/test", headers=USER).json()["has_gpu"] is None
    assert _gpu_calls(fake_server) == [] and "지문이 없다" in caplog.text

    fake_server.clusters[cluster]["hasGpu"] = True                         # server 값과 같으면 지문 없이도 그 값을 준다
    assert client.post(f"/clusters/{cluster}/test", headers=USER).json()["has_gpu"] is True
