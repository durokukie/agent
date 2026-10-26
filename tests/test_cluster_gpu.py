"""클러스터 개요의 gpu 칸 — DURO-118.

파싱은 순수 함수(clusters/gpu.py)라 픽스처(tests/fixtures/overview 의 gpu_*·dcgm·vllm)로 바로 본다.
엔드포인트는 test_cluster_overview.py 처럼 가짜 server 와 가짜 kubectl 로 본다.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import USER
from kukie_agent import clusters_api, server
from kukie_agent.clusters import gpu
from kukie_agent.kubectl import KubectlResult

FIXTURES = Path(__file__).parent / "fixtures" / "overview"
L40S_1, L40S_4, CPU = "ip-10-60-43-165", "ip-10-60-50-12", "ip-10-60-1-7"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _node(name: str, *, labels: dict | None = None, allocatable: dict | None = None,
          capacity: dict | None = None) -> dict:
    return {"metadata": {"name": name, "labels": labels or {}},
            "status": {"allocatable": allocatable or {}, "capacity": capacity or {}}}


def _list(*items: dict) -> str:
    return json.dumps({"items": list(items)})


# ── 노드 ─────────────────────────────────────────────────

def test_GPU_자원이_있는_노드만_싣고_실시간_값은_아직_null이다():
    rows = gpu.gpu_nodes(fixture("gpu_nodes.json"))
    assert [r["node"] for r in rows] == [L40S_1, L40S_4]                      # CPU 노드는 빠진다, 이름순
    assert rows[0] == {"node": L40S_1, "product": "NVIDIA-L40S", "count": 1, "memory_mib": 46068,
                       "utilization_pct": None, "memory_used_mib": None, "temperature_c": None, "power_w": None}


def test_time_slicing으로_불어난_자원_수보다_물리_GPU_수_라벨을_믿는다():
    """allocatable nvidia.com/gpu 16 은 4장을 4조각씩 나눈 것이다 — 메모리도 물리 4장 기준."""
    row = gpu.gpu_nodes(fixture("gpu_nodes.json"))[1]
    assert (row["count"], row["memory_mib"]) == (4, 4 * 46068)


def test_라벨이_없으면_자원_수와_인스턴스_종류로_메모리는_0():
    stdout = _list(_node("n1", labels={"node.kubernetes.io/instance-type": "g6e.xlarge"},
                         allocatable={"nvidia.com/gpu": "2"}))
    assert gpu.gpu_nodes(stdout)[0] == {
        "node": "n1", "product": "g6e.xlarge", "count": 2, "memory_mib": 0,
        "utilization_pct": None, "memory_used_mib": None, "temperature_c": None, "power_w": None}


def test_allocatable에_칸이_없을_때만_capacity를_보고_0이면_GPU_노드가_아니다():
    """device plugin 이 죽으면 capacity 는 남고 allocatable 만 0 이다 — 쓸 수 있는 GPU 가 없다 (PR #118 리뷰)."""
    stdout = _list(_node("a", capacity={"nvidia.com/gpu": "1"}),
                   _node("b", allocatable={"nvidia.com/gpu": "0"}, capacity={"nvidia.com/gpu": "8"}),
                   _node("c", allocatable={"nvidia.com/gpu": "abc"}))
    assert [r["node"] for r in gpu.gpu_nodes(stdout)] == ["a"]


def test_AMD_GPU도_센다():
    stdout = _list(_node("mi", labels={"amd.com/gpu.product-name": "AMD_Instinct_MI300X_OAM", "amd.com/gpu.vram": "192G"},
                         allocatable={"amd.com/gpu": "8"}))
    row = gpu.gpu_nodes(stdout)[0]
    assert (row["product"], row["count"], row["memory_mib"]) == ("AMD_Instinct_MI300X_OAM", 8, 8 * 192 * 1024)


def test_GPU_노드도_기다리는_파드도_없으면_build는_None이다():
    assert gpu.gpu_nodes(fixture("nodes.json")) == []
    assert gpu.build([], []) is None and gpu.build([], [], []) is None
    assert gpu.build([{"node": "n"}], []) == {"nodes": [{"node": "n"}], "serving": [], "benchmarks": [], "waiting": []}
    waiting = [{"namespace": "llm", "pod": "vllm-0", "gpus": 1, "phase": "Pending"}]
    assert gpu.build([], [], waiting) == {"nodes": [], "serving": [], "benchmarks": [], "waiting": waiting}


def test_노드_목록이_깨졌으면_ValueError():
    with pytest.raises(ValueError):
        gpu.gpu_nodes("not json")
    with pytest.raises(ValueError):
        gpu.gpu_nodes('{"items": {}}')


# ── 지표 파드 찾기 ──────────────────────────────────────────

def pods(name: str = "gpu_pods.json") -> list[dict]:
    return gpu.running_pods(fixture(name))


def test_GPU_노드마다_DCGM_exporter_파드_하나를_찾는다():
    assert gpu.dcgm_targets(pods(), {L40S_1, L40S_4}) == {
        L40S_1: ("gpu-operator", "nvidia-dcgm-exporter-abcde", 9400),
        L40S_4: ("gpu-operator", "nvidia-dcgm-exporter-fghij", 9400),
    }
    assert gpu.dcgm_targets(pods(), {L40S_1}) == {
        L40S_1: ("gpu-operator", "nvidia-dcgm-exporter-abcde", 9400)}       # GPU 노드가 아닌 곳의 exporter 는 안 본다


def test_vLLM_파드는_이미지나_명령으로_찾고_포트는_인자를_먼저_본다():
    assert gpu.vllm_targets(pods(), {L40S_1, L40S_4}) == [
        ("llm", "qwen-0", 8000, L40S_4),                                   # 명령 `vllm serve`, 포트를 안 적으면 기본 8000
        ("llm", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", 8080, L40S_1),          # --port 8080
    ]                                                                      # Running 이 아닌 파드는 빠진다
    # GPU 노드 밖의 vllm 파드(`vllm bench` Job 등)는 대상 칸을 먹지 않는다 (PR #118 리뷰)
    assert gpu.vllm_targets(pods(), {L40S_1}) == [("llm", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", 8080, L40S_1)]


def test_포트_이름과_등호_인자():
    def port(container: dict) -> int:
        item = {"metadata": {"name": "p", "namespace": "n"}, "spec": {"nodeName": "g", "containers": [container]},
                "status": {"phase": "Running"}}
        return gpu.vllm_targets(gpu.running_pods(_list(item)), {"g"})[0][2]
    assert port({"image": "vllm/vllm-openai", "args": ["--port=9000"]}) == 9000
    assert port({"image": "vllm/vllm-openai", "ports": [
        {"name": "grpc", "containerPort": 9001}, {"name": "http", "containerPort": 8001}]}) == 8001
    assert port({"image": "vllm/vllm-openai", "ports": [{"containerPort": 7000}]}) == 7000


def test_지표_파드_수에는_상한이_있다():
    many = _list(*({"metadata": {"name": f"v{i:02d}", "namespace": "llm"},
                    "spec": {"nodeName": "g", "containers": [{"image": "vllm/vllm-openai"}]}, "status": {"phase": "Running"}}
                   for i in range(20)))
    assert len(gpu.vllm_targets(gpu.running_pods(many), {"g"})) == gpu.MAX_METRIC_TARGETS


def test_proxy_command는_파드_프록시로_metrics를_읽는다():
    assert gpu.proxy_command("gpu-operator", "dcgm-x", 9400) == [
        "get", "--raw", "/api/v1/namespaces/gpu-operator/pods/dcgm-x:9400/proxy/metrics"]


# ── DCGM ─────────────────────────────────────────────────

def test_DCGM_지표로_실시간_값을_채운다():
    row = gpu.gpu_nodes(fixture("gpu_nodes.json"))[0]
    gpu.apply_dcgm(row, fixture("dcgm.txt"))
    assert (row["utilization_pct"], row["memory_used_mib"], row["temperature_c"], row["power_w"]) == (87, 41820, 61, 284)
    assert row["memory_mib"] == 46068                                       # 라벨 값을 그대로 둔다


def test_GPU가_여럿이면_사용률은_평균_메모리_전력은_합_온도는_최고():
    text = "\n".join(
        f'{name}{{gpu="{i}",UUID="GPU-{i}"}} {value}'
        for i, (util, used, free, temp, power) in enumerate([(100, 1000, 3000, 70, 300), (50, 2000, 2000, 60, 200)])
        for name, value in (("DCGM_FI_DEV_GPU_UTIL", util), ("DCGM_FI_DEV_FB_USED", used),
                            ("DCGM_FI_DEV_FB_FREE", free), ("DCGM_FI_DEV_GPU_TEMP", temp),
                            ("DCGM_FI_DEV_POWER_USAGE", power)))
    row = {"memory_mib": 0}
    gpu.apply_dcgm(row, text)
    assert row == {"memory_mib": 8000,                                      # 라벨이 없으면 DCGM 의 used + free
                   "utilization_pct": 75, "memory_used_mib": 3000, "temperature_c": 70, "power_w": 500}


def test_MIG_인스턴스는_부모_UUID가_같아도_따로_센다():
    """MIG 줄은 부모 GPU 의 UUID 를 같이 쓰고 GPU_I_ID 로만 갈린다 (PR #118 리뷰)."""
    text = "\n".join(f'DCGM_FI_DEV_FB_USED{{gpu="0",UUID="GPU-A",GPU_I_ID="{i}",GPU_I_PROFILE="1g.10gb"}} 9000'
                     for i in range(7))
    row = {"memory_mib": 81920}
    gpu.apply_dcgm(row, text)
    assert row["memory_used_mib"] == 63000


def test_DCGM_지표가_없거나_깨졌으면_null_그대로():
    row = {"memory_mib": 0, "utilization_pct": None}
    gpu.apply_dcgm(row, "garbage {{{ \n# 주석만\n")
    assert row == {"memory_mib": 0, "utilization_pct": None}


# ── vLLM ─────────────────────────────────────────────────

def test_vLLM_지표로_서빙_줄을_만든다():
    rows = gpu.serving_rows(fixture("vllm.txt"), pod="vllm-x", node=L40S_1)
    assert rows == [{
        "model": "openai/gpt-oss-20b", "pod": "vllm-x", "node": L40S_1, "running": 24, "waiting": 3,
        "kv_cache_pct": 6.3,
        "ttft_p95_ms": 708.3,       # 95번째가 0.5s(90)~0.75s(96) 버킷 사이 → 0.5 + 0.25 × 5/6
        "itl_p95_ms": 24.2,         # 190번째가 0.01s(20)~0.025s(200) 사이 → 0.01 + 0.015 × 170/180
    }]


def test_옛_vLLM_지표_이름도_읽고_엔진이_여럿이면_합친다():
    text = "\n".join([
        'vllm:num_requests_running{engine="0",model_name="m"} 2',
        'vllm:num_requests_running{engine="1",model_name="m"} 3',
        'vllm:gpu_cache_usage_perc{engine="0",model_name="m"} 0.2',
        'vllm:gpu_cache_usage_perc{engine="1",model_name="m"} 0.4',
        'vllm:time_per_output_token_seconds_bucket{engine="0",le="0.05",model_name="m"} 10',
        'vllm:time_per_output_token_seconds_bucket{engine="0",le="+Inf",model_name="m"} 10',
        'vllm:time_per_output_token_seconds_bucket{engine="1",le="0.05",model_name="m"} 10',
        'vllm:time_per_output_token_seconds_bucket{engine="1",le="+Inf",model_name="m"} 10',
    ])
    [row] = gpu.serving_rows(text, pod="p", node="n")
    assert (row["running"], row["waiting"], row["kv_cache_pct"]) == (5, 0, 30.0)
    assert row["ttft_p95_ms"] is None                                       # TTFT 히스토그램이 없다
    assert row["itl_p95_ms"] == 47.5                                        # 20개 중 19번째, 0~0.05 사이


def test_model_name이_없는_info_지표로_유령_줄이_생기지_않는다():
    """실제 vLLM 은 model_name 라벨 없는 info 지표를 늘 낸다 (PR #118 리뷰)."""
    text = fixture("vllm.txt") + "\n".join([
        'vllm:cache_config_info{block_size="16",cache_dtype="auto",engine="0",num_gpu_blocks="1234"} 1.0',
        'vllm:lora_requests_info{max_lora="1",running_lora_adapters="",waiting_lora_adapters=""} 1.0',
    ])
    assert [r["model"] for r in gpu.serving_rows(text, pod="vllm-x", node="n")] == ["openai/gpt-oss-20b"]


def test_무한대_값은_읽지_않는다():
    """±Inf 가 round() 에 들어가면 OverflowError 로 개요 전체가 500 이다 (PR #118 리뷰)."""
    row = {"memory_mib": 0}
    gpu.apply_dcgm(row, 'DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-A"} +Inf\nDCGM_FI_DEV_GPU_TEMP{gpu="0",UUID="GPU-A"} 60')
    assert (row["memory_used_mib"], row["temperature_c"]) == (None, 60)
    [model] = gpu.serving_rows('vllm:num_requests_running{model_name="m"} -Inf\n'
                               'vllm:num_requests_waiting{model_name="m"} 2', pod="p", node="n")
    assert (model["running"], model["waiting"]) == (0, 2)


def test_vLLM_지표가_없는_파드는_줄이_없다():
    assert gpu.serving_rows("http_requests_total 3\n", pod="router", node="n") == []


@pytest.mark.parametrize(("buckets", "expected"), [
    ({}, None),                                                             # 히스토그램이 없다
    ({0.1: 0, math.inf: 0}, None),                                          # 요청이 없었다
    ({0.1: 100, math.inf: 100}, 0.095),                                     # 첫 버킷 안 — 0 부터 선형
    ({0.1: 10, 1.0: 90, math.inf: 100}, 1.0),                               # +Inf 에 걸리면 가장 큰 유한 상한
    ({0.1: 50, 0.2: 50, 0.5: 100, math.inf: 100}, 0.47),
])
def test_histogram_quantile(buckets, expected):
    value = gpu.histogram_quantile(0.95, buckets)
    assert value == pytest.approx(expected) if expected is not None else value is None


def test_Prometheus_글_형식():
    rows = gpu.parse_metrics('\n'.join([
        "# HELP x y",
        'a{b="c",d="e \\"q\\" , f"} 1.5 1700000000000',
        "plain 2",
        'inf_bucket{le="+Inf"} 3',
        "nan NaN",
        "broken",
    ]))
    assert rows == [("a", {"b": "c", "d": 'e "q" , f'}, 1.5), ("plain", {}, 2.0), ("inf_bucket", {"le": "+Inf"}, 3.0)]
    assert gpu.parse_metrics("x +Inf\ny -Inf\nz Inf") == []


# ── 엔드포인트 ─────────────────────────────────────────────

@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)


def _result(stdout: str = "", *, stderr: str = "", success: bool = True) -> KubectlResult:
    return KubectlResult(command="kubectl …", stdout=stdout, stderr=stderr, success=success,
                         exit_code=0 if success else 1)


FORBIDDEN = 'Error from server (Forbidden): nodes is forbidden: User "arn:aws:sts::1:assumed-role/x" cannot list resource'


def _gpu_cluster(args: list[str], **kwargs) -> KubectlResult:
    """GPU 노드 둘, DCGM 둘, vLLM 둘인 가짜 클러스터. qwen-0 의 지표는 시간 초과."""
    if args[:2] == ["get", "--raw"]:
        path = args[2]
        if "dcgm-exporter" in path:
            return _result(fixture("dcgm.txt"))
        if "vllm-gpt-oss" in path:
            return _result(fixture("vllm.txt"))
        return _result(stderr="context deadline exceeded", success=False)
    if args == gpu.NODES_COMMAND:
        return _result(fixture("gpu_nodes.json"))
    if args == gpu.PODS_COMMAND:
        return _result(fixture("gpu_pods.json"))
    return _result(fixture({"deployments,statefulsets,daemonsets": "workloads.json", "pods": "pods.json",
                            "events": "events.json", "daemonsets": "daemonsets.json"}[args[1]]))


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
    assert [args for args, _ in seen[4:6]] == [gpu.NODES_COMMAND, gpu.PODS_COMMAND]
    assert sorted(args for args, _ in seen[6:]) == sorted([           # 지표는 동시에 읽는다
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


def _vllm_pending(namespace: str = "llm", name: str = "vllm-gpt-oss-20b-b897b5d97-kkjsq") -> dict:
    return {"metadata": {"name": name, "namespace": namespace}, "status": {"phase": "Pending"},
            "spec": {"containers": [{"name": "vllm", "image": "vllm/vllm-openai:v0.32.0",
                                     "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}


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
    assert r.json()["gpu"] == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [
        {"namespace": "llm", "pod": "vllm-gpt-oss-20b-b897b5d97-kkjsq", "gpus": 1, "phase": "Pending"}]}
    assert not any(args[1] == "--raw" for args in seen)
    assert [body["hasGpu"] for body in _gpu_calls(fake_server)] == ([True] if stored is None else [])
    assert fake_server.clusters[cluster]["hasGpu"] is True


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

    _, complete, looked = clusters_api._pods_by_namespace(run, lambda limit: limit, "{}", namespace=None,
                                                          gpu_node_names=set(), cache_key="c9")
    assert clusters_api._readable_namespaces["c9"].productive == {"team-b"} and complete is True and looked is True


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


def test_모르는_연결도_GPU_노드가_없으면_간격마다_한_번만_찾고_못_읽은_라운드는_세지_않는다(monkeypatch):
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
    assert call() == (None, None)                             # 파드를 하나도 못 읽었다 — 간격을 태우지 않는다
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

    def claim(key):
        return clusters_api._claim_absent_search(key, unknown=False, clock=lambda: now[0])

    assert claim("a") is not None and claim("b") is not None  # 기록이 없다 — 바로 한 번 찾는다
    assert claim("a") is None                                 # 같은 연결의 두 번째 개요 — 창은 이미 잡혔다
    now[0] += 3600                                            # 한 시간 뒤 묶음으로 다시
    first = claim("a")
    assert first is not None and claim("b") is not None      # b 도 찾을 때다
    clusters_api._release_absent_search("a", first)           # 못 읽고 끝났다 — 되돌린다
    assert claim("a") is not None
    now[0] += 3 * 24 * 3600                                   # 주말 — a 가 b 를 지워도
    assert claim("a") is not None and claim("b") is not None  # 지워진 b 도 미뤄지지 않고 바로 찾는다


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


def _namespaces(*names: str) -> str:
    return _list(*({"metadata": {"name": name}} for name in names))


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


# ── GPU 여부를 server 에 남기기 ─────────────────────────────

def _gpu_calls(fake_server) -> list[dict]:
    return [body for name, body in fake_server.calls if name == "update_cluster_gpu"]


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
