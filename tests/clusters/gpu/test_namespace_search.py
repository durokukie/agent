"""GPU 파드를 namespace 마다 찾는 탐색과 그 기억"""
from __future__ import annotations

import json

import pytest

from gpu_support import (
    CPU,
    FORBIDDEN,
    L40S_1,
    _gpu_calls,
    _gpu_cluster,
    _list,
    _namespaces,
    _result,
    _vllm_pending,
    fixture,
)
from helpers import USER
from kukie_agent import clusters_api
from kukie_agent.clusters import gpu
from kukie_agent.kubectl import KubectlResult


def test_GPU_노드가_없고_namespace마다_찾아도_없으면_모른다(client, fake_server, monkeypatch):
    """못 본 namespace 가 있을 수 있어 false 로 덮지 않는다 — 꺼진 GPU 클러스터를 GPU 없음으로 잘못 남기지 않게."""
    cluster = fake_server.add_cluster(has_gpu=True)

    def fake(args, **kwargs):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("llm"))
        if args[:3] == ["get", "pods", "-n"] and args[3] == "llm":
            return _result(stderr="context deadline exceeded", success=False)
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"] is None
    assert _gpu_calls(fake_server) == [] and fake_server.clusters[cluster]["hasGpu"] is True


def test_GPU를_기다리는_파드는_끝나지_않았고_돌지_않는_것만_센다():
    running = dict(_vllm_pending(name="run"), status={"phase": "Running"})
    done = dict(_vllm_pending(name="done"), status={"phase": "Succeeded"})
    cpu = {"metadata": {"name": "web", "namespace": "a"}, "status": {"phase": "Pending"},
           "spec": {"containers": [{"name": "web", "resources": {"limits": {"cpu": "1"}}}]}}
    two = {"metadata": {"name": "train", "namespace": "a"}, "status": {"phase": "Pending"},
           "spec": {"containers": [{"name": "a", "resources": {"requests": {"amd.com/gpu": "1"}}},
                                   {"name": "b", "resources": {"limits": {"nvidia.com/gpu": "2"}}}]}}
    rows = gpu.waiting_pods(_list(_vllm_pending(), running, done, cpu, two))
    assert [(r["namespace"], r["pod"], r["gpus"]) for r in rows] == [("a", "train", 3), ("llm", "vllm-gpt-oss-20b-b897b5d97-kkjsq", 1)]


def test_GPU_노드가_0대인_라운드는_namespace_기억의_GPU_namespace를_지우지_않는다():
    items = json.loads(fixture("gpu_pods.json"))["items"]
    vllm = _list(*(i for i in items if i["metadata"]["namespace"] == "llm"))
    pods = {"team-a": vllm}

    def run(args, timeout):
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("team-a"))
        return _result(pods.get(args[3], "{}"))

    clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="c0")
    assert clusters_api._readable_namespaces["c0"].productive == {"team-a"}
    pods["team-a"] = "{}"                                    # 노드가 0대라 지표 파드가 없다
    clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names=set(), cache_key="c0")
    assert clusters_api._readable_namespaces["c0"].productive == {"team-a"}


def test_GPU_노드가_0대인_라운드는_기다리는_파드가_나온_namespace를_기억에_더한다():
    """GPU 쪽 이름이 아닌 namespace 라도 — 노드가 뜨면 거기서 지표 파드가 돈다 (agent#121 리뷰)."""
    def run(args, timeout):
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("team-b", "zz"))
        return _result(_list(_vllm_pending(namespace="team-b")) if args[3] == "team-b" else "{}")

    _, complete = clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None,
                                                  gpu_node_names=set(), cache_key="c9")
    assert clusters_api._readable_namespaces["c9"].productive == {"team-b"} and complete is True


def _namespace_search_cluster(pods_by_namespace: dict[str, KubectlResult]):
    """노드 0대, 모든 namespace 파드는 Forbidden, namespace 마다 읽으면 pods_by_namespace (없는 곳은 거부)."""
    def fake(args, **kwargs):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces(*pods_by_namespace))
        if args[:3] == ["get", "pods", "-n"]:
            return pods_by_namespace.get(args[3], _result(stderr=FORBIDDEN, success=False))
        return _gpu_cluster(args)
    return fake


