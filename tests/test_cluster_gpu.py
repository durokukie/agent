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


def test_allocatable이_없으면_capacity를_보고_0이면_GPU_노드가_아니다():
    stdout = _list(_node("a", capacity={"nvidia.com/gpu": "1"}),
                   _node("b", allocatable={"nvidia.com/gpu": "0"}),
                   _node("c", allocatable={"nvidia.com/gpu": "abc"}))
    assert [r["node"] for r in gpu.gpu_nodes(stdout)] == ["a"]


def test_AMD_GPU도_센다():
    stdout = _list(_node("mi", labels={"amd.com/gpu.product-name": "AMD_Instinct_MI300X_OAM", "amd.com/gpu.vram": "192G"},
                         allocatable={"amd.com/gpu": "8"}))
    row = gpu.gpu_nodes(stdout)[0]
    assert (row["product"], row["count"], row["memory_mib"]) == ("AMD_Instinct_MI300X_OAM", 8, 8 * 192 * 1024)


def test_GPU_노드가_없으면_build는_None이다():
    assert gpu.gpu_nodes(fixture("nodes.json")) == []
    assert gpu.build([], []) is None
    assert gpu.build([{"node": "n"}], []) == {"nodes": [{"node": "n"}], "serving": [], "benchmarks": []}


def test_노드_목록이_깨졌으면_ValueError():
    with pytest.raises(ValueError):
        gpu.gpu_nodes("not json")
    with pytest.raises(ValueError):
        gpu.gpu_nodes('{"items": {}}')


# ── 지표 파드 찾기 ──────────────────────────────────────────

def test_GPU_노드마다_DCGM_exporter_파드_하나를_찾는다():
    assert gpu.dcgm_targets(fixture("gpu_pods.json"), {L40S_1, L40S_4}) == {
        L40S_1: ("gpu-operator", "nvidia-dcgm-exporter-abcde", 9400),
        L40S_4: ("gpu-operator", "nvidia-dcgm-exporter-fghij", 9400),
    }
    assert gpu.dcgm_targets(fixture("gpu_pods.json"), {L40S_1}) == {
        L40S_1: ("gpu-operator", "nvidia-dcgm-exporter-abcde", 9400)}       # GPU 노드가 아닌 곳의 exporter 는 안 본다


def test_vLLM_파드는_이미지나_명령으로_찾고_포트는_인자를_먼저_본다():
    assert gpu.vllm_targets(fixture("gpu_pods.json")) == [
        ("llm", "qwen-0", 8000, L40S_4),                                   # 명령 `vllm serve`, 포트를 안 적으면 기본 8000
        ("llm", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", 8080, L40S_1),          # --port 8080
    ]                                                                      # Running 이 아닌 파드는 빠진다


def test_포트_이름과_등호_인자():
    def pod(container: dict) -> str:
        return _list({"metadata": {"name": "p", "namespace": "n"}, "spec": {"containers": [container]},
                      "status": {"phase": "Running"}})
    assert gpu.vllm_targets(pod({"image": "vllm/vllm-openai", "args": ["--port=9000"]}))[0][2] == 9000
    assert gpu.vllm_targets(pod({"image": "vllm/vllm-openai", "ports": [
        {"name": "grpc", "containerPort": 9001}, {"name": "http", "containerPort": 8001}]}))[0][2] == 8001
    assert gpu.vllm_targets(pod({"image": "vllm/vllm-openai", "ports": [{"containerPort": 7000}]}))[0][2] == 7000


def test_지표_파드_수에는_상한이_있다():
    pods = _list(*({"metadata": {"name": f"v{i:02d}", "namespace": "llm"},
                    "spec": {"containers": [{"image": "vllm/vllm-openai"}]}, "status": {"phase": "Running"}}
                   for i in range(20)))
    assert len(gpu.vllm_targets(pods)) == gpu.MAX_METRIC_TARGETS


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
    assert [args for args, _ in seen[4:]] == [
        gpu.NODES_COMMAND, gpu.PODS_COMMAND,
        gpu.proxy_command("gpu-operator", "nvidia-dcgm-exporter-abcde", 9400),
        gpu.proxy_command("gpu-operator", "nvidia-dcgm-exporter-fghij", 9400),
        gpu.proxy_command("llm", "qwen-0", 8000),
        gpu.proxy_command("llm", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", 8080),
    ]
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


def test_GPU_노드가_없으면_gpu는_null이고_파드는_읽지_않는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster()
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(args)
        return _result(fixture("nodes.json")) if args == gpu.NODES_COMMAND else _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["gpu"] is None
    assert gpu.PODS_COMMAND not in seen and not any(args[1] == "--raw" for args in seen)


def test_모든_namespace_파드를_못_읽으면_클러스터_namespace_파드에서만_찾는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(namespace="llm")
    vllm_only = _list(*(item for item in json.loads(fixture("gpu_pods.json"))["items"]
                        if item["metadata"]["namespace"] == "llm"))

    def fake(args, **kwargs):
        if args == gpu.PODS_COMMAND:
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
