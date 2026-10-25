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
    assert "GPU 여부를 남기지 못했다: SERVER_UNAVAILABLE" in caplog.text


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