def test_모든_namespace_파드를_못_읽어도_namespace마다_끝까지_읽고_없으면_GPU_없음으로_남긴다(client, fake_server, monkeypatch):
    """거부된 곳은 볼 수 없는 곳이라 못 본 곳으로 치지 않는다 — 다음 개요부터는 지름길로 멈춘다 (agent#121 리뷰)."""
    cluster = fake_server.add_cluster()
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", _namespace_search_cluster(
        {"llm": _result("{}"), "secret": _result(stderr=FORBIDDEN, success=False)}))
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"] is None
    assert [body["hasGpu"] for body in _gpu_calls(fake_server)] == [False]


def test_namespace_목록이_상한으로_잘렸거나_지난_기억으로_읽었으면_끝까지_읽은_게_아니다(monkeypatch):
    """25번째부터는 읽지 않았고, TTL 지난 기억은 그 뒤 생긴 namespace 를 모른다 — GPU 없음으로 단정하지 않는다 (agent#121 리뷰)."""
    monkeypatch.setattr(gpu, "MAX_POD_NAMESPACES", 2)
    now = [0.0]
    listing_ok = [True]

    def run(args, timeout):
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("a", "b", "c")) if listing_ok[0] else _result(stderr="i/o timeout", success=False)
        return _result("{}")

    def call(key):
        return clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names=set(),
                                               cache_key=key, clock=lambda: now[0])[1]

    assert call("cut") is False                              # 셋 중 둘만 읽었다
    assert call("cut") is False                              # 기억(TTL 안)도 잘린 목록이다
    monkeypatch.setattr(gpu, "MAX_POD_NAMESPACES", 24)
    assert call("whole") is True
    now[0] += clusters_api.READABLE_NAMESPACES_TTL
    listing_ok[0] = False
    assert call("whole") is False                            # 목록을 못 읽어 지난 기억으로 읽었다


def test_모르는_연결도_GPU_노드가_없으면_간격마다_한_번만_찾고_조회를_못_보낸_라운드는_세지_않는다(monkeypatch):
    """namespace 가 상한보다 많아 끝까지 못 읽는 곳이 개요마다 파드를 전부 다시 읽지 않게 (agent#121 리뷰)."""
    monkeypatch.setattr(gpu, "MAX_POD_NAMESPACES", 1)
    now = [0.0]
    seen: list[list[str]] = []
    budget_left = [True]

    def run(args, timeout):
        seen.append(args)
        if args == gpu.NODES_COMMAND:
            if not budget_left[0]:
                now[0] += clusters_api.GPU_BUDGET                  # 노드 조회가 예산을 다 먹었다
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("a", "b"))                 # 상한 1 — 끝까지 못 읽는다
        return _result("{}")

    def call():
        return clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="c:fp:ns", known=None,
                                         clock=lambda: now[0])

    budget_left[0] = False
    assert call() == (None, None)                             # 예산이 모자라 조회를 못 보냈다 — 창을 잡지 않는다
    budget_left[0] = True
    seen.clear()
    assert call() == (None, None) and gpu.NAMESPACES_COMMAND in seen   # 바로 다시 찾는다(끝까지는 못 읽음)
    seen.clear()
    assert call() == (None, None) and seen == [gpu.NODES_COMMAND]      # 간격 안 — 노드만 본다
    now[0] += clusters_api.GPU_ABSENT_RECHECK
    seen.clear()
    call()
    assert any(args[:3] == ["get", "pods", "-n"] for args in seen)   # 간격이 지나 다시 namespace 마다 찾았다


def test_GPU_없음_시각은_오래_안_부른_연결도_되감기지_않고_동시_개요는_창_하나를_나눈다():
    """한 시간에 한 번 대시보드를 여는 쓰임에서도 다시 찾고, 같은 연결의 동시 개요는 한 번만 찾는다 (agent#121 리뷰)."""
    now = [0.0]

    def due(key):
        return clusters_api._absent_search_due(key, unknown=False, clock=lambda: now[0])

    assert due("a") and due("b")                              # 기록이 없다 — 바로 한 번 찾는다
    assert not due("a")                                       # 같은 연결의 두 번째 개요 — 창은 이미 잡혔다
    now[0] += 3600                                            # 한 시간 뒤 묶음으로 다시
    assert due("a") and due("b")                              # b 도 찾을 때다
    now[0] += 3 * 24 * 3600                                   # 주말 — a 가 b 를 지워도
    assert due("a") and due("b")                              # 지워진 b 도 미뤄지지 않고 바로 찾는다


