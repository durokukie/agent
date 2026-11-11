"""개요 엔드포인트의 gpu 칸과 시간 예산 (DURO-118)"""
from __future__ import annotations

import pytest

from gpu_support import (
    FORBIDDEN,
    L40S_1,
    L40S_4,
    NO_POOL_CRD,
    _gpu_calls,
    _gpu_cluster,
    _list,
    _result,
    _vllm_pending,
    fixture,
)
from helpers import USER
from kukie_agent import clusters_api
from kukie_agent.clusters import gpu


def test_GPU_클러스터는_노드_파드_지표_순으로_읽어_gpu_칸을_채운다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(namespace="shop")
    seen: list[tuple[list[str], int]] = []

    def fake(args, *, context, timeout=30, kubeconfig=None, **kwargs):
        seen.append((args, timeout))
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    body = r.json()["gpu"]
    assert [n["node"] for n in body["nodes"]] == [L40S_1, L40S_4]
    assert body["nodes"][0]["utilization_pct"] == 87 and body["nodes"][1]["utilization_pct"] == 87
    assert [(s["model"], s["pod"], s["node"]) for s in body["serving"]] == [
        ("openai/gpt-oss-20b", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", L40S_1)]   # 지표를 못 읽은 qwen-0 은 빠진다
    assert body["benchmarks"] == []
    assert [args for args, _ in seen[4:8]] == [gpu.NODES_COMMAND, gpu.PODS_COMMAND, gpu.NODE_POOLS_COMMAND,
                                               gpu.SERVED_MODELS_COMMAND]
    assert body["served_models"] is None                              # 이 가짜 클러스터에는 CRD 가 없다
    assert body["node_pools"] == []                                   # NodePool CRD 가 없으면 "GPU 풀 없음" — 앱이 설치를 안내한다
    assert sorted(args for args, _ in seen[8:]) == sorted([           # 지표는 동시에 읽는다
        gpu.proxy_command("gpu-operator", "nvidia-dcgm-exporter-abcde", 9400),
        gpu.proxy_command("gpu-operator", "nvidia-dcgm-exporter-fghij", 9400),
        gpu.proxy_command("llm", "qwen-0", 8000),
        gpu.proxy_command("llm", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", 8080),
    ])
    # 지표는 파드마다 한 번이라 더 짧게 끊는다
    assert {timeout for args, timeout in seen if args[1] == "--raw"} == {clusters_api.GPU_METRICS_TIMEOUT}
    assert clusters_api.GPU_METRICS_TIMEOUT < clusters_api.OVERVIEW_TIMEOUT


def test_노드를_못_읽으면_gpu만_null이고_파드_지표는_읽지_않는다(client, fake_server, monkeypatch, caplog):
    cluster = fake_server.add_cluster()
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(args)
        return _result(stderr=FORBIDDEN, success=False) if args == gpu.NODES_COMMAND else _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    with caplog.at_level("DEBUG", logger="kukie_agent.clusters_api"):
        r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    assert r.json()["gpu"] is None and len(r.json()["workloads"]) == 7
    assert seen[-1] == gpu.NODES_COMMAND
    assert "GPU 조회를 건너뛴다" in caplog.text and "arn:aws" not in caplog.text   # 원문은 남기지 않는다


def test_GPU가_없다고_이미_안_클러스터는_간격_안에서는_파드를_읽지_않는다(client, fake_server, monkeypatch):
    """GPU 없는 클러스터마다 개요 때 파드 조회가 늘지 않게 — 한 번 찾은 뒤 GPU_ABSENT_RECHECK 동안은 노드 목록에서 멈춘다."""
    cluster = fake_server.add_cluster(has_gpu=False)
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(args)
        return _result(fixture("nodes.json")) if args == gpu.NODES_COMMAND else _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    assert client.get(f"/clusters/{cluster}/overview", headers=USER).status_code == 200
    assert seen.count(gpu.PODS_COMMAND) == 1                  # 이 프로세스에서 처음 — 한 번 찾았다
    seen.clear()
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"] is None
    assert gpu.PODS_COMMAND not in seen and not any(args[1] == "--raw" for args in seen)


def test_GPU가_없다고_남은_클러스터도_간격마다_한_번은_기다리는_파드를_다시_찾아_GPU_클러스터로_바꾼다():
    """노드 그룹은 0대 그대로 GPU 워크로드를 새로 올린 경우 (agent#121 Codex 리뷰)."""
    now = [0.0]
    seen: list[list[str]] = []
    pods = ["{}"]

    def run(args, timeout):
        seen.append(args)
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.NODE_POOLS_COMMAND:
            return _result(stderr=NO_POOL_CRD, success=False)
        return _result(pods[0])

    def call(key="c:fp:ns"):
        return clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=key, known=False, clock=lambda: now[0])

    assert call() == (None, False) and gpu.PODS_COMMAND in seen   # 이 프로세스에서 처음 — 바로 한 번 찾는다
    pods[0] = _list(_vllm_pending())                          # 0대인 채 vLLM 을 올렸다
    seen.clear()
    now[0] += clusters_api.GPU_ABSENT_RECHECK - 1
    assert call() == (None, False) and gpu.PODS_COMMAND not in seen   # 간격 안 — 노드만 본다
    now[0] += 1
    body, has_gpu = call()                                    # 간격이 지났다 — 찾아서 GPU 클러스터로 바뀐다
    assert has_gpu is True and body["waiting"][0]["pod"] == "vllm-gpt-oss-20b-b897b5d97-kkjsq"
    seen.clear()
    assert call(None) == (None, False) and gpu.PODS_COMMAND not in seen   # 지문 없는 false 는 찾지 않는다


def test_파드_조회를_보냈으면_못_읽었어도_간격을_센다():
    """GPU 읽기 RBAC 가 없어 모든 파드·namespace 목록이 거부되는 곳이 개요마다 다시 묻지 않게 (agent#121 리뷰)."""
    now = [0.0]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        return _result(stderr=FORBIDDEN, success=False)

    def call():
        return clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="c:fp:ns", known=None, clock=lambda: now[0])

    assert call() == (None, None) and gpu.PODS_COMMAND in seen
    seen.clear()
    now[0] += 20
    assert call() == (None, None) and seen == [gpu.NODES_COMMAND]


@pytest.mark.parametrize("stored", [None, True])
def test_GPU_노드가_0대여도_GPU를_기다리는_파드가_있으면_꺼진_GPU_클러스터다(client, fake_server, monkeypatch, stored):
    """GPU 노드 그룹을 0대로 내려 둔 클러스터 (우리 kukie-gpu). 지표는 읽지 않는다."""
    cluster = fake_server.add_cluster(has_gpu=stored)
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(args)
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(_list(_vllm_pending()))
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    assert r.json()["gpu"] == {"nodes": [], "serving": [], "benchmarks": [], "served_models": None, "node_pools": [], "counter": None,
        "waiting": [{"namespace": "llm", "pod": "vllm-gpt-oss-20b-b897b5d97-kkjsq", "gpus": 1, "phase": "Pending"}]}
    assert not any(args[1] == "--raw" for args in seen)
    assert [body["hasGpu"] for body in _gpu_calls(fake_server)] == ([True] if stored is None else [])
    assert fake_server.clusters[cluster]["hasGpu"] is True


@pytest.mark.parametrize("broken", ["not json", '{"items": {}}'])
def test_노드나_파드_목록이_깨져도_개요는_200(client, fake_server, monkeypatch, broken):
    cluster = fake_server.add_cluster()
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: _result(broken) if args == gpu.NODES_COMMAND else _gpu_cluster(args))
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"] is None

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: _result(broken) if args == gpu.PODS_COMMAND else _gpu_cluster(args))
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    assert [n["node"] for n in r.json()["gpu"]["nodes"]] == [L40S_1, L40S_4] and r.json()["gpu"]["serving"] == []