def test_파드_목록을_읽지_못했으면_기다리는_파드가_없다고_단정하지_않는다(client, fake_server, monkeypatch, caplog):
    cluster = fake_server.add_cluster(has_gpu=True)

    def fake(args, **kwargs):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result('{"items": 3}')                   # 모양이 다르다
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    with caplog.at_level("DEBUG", logger="kukie_agent.clusters_api"):
        r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"] is None
    assert _gpu_calls(fake_server) == [] and fake_server.clusters[cluster]["hasGpu"] is True
    assert "GPU 를 기다리는 파드를 찾지 못했다" in caplog.text


def test_모든_namespace_파드도_namespace_목록도_못_읽으면_클러스터_namespace_파드에서만_찾는다(client, fake_server, monkeypatch):
    """GPU 읽기 RBAC 를 적용하지 않은 고객 EKS — namespace 범위 권한뿐이다."""
    cluster = fake_server.add_cluster(namespace="llm")
    vllm_only = _list(*(item for item in json.loads(fixture("gpu_pods.json"))["items"]
                        if item["metadata"]["namespace"] == "llm"))

    def fake(args, **kwargs):
        if args in (gpu.PODS_COMMAND, gpu.NAMESPACES_COMMAND):
            return _result(stderr=FORBIDDEN, success=False)
        if args == ["get", "pods", "-o", "json", "-n", "llm"]:
            return _result(vllm_only)
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    body = r.json()["gpu"]
    assert [n["utilization_pct"] for n in body["nodes"]] == [None, None]    # DCGM 은 다른 namespace 라 못 찾는다
    assert [s["model"] for s in body["serving"]] == ["openai/gpt-oss-20b"]


def test_모든_namespace_파드를_못_읽으면_namespace마다_읽은_파드를_합친다(client, fake_server, monkeypatch):
    """GPU 읽기 RBAC: 파드 목록은 고객이 RoleBinding 한 namespace 에만 (kukie-infra#124 리뷰). 못 읽는 namespace 는 빠진다."""
    cluster = fake_server.add_cluster(namespace="shop")
    items = json.loads(fixture("gpu_pods.json"))["items"]
    by_ns = {ns: _list(*(i for i in items if i["metadata"]["namespace"] == ns)) for ns in ("gpu-operator", "llm")}
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(args)
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("default", "shop", "kube-system", "llm", "gpu-operator"))
        if args[:3] == ["get", "pods", "-n"]:                                 # namespace_pods_command
            return _result(by_ns[args[3]]) if args[3] in by_ns else _result(stderr=FORBIDDEN, success=False)
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    body = r.json()["gpu"]
    assert [n["utilization_pct"] for n in body["nodes"]] == [87, 87]          # 다른 namespace 의 DCGM 을 찾았다
    assert [s["model"] for s in body["serving"]] == ["openai/gpt-oss-20b"]
    per_ns = sorted(args[3] for args in seen if args[:3] == ["get", "pods", "-n"])
    assert per_ns == ["default", "gpu-operator", "llm"]        # kube-* 와 클러스터 namespace(shop)는 읽지 않는다

    # 다음 개요는 읽혔던 namespace 만 — namespace 목록도, 거부된 default 도 다시 읽지 않는다 (agent#120 리뷰)
    seen.clear()
    assert client.get(f"/clusters/{cluster}/overview", headers=USER).json()["gpu"]["serving"]
    assert gpu.NAMESPACES_COMMAND not in seen
    assert sorted(args[3] for args in seen if args[:3] == ["get", "pods", "-n"]) == ["gpu-operator", "llm"]


def test_GPU_쪽_이름이_아닌_namespace의_vLLM도_찾는다():
    """vLLM 은 몇 개인지 정해져 있지 않아 찾은 것으로 일찍 멈추지 않는다."""
    items = json.loads(fixture("gpu_pods.json"))["items"]
    dcgm = _list(*(i for i in items if i["metadata"]["namespace"] == "gpu-operator"))
    vllm = _list(*(dict(i, metadata=dict(i["metadata"], namespace="team-a"))
                   for i in items if i["metadata"]["namespace"] == "llm"))
    seen: list[str] = []

    def run(args, timeout):
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("team-a", "gpu-operator", "zz"))
        if args[:3] == ["get", "pods", "-n"]:
            seen.append(args[3])
            return _result({"gpu-operator": dcgm, "team-a": vllm}.get(args[3], "{}"))
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}")
    assert [s["model"] for s in body["serving"]] == ["openai/gpt-oss-20b"]
    assert sorted(seen) == ["gpu-operator", "team-a", "zz"]


def test_Forbidden인_namespace만_기억에서_빼고_시간_초과한_것은_다시_읽는다():
    """느린 클러스터에서도 기억이 걸린다 — 모르는 것은 남기고 거부된 것만 뺀다 (agent#120 리뷰)."""
    calls: list[list[str]] = []

    def run(args, timeout):
        calls.append(args)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("a", "slow", "denied"))
        return {"a": _result("{}"), "slow": _result(stderr="context deadline exceeded", success=False),
                "denied": _result(stderr=FORBIDDEN, success=False)}[args[3]]

    clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="c1")
    calls.clear()
    clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="c1")
    assert gpu.NAMESPACES_COMMAND not in calls
    assert sorted(args[3] for args in calls) == ["a", "slow"]


def test_기억은_TTL이_지나면_다시_목록을_읽고_오래된_항목은_지운다():
    now = [0.0]
    calls: list[list[str]] = []

    def run(args, timeout):
        calls.append(args)
        return _result(_namespaces("a")) if args == gpu.NAMESPACES_COMMAND else _result("{}")

    def call(key):
        clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key=key,
                                        clock=lambda: now[0])

    call("gone")                                   # 다시 부르지 않는 연결
    call("live")
    now[0] += clusters_api.READABLE_NAMESPACES_TTL
    call("live")                                   # TTL 이 지나 목록을 다시 읽는다
    assert calls.count(gpu.NAMESPACES_COMMAND) == 3
    assert "gone" in clusters_api._readable_namespaces       # TTL 두 배까지는 남긴다
    now[0] += clusters_api.READABLE_NAMESPACES_TTL
    call("live")
    assert set(clusters_api._readable_namespaces) == {"live"}


def test_기억을_쓰는_라운드에서도_Forbidden은_빼고_TTL은_늘리지_않는다():
    now = [0.0]
    denied = {"b"}
    calls: list[list[str]] = []

    def run(args, timeout):
        calls.append(args)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("a", "b"))
        if args[3] in denied:
            return _result(stderr=FORBIDDEN, success=False)
        return _result(stderr="context deadline exceeded", success=False)  # a 는 처음에 느렸다

    def call():
        clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="k",
                                        clock=lambda: now[0])

    denied = set()
    call()                                         # 둘 다 시간 초과 → 둘 다 남는다
    assert clusters_api._readable_namespaces["k"].names == ("a", "b")
    denied = {"b"}
    now[0] += 10
    call()                                         # 기억을 쓰는 라운드: b 가 Forbidden 으로 확인돼 빠진다
    memo = clusters_api._readable_namespaces["k"]
    assert (memo.at, memo.names) == (0.0, ("a",))
    assert calls.count(gpu.NAMESPACES_COMMAND) == 1


def test_예산이_모자라_멈춘_자리부터_다음에_이어서_읽는다():
    budget_left = [2]
    seen: list[str] = []

    def run(args, timeout):
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("a", "b", "c"))
        seen.append(args[3])
        return _result("{}")

    def budget(limit):
        budget_left[0] -= 1
        return limit if budget_left[0] >= 0 else 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
        budget_left[0] = 4                         # 목록(확인·조회) 2번 + namespace 2개
        clusters_api._pods_by_namespace(run, budget, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="k")
        assert seen == ["a", "b"]
        seen.clear()
        budget_left[0] = 1
        clusters_api._pods_by_namespace(run, budget, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="k")
        assert seen == ["c"]                       # 지난번에 멈춘 자리(c)부터