def test_GPU_조회는_시간_예산을_넘기면_남은_지표를_건너뛴다(monkeypatch):
    """지표 파드가 다 매달려도 개요 슬롯을 GPU_BUDGET 이상 잡지 않는다 (PR #118 리뷰).

    가짜 시계는 호출마다 시간이 쌓인다고 보므로 지표를 차례로 돌린다 — 동시에 도는 것은 엔드포인트 테스트가 본다.
    """
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    seen: list[tuple[list[str], int]] = []

    def run(args, timeout):
        seen.append((args, timeout))
        now[0] += {"nodes": 2, "pods": 3}.get(args[1], 0)
        if args[1] == "--raw":
            now[0] += timeout                                                # 지표는 시간 제한까지 매달린다
            return _result(stderr="timed out", success=False)
        return _gpu_cluster(args)

    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert has_gpu is True
    assert [r["node"] for r in body["nodes"]] == [L40S_1, L40S_4] and body["serving"] == []
    raw = [timeout for args, timeout in seen if args[1] == "--raw"]
    assert all(1 <= t <= clusters_api.GPU_METRICS_TIMEOUT for t in raw)
    assert now[0] <= clusters_api.GPU_BUDGET                                 # 넘기면 건너뛴다
    assert len(raw) < 4


def test_노드_조회에서_예산을_다_쓰면_파드는_읽지_않는다():
    now = [0.0]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        now[0] += clusters_api.GPU_BUDGET
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert seen == [gpu.NODES_COMMAND] and len(body["nodes"]) == 2


def test_지표_값이_무한대여도_개요는_200(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster()

    def fake(args, **kwargs):
        if args[1] == "--raw":
            return _result('DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-A"} +Inf\nvllm:num_requests_running{model_name="m"} +Inf')
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"]["nodes"][0]["memory_used_mib"] is None