def test_예산이_모자라도_줄마다_돌아가며_읽어_어느_namespace도_빠지지_않는다():
    """앞 줄(GPU 쪽 이름·GPU 파드가 나온 곳)이 먼저, 줄마다 지난번에 멈춘 자리부터 (agent#120 리뷰)."""
    vllm = {"metadata": {"namespace": "team-x", "name": "v"}, "status": {"phase": "Running"},
            "spec": {"nodeName": L40S_1, "containers": [{"name": "vllm", "image": "vllm/vllm-openai:v0.32.0"}]}}
    app = {"metadata": {"name": "web"}, "status": {"phase": "Running"}, "spec": {"nodeName": CPU, "containers": []}}
    rounds: list[list[str]] = []

    def run(args, timeout):
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("gpu-operator", "a", "b", "c", "team-x"))
        rounds[-1].append(args[3])
        return _result(_list(vllm) if args[3] == "team-x" else _list(app) if args[3] in ("a", "b") else "{}")

    def call(reads):
        left = [reads]

        def budget(limit):
            left[0] -= 1
            return limit if left[0] >= 0 else 0
        rounds.append([])
        clusters_api._pods_by_namespace(run, budget, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="k")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
        call(2 + 5)                                       # 처음은 전부 → team-x 에 vLLM, 앞 줄은 gpu-operator·team-x
        call(0)                                           # 한 번 아무것도 못 읽어도 team-x 는 앞 줄에 남는다
        for _ in range(3):
            call(3)                                       # 앞 줄 둘 + 뒤 줄 하나
        for _ in range(2):
            call(1)                                       # 앞 줄보다 예산이 작아도 앞 줄 안에서 돈다
    assert all({r[0], r[2]} == {"gpu-operator", "team-x"} for r in rounds[2:5])   # 앞 줄 둘
    assert {r[1] for r in rounds[2:5]} == {"a", "b", "c"}                          # 끼운 뒤 줄 한 칸이 돈다
    assert {r[0] for r in rounds[5:]} == {"gpu-operator", "team-x"}               # 예산 1: 앞 줄 안에서 돈다


def test_앞_줄이_예산을_다_써도_뒤_줄이_매_라운드_한_칸은_읽힌다():
    names = ["gpu-a", "gpu-b", "gpu-c", "gpu-d", "team-a"]
    rounds: list[list[str]] = []

    def run(args, timeout):
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces(*names))
        rounds[-1].append(args[3])
        return _result("{}")

    def call(reads):
        left = [reads]

        def budget(limit):
            left[0] -= 1
            return limit if left[0] >= 0 else 0
        rounds.append([])
        clusters_api._pods_by_namespace(run, budget, "{}", namespace=None, gpu_node_names={L40S_1}, cache_key="k")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
        call(2 + 2)                                       # 목록 + 둘 — 앞 줄(gpu-*)은 넷이라 예산이 모자란다
        call(2)
    assert all("team-a" in r for r in rounds)


def test_목록을_부르지도_못한_라운드는_기억_시각을_늘리지_않는다():
    """예산이 없어 get namespaces 를 못 부른 라운드가 TTL 을 미루지 않는다 (agent#120 리뷰)."""
    now = [0.0]

    def run(args, timeout):
        return _result(_namespaces("llm")) if args == gpu.NAMESPACES_COMMAND else _result("{}")

    clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None, gpu_node_names={L40S_1},
                                    cache_key="k", clock=lambda: now[0])
    now[0] += clusters_api.READABLE_NAMESPACES_TTL
    clusters_api._pods_by_namespace(run, lambda limit: 0, "{}", namespace=None, gpu_node_names={L40S_1},
                                    cache_key="k", clock=lambda: now[0])
    assert clusters_api._readable_namespaces["k"].at == 0.0

def test_GPU_파드가_나온_namespace는_상한에_잘리지_않는다(monkeypatch):
    monkeypatch.setattr(gpu, "MAX_POD_NAMESPACES", 2)
    names, whole = gpu.namespace_names(_namespaces("a", "b", "c", "zz-llm-team"), prefer={"zz-llm-team"})
    assert names == ["zz-llm-team", "a"]


def test_목록을_다시_읽다_실패하면_지난_기억으로_읽는다():
    now = [0.0]
    listing_ok = [True]
    seen: list[str] = []

    def run(args, timeout):
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("llm")) if listing_ok[0] else _result(stderr="i/o timeout", success=False)
        seen.append(args[3])
        return _result("{}")

    def call():
        return clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None,
                                               gpu_node_names={L40S_1}, cache_key="k", clock=lambda: now[0])[0]

    call()
    now[0] += clusters_api.READABLE_NAMESPACES_TTL
    listing_ok[0] = False
    seen.clear()
    call()
    assert seen == ["llm"]


def test_GPU_대상_판정은_모양이_이상한_파드_목록에도_False():
    """개요를 500 으로 만들지 않는다 (agent#120 리뷰)."""
    broken = '{"items": [{"metadata": {"name": "p"}, "status": "Running", "spec": "x"}]}'
    assert gpu.has_gpu_targets(broken, {L40S_1}) is False
    assert gpu.has_gpu_targets("not json", {L40S_1}) is False

def test_연결_지문이_없으면_namespace를_기억하지_않는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(namespace="shop")
    fake_server.clusters[cluster]["fingerprint"] = ""

    def fake(args, **kwargs):
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("llm"))
        if args[:3] == ["get", "pods", "-n"]:
            return _result("{}")
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    assert client.get(f"/clusters/{cluster}/overview", headers=USER).status_code == 200
    assert clusters_api._readable_namespaces == {}

def test_namespace_탐색은_남은_GPU_예산의_절반까지만_쓴다(monkeypatch):
    """느린 API 서버에서도 지표를 읽을 시간을 남긴다 (agent#120 리뷰)."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    searched: list[float] = []

    def run(args, timeout):
        now[0] += {"nodes": 1}.get(args[1], 0)
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces(*(f"ns-{i}" for i in range(20))))
        if args[:3] == ["get", "pods", "-n"]:
            now[0] += timeout                                                # namespace 마다 시간 제한까지 매달린다
            searched.append(now[0])
            return _result(stderr="timed out", success=False)
        return _gpu_cluster(args)

    clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert searched and max(searched) <= 1 + (clusters_api.GPU_BUDGET - 1) / 2 + 1


def test_GPU_노드가_없으면_namespace_탐색이_남은_예산을_다_쓴다(monkeypatch):
    """지표를 읽지 않으니 반으로 자르지 않는다 — 기다리는 파드를 찾는 길이 이것뿐이다 (agent#121 리뷰)."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    searched: list[float] = []

    def run(args, timeout):
        now[0] += {"nodes": 1}.get(args[1], 0)
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces(*(f"ns-{i}" for i in range(20))))
        now[0] += timeout
        searched.append(now[0])
        return _result(stderr="timed out", success=False)

    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert max(searched) > 1 + (clusters_api.GPU_BUDGET - 1) / 2 + 1
    assert max(searched) <= clusters_api.GPU_BUDGET + 1
    assert (body, has_gpu) == (None, None)                   # 시간 초과가 있으면 모른다


def test_namespace_목록도_못_읽으면_클러스터_namespace_파드만_쓴다():
    pods = _list(*(i for i in json.loads(fixture("gpu_pods.json"))["items"] if i["metadata"]["namespace"] == "llm"))
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args in (gpu.PODS_COMMAND, gpu.NAMESPACES_COMMAND):
            return _result(stderr=FORBIDDEN, success=False)
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods=pods)
    assert [s["model"] for s in body["serving"]] == ["openai/gpt-oss-20b"]
    assert not any(args[:3] == ["get", "pods", "-n"] for args in seen)


def test_namespace는_GPU_쪽_이름부터_상한까지_고른다(monkeypatch):
    monkeypatch.setattr(gpu, "MAX_POD_NAMESPACES", 3)
    names, whole = gpu.namespace_names(_namespaces("a", "b", "kube-system", "kube-public", "my-vllm", "c", "gpu-operator", "a"))
    assert names == ["gpu-operator", "my-vllm", "a"]
    assert gpu.hinted_namespace("monitoring") and gpu.hinted_namespace("observability")
    # 클러스터 namespace 는 상한을 매기기 전에 뺀다 — 한 칸을 먹지 않는다
    assert gpu.namespace_names(_namespaces("gpu-operator", "a", "b"), skip="gpu-operator") == (["a", "b"], True)


def test_파드_목록을_합칠_때_같은_파드는_한_번이고_깨진_출력은_건너뛴다():
    pod = {"metadata": {"namespace": "llm", "name": "p"}, "status": {"phase": "Running"}}
    merged = json.loads(gpu.merge_pod_lists([_list(pod), "not json", _list(pod), '{"items": {}}']))
    assert merged["items"] == [pod]
