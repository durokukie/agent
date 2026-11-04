"""클러스터 개요의 gpu 칸 — DURO-118.

파싱은 순수 함수(clusters/gpu.py)라 픽스처(tests/fixtures/overview 의 gpu_*·dcgm·vllm·served_models·node_pools)로 바로 본다.
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
    assert gpu.build([{"node": "n"}], []) == {"nodes": [{"node": "n"}], "serving": [], "benchmarks": [], "waiting": [],
                                              "served_models": None, "node_pools": None}
    waiting = [{"namespace": "llm", "pod": "vllm-0", "gpus": 1, "phase": "Pending"}]
    assert gpu.build([], [], waiting) == {"nodes": [], "serving": [], "benchmarks": [], "waiting": waiting,
                                          "served_models": None, "node_pools": None}
    # ServedModel 을 읽었으면 하나도 없어도 칸이 있다 — null(못 읽음)과 빈 배열(없음)은 다르다
    assert gpu.build([], [], [], []) == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [], "served_models": [],
                                         "node_pools": None}
    # GPU 풀이 있으면 그것만으로 칸이 있다. 빈 배열은 "GPU 풀 없음" 이라 칸을 만들지 않는다
    pool = [{"name": "gpu", "gpu_limit": 2}]
    assert gpu.build([], [], [], None, pool) == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [],
                                                 "served_models": None, "node_pools": pool}
    assert gpu.build([], [], [], None, []) is None


def test_노드_목록이_깨졌으면_ValueError():
    with pytest.raises(ValueError):
        gpu.gpu_nodes("not json")
    with pytest.raises(ValueError):
        gpu.gpu_nodes('{"items": {}}')


# ── ServedModel (DURO-182) ─────────────────────────────────

def test_ServedModel을_namespace_이름순으로_응답_줄로_바꾼다():
    assert gpu.served_models(fixture("served_models.json")) == [
        {"namespace": "a-team", "name": "gpt-oss-20b", "model": "openai/gpt-oss-20b", "phase": "Failed",
         "reason": "Unschedulable", "message": "0/2 nodes are available: 2 Insufficient nvidia.com/gpu.",
         "download_percent": 100, "download_bytes": None, "download_total_bytes": None,
         "max_concurrency": None, "started_at": None, "stops_at": None},
        {"namespace": "llm", "name": "gpt-oss-20b", "model": "openai/gpt-oss-20b", "phase": "Ready",
         "reason": "", "message": "", "download_percent": 100, "download_bytes": 44452911104,
         "download_total_bytes": 44452911104, "max_concurrency": 12,
         "started_at": "2026-11-04T01:00:00Z", "stops_at": "2026-11-04T03:00:00Z"},
        # 오퍼레이터가 아직 status 를 쓰지 않았다 — 문자열은 "", 숫자는 null
        {"namespace": "llm", "name": "just-created", "model": "org/just-created", "phase": "",
         "reason": "", "message": "", "download_percent": None, "download_bytes": None, "download_total_bytes": None,
         "max_concurrency": None,
         "started_at": None, "stops_at": None},
        # 내려받기 0% 는 값이다 — null 이 아니다. 전체 크기를 모르면 받은 용량만 있다 (약속: percent 0, totalBytes 없음)
        {"namespace": "llm", "name": "qwen3", "model": "Qwen/Qwen3.6-35B-A3B", "phase": "Downloading",
         "reason": "Downloading", "message": "download Job is running", "download_percent": 0,
         "download_bytes": 1073741824, "download_total_bytes": None, "max_concurrency": None,
         "started_at": None, "stops_at": None},
    ]


def test_ServedModel이_하나도_없으면_빈_배열이고_이름_없는_항목은_뺀다():
    assert gpu.served_models('{"items": []}') == []
    assert gpu.served_models(_list({"metadata": {"namespace": "llm"}}, {"spec": {}})) == []


@pytest.mark.parametrize("broken", ["", "not json", "{}", '{"items": {}}', '{"items": ["x"]}', "[]"])
def test_ServedModel_목록_모양이_아니면_ValueError다(broken):
    """빈 배열은 "없다" 라는 뜻이다 — 모양이 다른 출력을 빈 배열로 읽으면 "없다" 로 거짓말한다."""
    with pytest.raises(ValueError):
        gpu.served_models(broken)


@pytest.mark.parametrize(("value", "expected"), [(55, 55), (0, 0), (None, None), ("55", None), (True, None), (1.5, None)])
def test_ServedModel_숫자_칸은_정수만_싣는다(value, expected):
    item = {"metadata": {"name": "m"}, "status": {"download": {"percent": value}, "baseline": {"maxConcurrency": value}}}
    [row] = gpu.served_models(_list(item))
    assert (row["download_percent"], row["max_concurrency"]) == (expected, expected)


@pytest.mark.parametrize(("value", "expected"), [
    (13314398618, 13314398618), (0, 0), (None, None), ("13314398618", None), (True, None), (1.5, None),
])
def test_내려받은_용량과_전체_용량은_정수만_싣는다(value, expected):
    """int64 라 32비트를 넘는 값(모델은 수십 GB 다)도 그대로 싣는다."""
    item = {"metadata": {"name": "m"}, "status": {"download": {"downloadedBytes": value, "totalBytes": value}}}
    [row] = gpu.served_models(_list(item))
    assert (row["download_bytes"], row["download_total_bytes"]) == (expected, expected)


def test_옛_오븐은_용량_칸이_null이고_퍼센트와_이유는_그대로다():
    """오븐 0.5.1 전에는 percent 만 쓴다. 그래도 기존 download_percent·reason 은 바뀌지 않는다."""
    item = {"metadata": {"name": "m"},
            "status": {"phase": "Downloading", "reason": "JobRunning", "download": {"percent": 40}}}
    [row] = gpu.served_models(_list(item))
    assert (row["download_percent"], row["reason"]) == (40, "JobRunning")
    assert (row["download_bytes"], row["download_total_bytes"]) == (None, None)


def test_시간이_지나_멈춘_ServedModel은_상태와_시각을_그대로_싣는다():
    """stops_at 은 오퍼레이터가 쓴 값만 싣는다 — maxRuntime·연장 annotation 을 아는 쪽은 오퍼레이터뿐이라 계산하지 않는다."""
    item = {"metadata": {"name": "m"}, "spec": {"maxRuntime": "2h"},
            "status": {"phase": "Stopped", "reason": "MaxRuntimeReached", "startedAt": "2026-11-04T01:00:00Z"}}
    [row] = gpu.served_models(_list(item))
    assert (row["phase"], row["reason"]) == ("Stopped", "MaxRuntimeReached")
    assert (row["started_at"], row["stops_at"]) == ("2026-11-04T01:00:00Z", None)


@pytest.mark.parametrize("value", [None, "", 1730682000, True, {}])
def test_ServedModel_시각_칸은_문자열만_싣는다(value):
    [row] = gpu.served_models(_list({"metadata": {"name": "m"}, "status": {"startedAt": value, "stopsAt": value}}))
    assert (row["started_at"], row["stops_at"]) == (None, None)


@pytest.mark.parametrize(("stderr", "expected"), [
    ('error: the server doesn\'t have a resource type "servedmodels"', True),
    ('Error from server (Forbidden): servedmodels.serving.kukie.io is forbidden: cannot list resource', True),
    ("Unable to connect to the server: net/http: TLS handshake timeout", False),
    ("error: unexpected response from gateway: 403 Forbidden", False),   # 사이 프록시의 문구로 hasGpu 를 덮지 않는다
    ('Error from server (NotFound): Unable to list "serving.kukie.io/v1alpha1, Resource=servedmodels": '
     "the server could not find the requested resource", False),
    ("", False),
])
def test_ServedModel_조회_실패는_없다고_확인된_것과_모르는_것을_가른다(stderr, expected):
    assert gpu.served_models_absent(stderr) is expected


# ── Karpenter GPU 풀 (DURO-184) ─────────────────────────────

def _pool(name: str = "p", *, limits: dict | None = None, taints: list | None = None,
          requirements: list | None = None) -> dict:
    return {"metadata": {"name": name},
            "spec": {"limits": limits or {}, "template": {"spec": {"taints": taints or [], "requirements": requirements or []}}}}


FAMILY, INSTANCE_TYPE = "karpenter.k8s.aws/instance-family", "node.kubernetes.io/instance-type"


def _picks(key: str, *values: str, operator: str = "In") -> list[dict]:
    return [{"key": key, "operator": operator, "values": list(values)}]


def test_NodePool_목록에서_GPU_풀만_이름순으로_싣는다():
    assert gpu.node_pools(fixture("node_pools.json")) == ([
        {"name": "batch-gpu", "gpu_limit": None},      # 계열로만 알아본 풀 — limits 가 없다
        {"name": "gpu", "gpu_limit": 2},
    ], False)                                          # default 는 CPU 풀이다. NotIn 으로 GPU 계열을 뺀 것이 GPU 풀이 되면 안 된다
    assert gpu.NODE_POOLS_COMMAND == ["get", "nodepools.karpenter.sh", "-o", "json"]


@pytest.mark.parametrize("pool", [
    _pool(taints=[{"key": "nvidia.com/gpu", "effect": "NoSchedule"}]),
    _pool(limits={"nvidia.com/gpu": "4"}),
    _pool(requirements=_picks(FAMILY, "g6e")),
    _pool(requirements=_picks(FAMILY, "p4d")),
    _pool(requirements=_picks(FAMILY, "gr6")),
    _pool(requirements=_picks(INSTANCE_TYPE, "g6e.xlarge")),
    _pool(requirements=_picks(INSTANCE_TYPE, "m5.large", "p5.48xlarge")),    # 섞여 있어도 GPU 노드를 띄울 수 있다
    _pool(taints=[{"key": "amd.com/gpu", "effect": "NoSchedule"}]),
    _pool(requirements=_picks("karpenter.k8s.aws/instance-category", "g", "p")),
    _pool(requirements=_picks("karpenter.k8s.aws/instance-gpu-count", "0", operator="Gt")),
    _pool(requirements=_picks("karpenter.k8s.aws/instance-gpu-memory", "24000", operator="Lt")),   # 큰 GPU 를 빼도 GPU 풀이다
    _pool(requirements=_picks("karpenter.k8s.aws/instance-gpu-manufacturer", "nvidia")),
    _pool(requirements=[{"key": "karpenter.k8s.aws/instance-gpu-name", "operator": "Exists"}]),
])
def test_GPU_풀로_본다(pool):
    assert [row["name"] for row in gpu.node_pools(_list(pool))[0]] == ["p"]


@pytest.mark.parametrize("pool", [
    _pool(),
    _pool(limits={"cpu": "1000"}, taints=[{"key": "dedicated", "value": "batch", "effect": "NoSchedule"}]),
    _pool(requirements=_picks(FAMILY, "g6e", "p5", operator="NotIn")),       # 일반 풀이 GPU 를 빼는 흔한 설정
    _pool(requirements=[{"key": FAMILY, "operator": "Exists"}]),
    _pool(requirements=_picks(FAMILY, "m7g", "c7gn", "hpc7g", "inf2")),      # Graviton 은 g 로 끝나지 시작하지 않는다
    _pool(requirements=_picks("kubernetes.io/arch", "g6e")),                 # 인스턴스 종류를 고르는 키가 아니다
    _pool(requirements=_picks("karpenter.k8s.aws/instance-category", "c", "m", "r")),
    _pool(requirements=_picks("karpenter.k8s.aws/instance-category", "g", "p", operator="NotIn")),
    _pool(requirements=[{"key": "karpenter.k8s.aws/instance-gpu-count", "operator": "DoesNotExist"}]),   # GPU 를 뺀다
    _pool(requirements=_picks("karpenter.k8s.aws/instance-gpu-manufacturer", "nvidia", operator="NotIn")),
    {"spec": {"limits": {"nvidia.com/gpu": 1}}},                             # 이름이 없다
])
def test_GPU_풀로_보지_않는다(pool):
    assert gpu.node_pools(_list(pool)) == ([], False)


@pytest.mark.parametrize(("limit", "expected"), [(2, 2), ("2", 2), ("1k", None), (1.5, None), (True, None),
                                                 (-1, None), (None, None), ("", None)])
def test_GPU_상한은_정수로_읽히는_것만_싣는다(limit, expected):
    assert gpu.node_pools(_list(_pool(limits={"nvidia.com/gpu": limit}))) == ([{"name": "p", "gpu_limit": expected}], False)


@pytest.mark.parametrize("limits", [{"nvidia.com/gpu": 0}, {"nvidia.com/gpu": "0"}, {"cpu": 0}, {"cpu": "0"},
                                    {"memory": "0Gi"}, {"cpu": "0.0", "nvidia.com/gpu": 2}])
def test_limits의_어느_자원이든_0이면_꺼_둔_풀이라_응답에서_뺀다(limits):
    """Karpenter 는 limits 를 넘는 노드를 띄우지 않는다 — 풀을 멈춰 두는 흔한 방법이다 (agent#139 리뷰)."""
    off = _pool("off", limits=limits, requirements=_picks(FAMILY, "g6e"))
    assert gpu.node_pools(_list(off)) == ([], True)
    assert gpu.node_pools(_list(off, _pool("on", limits={"nvidia.com/gpu": 2}))) == ([{"name": "on", "gpu_limit": 2}], True)


def test_CPU_풀을_꺼_둔_것은_GPU_풀과_상관없다():
    assert gpu.node_pools(_list(_pool("cpu", limits={"cpu": 0}))) == ([], False)
    assert gpu.node_pools(_list(_pool("p", limits={"cpu": "10", "memory": "100Gi", "nvidia.com/gpu": 1}))) == (
        [{"name": "p", "gpu_limit": 1}], False)


@pytest.mark.parametrize("broken", ["", "not json", "{}", '{"items": {}}', '{"items": ["x"]}', "[]"])
def test_NodePool_목록_모양이_아니면_ValueError다(broken):
    with pytest.raises(ValueError):
        gpu.node_pools(broken)


@pytest.mark.parametrize(("stderr", "expected"), [
    ('error: the server doesn\'t have a resource type "nodepools"', True),
    ('Error from server (Forbidden): nodepools.karpenter.sh is forbidden: cannot list resource', True),
    ("Unable to connect to the server: net/http: TLS handshake timeout", False),
    ("error: unexpected response from gateway: 403 Forbidden", False),
    ("", False),
])
def test_NodePool_조회_실패는_없다고_확인된_것과_모르는_것을_가른다(stderr, expected):
    assert gpu.node_pools_absent(stderr) is expected


@pytest.mark.parametrize(("stderr", "expected"), [
    ('error: the server doesn\'t have a resource type "nodepools"', True),
    ('Error from server (Forbidden): nodepools.karpenter.sh is forbidden: cannot list resource', False),   # 거부는 모른다 — null
    ("Unable to connect to the server: net/http: TLS handshake timeout", False),
    ("", False),
])
def test_응답의_빈_배열은_CRD가_없을_때뿐이다(stderr, expected):
    assert gpu.node_pools_crd_missing(stderr) is expected


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
        ("llm", "qwen-0", 8000, L40S_4, ":0"),                             # 명령 `vllm serve`, 포트를 안 적으면 기본 8000
        ("llm", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", 8080, L40S_1, ":0"),    # --port 8080. 끝은 pod_identity (픽스처엔 uid 없음)
    ]                                                                      # Running 이 아닌 파드는 빠진다
    # GPU 노드 밖의 vllm 파드(`vllm bench` Job 등)는 대상 칸을 먹지 않는다 (PR #118 리뷰)
    assert gpu.vllm_targets(pods(), {L40S_1}) == [("llm", "vllm-gpt-oss-20b-7c9d5b8f6-q2k8m", 8080, L40S_1, ":0")]


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

def test_vLLM_지표로_서빙_줄을_만들고_p95는_두_수집의_차이로_낸다():
    """첫 수집은 기억이 없어 null, 두 번째부터 직전 수집과의 차이(최근 구간)로 p95 를 낸다 (DURO-124)."""
    rows, first = gpu.serving_rows(fixture("vllm.txt"), pod="vllm-x", node=L40S_1, at=100.0)
    assert rows == [{
        "model": "openai/gpt-oss-20b", "pod": "vllm-x", "node": L40S_1, "running": 24, "waiting": 3,
        "kv_cache_pct": 6.3, "ttft_p95_ms": None, "itl_p95_ms": None, "latency_window_seconds": None,
    }]
    assert first.at == 100.0 and set(first.histograms) == {("openai/gpt-oss-20b", "ttft"), ("openai/gpt-oss-20b", "itl")}
    assert first.histograms[("openai/gpt-oss-20b", "ttft")] == gpu.Histogram(
        {0.1: 10, 0.25: 40, 0.5: 90, 0.75: 96, 1.0: 100, math.inf: 100}, count=100, sum=31.2)

    rows, second = gpu.serving_rows(fixture("vllm_later.txt"), pod="vllm-x", node=L40S_1, previous=first, at=120.0)
    assert rows == [{
        "model": "openai/gpt-oss-20b", "pod": "vllm-x", "node": L40S_1, "running": 8, "waiting": 0,
        "kv_cache_pct": 2.1,
        "ttft_p95_ms": 975.0,       # 구간의 20개 중 19번째가 0.75s(10)~1.0s(20) 사이 → 0.75 + 0.25 × 9/10. 누적이면 892.9
        "itl_p95_ms": 50.0,         # 구간의 40개 중 38번째가 +Inf 버킷(0.05s 까지 30) → 가장 큰 유한 상한
        "latency_window_seconds": 20.0,
    }]
    assert second.histograms[("openai/gpt-oss-20b", "ttft")].count == 120   # 기억은 늘 누적값 그대로


def test_구간에_요청이_없으면_p95는_null이고_구간_길이는_있다():
    """앱이 "최근 요청 없음" 으로 그린다 — 기억이 없어 모르는 것(둘 다 null)과 다르다."""
    _, first = gpu.serving_rows(fixture("vllm.txt"), pod="p", node="n", at=0.0)
    [row], _ = gpu.serving_rows(fixture("vllm.txt"), pod="p", node="n", previous=first, at=20.0)
    assert (row["ttft_p95_ms"], row["itl_p95_ms"], row["latency_window_seconds"]) == (None, None, 20.0)


def test_값이_줄었으면_프로세스가_다시_뜬_것이라_이번은_null이고_기억은_이번_값으로_바뀐다():
    _, before = gpu.serving_rows(fixture("vllm_later.txt"), pod="p", node="n", at=0.0)
    [row], restarted = gpu.serving_rows(fixture("vllm.txt"), pod="p", node="n", previous=before, at=20.0)
    assert (row["ttft_p95_ms"], row["itl_p95_ms"], row["latency_window_seconds"]) == (None, None, None)
    assert restarted.histograms[("openai/gpt-oss-20b", "ttft")].count == 100
    # 돌려준 기억으로 다음 수집을 이으면 다시 구간이 생긴다
    [row], _ = gpu.serving_rows(fixture("vllm_later.txt"), pod="p", node="n", previous=restarted, at=40.0)
    assert (row["ttft_p95_ms"], row["latency_window_seconds"]) == (975.0, 20.0)


def test_히스토그램_하나만_줄어도_그_파드_전체가_null이다():
    """TTFT 는 그대로인데 ITL 만 줄었다 — 같은 프로세스의 히스토그램이라 하나만 이을 수 없다."""
    ttft = ('vllm:time_to_first_token_seconds_bucket{le="1",model_name="m"} 10\n'
            'vllm:time_to_first_token_seconds_bucket{le="+Inf",model_name="m"} 10\n'
            'vllm:time_to_first_token_seconds_count{model_name="m"} 10\n')
    itl = ('vllm:inter_token_latency_seconds_bucket{le="0.05",model_name="m"} %d\n'
           'vllm:inter_token_latency_seconds_bucket{le="+Inf",model_name="m"} %d\n')
    _, before = gpu.serving_rows(ttft + itl % (50, 50), pod="p", node="n", at=0.0)
    [row], _ = gpu.serving_rows(ttft + itl % (5, 5), pod="p", node="n", previous=before, at=20.0)
    assert (row["ttft_p95_ms"], row["itl_p95_ms"], row["latency_window_seconds"]) == (None, None, None)


def test_직전_수집에_없던_모델은_다음_수집부터_구간이_있다():
    one = 'vllm:time_to_first_token_seconds_bucket{le="+Inf",model_name="a"} 10\n'
    two = one + 'vllm:time_to_first_token_seconds_bucket{le="+Inf",model_name="b"} 3\n'
    _, before = gpu.serving_rows(one, pod="p", node="n", at=0.0)
    rows, _ = gpu.serving_rows(two, pod="p", node="n", previous=before, at=20.0)
    assert [(r["model"], r["latency_window_seconds"]) for r in rows] == [("a", 20.0), ("b", None)]


def test_엔진이_늘어_같은_le의_합이_뛴_것은_요청으로_센다():
    """데이터 병렬 엔진이 늘면 같은 le 의 합이 뛴다 — 줄어든 것이 아니라 그만큼 요청이 있었던 것으로 본다."""
    line = 'vllm:time_to_first_token_seconds_bucket{engine="%s",le="%s",model_name="m"} %d\n'
    before_text = line % (0, "0.5", 10) + line % (0, "+Inf", 10)
    after_text = before_text + line % (1, "0.5", 4) + line % (1, "+Inf", 5)
    _, before = gpu.serving_rows(before_text, pod="p", node="n", at=0.0)
    [row], _ = gpu.serving_rows(after_text, pod="p", node="n", previous=before, at=20.0)
    assert row["ttft_p95_ms"] == 500.0                                      # 구간 5개 중 4.75번째 → +Inf 버킷 → 0.5s


def test_le_집합이_달라진_히스토그램은_잇지_않는다():
    """직전에 없던 le 를 0 부터 빼면 차이 버킷이 위 le 보다 커져 p95 가 낮게 나온다 (agent#137 리뷰) — 그 키는 이번엔 구간 없음."""
    line = 'vllm:time_to_first_token_seconds_bucket{le="%s",model_name="m"} %d\n'
    before_text = line % ("0.5", 10) + line % ("+Inf", 10)
    after_text = line % ("0.25", 8) + line % ("0.5", 12) + line % ("+Inf", 12)     # 버킷 설정이 다른 vLLM
    _, before = gpu.serving_rows(before_text, pod="p", node="n", at=0.0)
    [row], after = gpu.serving_rows(after_text, pod="p", node="n", previous=before, at=20.0)
    assert (row["ttft_p95_ms"], row["latency_window_seconds"]) == (None, None)
    [row], _ = gpu.serving_rows(after_text, pod="p", node="n", previous=after, at=40.0)
    assert (row["ttft_p95_ms"], row["latency_window_seconds"]) == (None, 20.0)   # 다음 수집부터 잇는다


def test_옛_vLLM_지표_이름도_읽고_엔진이_여럿이면_합친다():
    def text(count: int) -> str:
        return "\n".join([
            'vllm:num_requests_running{engine="0",model_name="m"} 2',
            'vllm:num_requests_running{engine="1",model_name="m"} 3',
            'vllm:gpu_cache_usage_perc{engine="0",model_name="m"} 0.2',
            'vllm:gpu_cache_usage_perc{engine="1",model_name="m"} 0.4',
            f'vllm:time_per_output_token_seconds_bucket{{engine="0",le="0.05",model_name="m"}} {count}',
            f'vllm:time_per_output_token_seconds_bucket{{engine="0",le="+Inf",model_name="m"}} {count}',
            f'vllm:time_per_output_token_seconds_bucket{{engine="1",le="0.05",model_name="m"}} {count}',
            f'vllm:time_per_output_token_seconds_bucket{{engine="1",le="+Inf",model_name="m"}} {count}',
        ])
    _, before = gpu.serving_rows(text(10), pod="p", node="n", at=0.0)
    [row], _ = gpu.serving_rows(text(20), pod="p", node="n", previous=before, at=20.0)
    assert (row["running"], row["waiting"], row["kv_cache_pct"]) == (5, 0, 30.0)
    assert row["ttft_p95_ms"] is None                                       # TTFT 히스토그램이 없다
    assert row["itl_p95_ms"] == 47.5                                        # 구간 20개 중 19번째, 0~0.05 사이


def test_model_name이_없는_info_지표로_유령_줄이_생기지_않는다():
    """실제 vLLM 은 model_name 라벨 없는 info 지표를 늘 낸다 (PR #118 리뷰)."""
    text = fixture("vllm.txt") + "\n".join([
        'vllm:cache_config_info{block_size="16",cache_dtype="auto",engine="0",num_gpu_blocks="1234"} 1.0',
        'vllm:lora_requests_info{max_lora="1",running_lora_adapters="",waiting_lora_adapters=""} 1.0',
    ])
    assert [r["model"] for r in gpu.serving_rows(text, pod="vllm-x", node="n")[0]] == ["openai/gpt-oss-20b"]


def test_무한대_값은_읽지_않는다():
    """±Inf 가 round() 에 들어가면 OverflowError 로 개요 전체가 500 이다 (PR #118 리뷰)."""
    row = {"memory_mib": 0}
    gpu.apply_dcgm(row, 'DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-A"} +Inf\nDCGM_FI_DEV_GPU_TEMP{gpu="0",UUID="GPU-A"} 60')
    assert (row["memory_used_mib"], row["temperature_c"]) == (None, 60)
    [model], _ = gpu.serving_rows('vllm:num_requests_running{model_name="m"} -Inf\n'
                                  'vllm:num_requests_waiting{model_name="m"} 2', pod="p", node="n")
    assert (model["running"], model["waiting"]) == (0, 2)


def test_vLLM_지표가_없는_파드는_줄이_없다():
    assert gpu.serving_rows("http_requests_total 3\n", pod="router", node="n") == ([], gpu.LatencySample(0.0, {}))


def test_파드의_정체는_uid와_vLLM_컨테이너의_재시작_수다():
    """사이드카(메시·로그)가 다시 떠도 vLLM 프로세스는 그대로라 세지 않는다 (agent#137 Codex 리뷰). init 컨테이너는
    vllm_targets 가 보지 않는 범위라 여기서도 세지 않는다."""
    item = {"metadata": {"name": "qwen-0", "uid": "u1"},
            "spec": {"containers": [{"name": "server", "command": ["vllm", "serve"]}, {"name": "istio-proxy", "image": "istio/proxyv2"}],
                     "initContainers": [{"name": "warm", "image": "vllm/vllm-openai"}]},
            "status": {"startTime": "2026-11-03T00:00:00Z",
                       "containerStatuses": [{"name": "server", "restartCount": 2}, {"name": "istio-proxy", "restartCount": 7}],
                       "initContainerStatuses": [{"name": "warm", "restartCount": 1}]}}
    assert gpu.pod_identity(item) == "u1:2"
    assert gpu.pod_identity({"metadata": {"name": "p"}, "status": {"startTime": "2026-11-03T00:00:00Z"}}) == "2026-11-03T00:00:00Z:0"
    assert gpu.pod_identity({"metadata": {"name": "p"}}) == ":0"


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
NO_CRD = 'error: the server doesn\'t have a resource type "servedmodels"'
NO_POOL_CRD = 'error: the server doesn\'t have a resource type "nodepools"'


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
    if args == gpu.SERVED_MODELS_COMMAND:
        return _result(stderr=NO_CRD, success=False)
    if args == gpu.NODE_POOLS_COMMAND:
        return _result(stderr=NO_POOL_CRD, success=False)
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
    assert r.json()["gpu"] == {"nodes": [], "serving": [], "benchmarks": [], "served_models": None, "node_pools": [], "waiting": [
        {"namespace": "llm", "pod": "vllm-gpt-oss-20b-b897b5d97-kkjsq", "gpus": 1, "phase": "Pending"}]}
    assert not any(args[1] == "--raw" for args in seen)
    assert [body["hasGpu"] for body in _gpu_calls(fake_server)] == ([True] if stored is None else [])
    assert fake_server.clusters[cluster]["hasGpu"] is True


def _served_models_cluster(served: KubectlResult, *, nodes: str = "gpu_nodes.json", pods: str = "{}",
                           pools: KubectlResult = _result(stderr=NO_POOL_CRD, success=False)):
    """(run, 부른 명령) — ServedModel·NodePool 조회 결과를 정해 둔 가짜 클러스터. 지표는 빈 값이다."""
    seen: list[tuple[list[str], int]] = []

    def run(args, timeout):
        seen.append((args, timeout))
        if args == gpu.NODES_COMMAND:
            return _result(fixture(nodes))
        if args == gpu.PODS_COMMAND:
            return _result(pods)
        if args == gpu.SERVED_MODELS_COMMAND:
            return served
        if args == gpu.NODE_POOLS_COMMAND:
            return pools
        return _result("")

    return run, seen


def test_개요_gpu_칸에_ServedModel을_싣는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(namespace="shop")
    seen: list[tuple[list[str], int]] = []

    def fake(args, *, context, timeout=30, kubeconfig=None, **kwargs):
        seen.append((args, timeout))
        return _result(fixture("served_models.json")) if args == gpu.SERVED_MODELS_COMMAND else _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    assert [(m["namespace"], m["name"], m["phase"]) for m in r.json()["gpu"]["served_models"]] == [
        ("a-team", "gpt-oss-20b", "Failed"), ("llm", "gpt-oss-20b", "Ready"),
        ("llm", "just-created", ""), ("llm", "qwen3", "Downloading")]
    assert [args for args, _ in seen].count(gpu.SERVED_MODELS_COMMAND) == 1
    assert dict((tuple(args), t) for args, t in seen)[tuple(gpu.SERVED_MODELS_COMMAND)] == clusters_api.GPU_SERVED_MODELS_TIMEOUT
    assert gpu.SERVED_MODELS_COMMAND == ["get", "servedmodels.serving.kukie.io", "-A", "-o", "json"]


@pytest.mark.parametrize("served", [
    _result(stderr=NO_CRD, success=False),               # 애드온을 안 깔았다
    _result(stderr=FORBIDDEN, success=False),            # 권한이 없다
    _result(stderr="context deadline exceeded", success=False),
    _result("not json"),
    _result("{}"),
])
def test_ServedModel을_못_읽어도_그_칸만_null이고_나머지는_그대로다(served):
    run, _ = _served_models_cluster(served, pods=fixture("gpu_pods.json"))
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert has_gpu is True and body["served_models"] is None
    assert [n["node"] for n in body["nodes"]] == [L40S_1, L40S_4]


def test_ServedModel은_지표보다_먼저_읽어_지표가_매달려도_읽힌다(monkeypatch):
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args[1] == "--raw":
            now[0] += timeout
            return _result(stderr="timed out", success=False)
        if args == gpu.SERVED_MODELS_COMMAND:
            return _result(fixture("served_models.json"))
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert len(body["served_models"]) == 4
    assert seen.index(gpu.SERVED_MODELS_COMMAND) < min(i for i, args in enumerate(seen) if args[1] == "--raw")


def test_ServedModel_조회는_남은_예산만큼만_기다리고_예산이_없으면_건너뛴다():
    now = [0.0]
    seen: list[tuple[list[str], int]] = []

    def run(args, timeout):
        seen.append((args, timeout))
        if args == gpu.PODS_COMMAND:
            now[0] = clusters_api.GPU_BUDGET - spare[0]
        return _gpu_cluster(args)

    spare = [6]                                              # 파드를 읽고 나니 6초 남았다 — 노드가 있으니 절반까지만
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert dict((tuple(args), t) for args, t in seen)[tuple(gpu.SERVED_MODELS_COMMAND)] == 3
    assert len(body["nodes"]) == 2

    now[0], spare[0] = 0.0, 0                                # 하나도 안 남았다
    seen.clear()
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert gpu.SERVED_MODELS_COMMAND not in [args for args, _ in seen]
    assert body["served_models"] is None and len(body["nodes"]) == 2


def test_ServedModel이_매달려도_지표를_읽을_예산이_남는다(monkeypatch):
    """파드 조회가 매달려 2초만 남은 라운드 — ServedModel 이 다 쓰면 지표가 전부 빈다 (agent#138 리뷰)."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    raw: list[int] = []

    def run(args, timeout):
        if args == gpu.PODS_COMMAND:
            now[0] = clusters_api.GPU_BUDGET - 2
        if args == gpu.SERVED_MODELS_COMMAND:
            now[0] += timeout
            return _result(stderr="timed out", success=False)
        if args[1] == "--raw":
            raw.append(timeout)
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert body["served_models"] is None and raw


def test_GPU로_알려진_꺼진_클러스터는_namespace_탐색이_ServedModel을_읽을_예산을_남긴다(monkeypatch):
    """노드가 없으면 탐색이 예산을 다 쓰는데, 그러면 꺼진 클러스터에서 ServedModel 을 못 읽는다 (agent#138 리뷰).

    가짜 시계는 호출마다 시간이 쌓인다고 보므로 탐색을 차례로 돌린다."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    served: list[int] = []

    def run(args, timeout):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces(*(f"ns-{i}" for i in range(20))))
        if args == gpu.SERVED_MODELS_COMMAND:
            served.append(timeout)
            return _result(fixture("served_models.json"))
        now[0] += timeout
        return _result(stderr="timed out", success=False)

    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: now[0])
    assert served == [clusters_api.GPU_SERVED_MODELS_TIMEOUT]
    assert has_gpu is True and len(body["served_models"]) == 4


def test_파드_목록이_깨진_꺼진_클러스터도_읽힌_ServedModel은_싣고_waiting은_빈_배열이다():
    """waiting 은 늘 배열이다 — 노드가 있는 라운드와 같은 규칙 (agent#138 리뷰)."""
    run, _ = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json", pods="not json")
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert has_gpu is True and body["waiting"] == [] and len(body["served_models"]) == 4


def test_GPU로_알려진_꺼진_클러스터는_기다리는_파드가_없어도_ServedModel을_읽는다():
    """노드 0대, 파드 없음 — 내려받는 중이거나 Unschedulable 로 멈춘 모델을 화면이 보여야 한다."""
    run, _ = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json")
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert has_gpu is True and body["nodes"] == [] and body["waiting"] == []
    assert len(body["served_models"]) == 4


def test_GPU로_알려진_꺼진_클러스터에_ServedModel이_하나도_없어도_빈_배열로_답하고_GPU_클러스터로_둔다():
    """모델을 지우고 노드를 끈 뒤 — 애드온이 깔린 GPU 클러스터다. GPU 없음으로 덮으면 다음부터 CRD 를 읽지 않는다."""
    run, _ = _served_models_cluster(_result('{"items": []}'), nodes="nodes.json")
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert has_gpu is True
    assert body == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [], "served_models": [], "node_pools": []}


@pytest.mark.parametrize(("served", "expected"), [
    (_result(stderr=NO_CRD, success=False), False),      # 애드온도 노드도 파드도 없다 — 지금까지처럼 GPU 없음
    (_result(stderr=FORBIDDEN, success=False), False),   # 볼 수 없는 곳은 못 본 곳으로 치지 않는다
    (_result(stderr="context deadline exceeded", success=False), None),   # 한 번의 실패로 GPU 없음으로 덮지 않는다
    (_result("not json"), None),
])
def test_GPU로_알려진_꺼진_클러스터에서_ServedModel을_못_읽으면_까닭에_따라_GPU_여부가_갈린다(served, expected):
    run, _ = _served_models_cluster(served, nodes="nodes.json")
    assert clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0) == (None, expected)


def test_GPU로_알려진_꺼진_클러스터도_예산이_없어_ServedModel을_못_읽었으면_모른다():
    now = [0.0]

    def run(args, timeout):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        now[0] = clusters_api.GPU_BUDGET
        return _result("{}")

    assert clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: now[0]) == (None, None)


def test_노드_0대에_GPU를_기다리는_파드가_있으면_server_값이_없어도_ServedModel을_읽는다():
    run, _ = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json",
                                    pods=_list(_vllm_pending()))
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=None, clock=lambda: 0.0)
    assert has_gpu is True and len(body["served_models"]) == 4 and len(body["waiting"]) == 1


@pytest.mark.parametrize(("known", "expected"), [(None, False), (False, False)])
def test_GPU_없는_일반_클러스터에는_ServedModel_조회가_늘지_않는다(known, expected):
    """파드를 찾는 라운드에도, 간격 안의 라운드에도 ServedModel CRD 를 묻지 않는다."""
    run, seen = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json")
    assert clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="plain:fp:ns", known=known,
                                     clock=lambda: 0.0) == (None, expected)
    # 이 프로세스에서 처음 — 파드와 NodePool 만 찾았다
    assert [args for args, _ in seen] == [gpu.NODES_COMMAND, gpu.PODS_COMMAND, gpu.NODE_POOLS_COMMAND]
    seen.clear()
    assert clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="plain:fp:ns", known=known,
                                     clock=lambda: 1.0)[0] is None
    assert [args for args, _ in seen] == [gpu.NODES_COMMAND]                           # 간격 안 — 노드만 본다


# ── Karpenter GPU 풀만 있는 클러스터 (DURO-184) ─────────────

GPU_POOLS = _result(_list(_pool("gpu", limits={"nvidia.com/gpu": 2})))


@pytest.mark.parametrize("known", [None, False, True])
def test_노드도_대기_파드도_모델도_없어도_GPU_풀이_있으면_GPU_클러스터다(known):
    """모델을 시작하기 전의 Karpenter 클러스터 — 이 길이 없으면 GPU 화면으로 들어가지 못해 모델을 시작할 수 없다."""
    run, seen = _served_models_cluster(_result(stderr=NO_CRD, success=False), nodes="nodes.json", pools=GPU_POOLS)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"pool-only:{known}", known=known,
                                              clock=lambda: 0.0)
    assert has_gpu is True
    assert body == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [], "served_models": None,
                    "node_pools": [{"name": "gpu", "gpu_limit": 2}]}
    assert [args for args, _ in seen] == [gpu.NODES_COMMAND, gpu.PODS_COMMAND, gpu.NODE_POOLS_COMMAND,
                                          gpu.SERVED_MODELS_COMMAND]   # GPU 풀을 찾은 라운드에 ServedModel 도 읽는다


def test_GPU_없음으로_남은_클러스터도_재확인_주기에_GPU_풀을_찾아_server를_true로_올린다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(has_gpu=False)

    def fake(args, **kwargs):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result('{"items": []}')
        if args == gpu.NODE_POOLS_COMMAND:
            return _result(fixture("node_pools.json"))
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    assert r.json()["gpu"]["nodes"] == []
    assert r.json()["gpu"]["node_pools"] == [{"name": "batch-gpu", "gpu_limit": None}, {"name": "gpu", "gpu_limit": 2}]
    assert [body["hasGpu"] for body in _gpu_calls(fake_server)] == [True]
    assert fake_server.clusters[cluster]["hasGpu"] is True


def test_GPU_풀을_나중에_만들면_재확인_주기가_지나야_보이고_그_사이에는_kubectl이_늘지_않는다():
    now = [0.0]
    pools = [_result(stderr=NO_POOL_CRD, success=False)]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.NODE_POOLS_COMMAND:
            return pools[0]
        if args == gpu.SERVED_MODELS_COMMAND:
            return _result(stderr=NO_CRD, success=False)
        return _result('{"items": []}')

    def call():
        return clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="later:fp:ns", known=False, clock=lambda: now[0])

    assert call() == (None, False)
    pools[0] = GPU_POOLS                                     # Karpenter 와 GPU 풀을 깔았다
    seen.clear()
    now[0] += clusters_api.GPU_ABSENT_RECHECK - 1
    assert call() == (None, False) and seen == [gpu.NODES_COMMAND]   # 간격 안 — 노드만 본다
    now[0] += 1
    body, has_gpu = call()
    assert has_gpu is True and body["node_pools"] == [{"name": "gpu", "gpu_limit": 2}]


@pytest.mark.parametrize("known", [None, False])
def test_일반_클러스터는_재확인_주기마다_NodePool을_한_번만_읽는다(known):
    """개요를 몇 번 불러도 간격 안에서는 노드 목록뿐이다 — 늘어나는 것은 GPU_ABSENT_RECHECK 마다 한 번이다."""
    now = [0.0]
    run, seen = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json")

    def counts() -> dict[str, int]:
        return {name: sum(args == command for args, _ in seen)
                for name, command in (("nodes", gpu.NODES_COMMAND), ("pods", gpu.PODS_COMMAND), ("pools", gpu.NODE_POOLS_COMMAND),
                                      ("served", gpu.SERVED_MODELS_COMMAND))}

    for _ in range(30):                                      # 20초마다 10분 — 간격을 한 번 채우기 전까지
        clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"plain-count:{known}", known=known, clock=lambda: now[0])
        now[0] += 20
    assert clusters_api.GPU_ABSENT_RECHECK == 600
    assert counts() == {"nodes": 30, "pods": 1, "pools": 1, "served": 0}
    assert len(seen) == 32


@pytest.mark.parametrize("known", [None, False])
def test_CPU_풀만_있는_Karpenter_클러스터는_GPU_클러스터가_아니다(known):
    pools = _result(_list(_pool("default", limits={"cpu": "1000"}, requirements=_picks(FAMILY, "m5"))))
    run, seen = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json", pools=pools)
    assert clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"cpu-pools:{known}", known=known,
                                     clock=lambda: 0.0) == (None, False)
    assert gpu.SERVED_MODELS_COMMAND not in [args for args, _ in seen]


@pytest.mark.parametrize(("pools", "expected"), [
    (_result(stderr=NO_POOL_CRD, success=False), False),   # Karpenter 를 안 깔았다
    (_result(stderr=FORBIDDEN, success=False), False),     # 볼 수 없는 곳은 못 본 곳으로 치지 않는다
    (_result('{"items": []}'), False),
    (_result(stderr="context deadline exceeded", success=False), None),   # 못 읽은 풀이 GPU 풀일 수 있다 — false 로 덮지 않는다
    (_result("not json"), None),
    (_result(_list({"metadata": {"name": "p"}, "spec": "x"})), None),     # 객체가 아닌 spec 도 개요를 깨지 않는다
])
def test_NodePool을_못_읽으면_까닭에_따라_GPU_여부가_갈린다(pools, expected):
    run, _ = _served_models_cluster(_result(stderr=NO_CRD, success=False), nodes="nodes.json", pools=pools)
    assert clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"pool-fail:{expected}:{pools.stdout}{pools.stderr}",
                                     known=None, clock=lambda: 0.0) == (None, expected)


@pytest.mark.parametrize(("pools", "expected"), [
    (_result(stderr=NO_POOL_CRD, success=False), []),      # Karpenter 를 안 깐 곳 — 앱이 설치를 안내한다
    (_result('{"items": []}'), []),
    (_result(stderr=FORBIDDEN, success=False), None),      # 아래는 못 읽음 — 앱이 "확인하지 못했어요" 를 띄운다
    (_result(stderr="context deadline exceeded", success=False), None),
    (_result("not json"), None),
])
def test_CRD_없음과_GPU_풀_없음은_빈_배열이고_못_읽음만_null이다(pools, expected):
    run, _ = _served_models_cluster(_result('{"items": []}'), pods=fixture("gpu_pods.json"), pools=pools)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] == expected and len(body["nodes"]) == 2


def test_CRD가_없어도_GPU_클러스터의_hasGpu는_다른_근거가_있으면_true로_남고_빈_배열이_칸을_만들지_않는다():
    """응답이 [] 로 바뀌어도 hasGpu 판단과 gpu 칸 유무는 그대로다 — 칸은 다른 근거가 있을 때만 생긴다."""
    missing = _result(stderr=NO_POOL_CRD, success=False)
    run, _ = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json", pools=missing)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] == [] and len(body["served_models"]) == 4

    run, _ = _served_models_cluster(_result(fixture("served_models.json")), nodes="nodes.json", pods=_list(_vllm_pending()), pools=missing)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=None, clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] == [] and len(body["waiting"]) == 1


def test_GPU_노드가_있는_클러스터도_node_pools를_싣고_못_읽으면_그_칸만_null이다():
    run, _ = _served_models_cluster(_result('{"items": []}'), pods=fixture("gpu_pods.json"), pools=GPU_POOLS)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] == [{"name": "gpu", "gpu_limit": 2}] and len(body["nodes"]) == 2

    run, _ = _served_models_cluster(_result('{"items": []}'), pods=fixture("gpu_pods.json"),
                                    pools=_result(stderr="timed out", success=False))
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] is None and len(body["nodes"]) == 2


OFF_POOLS = _result(_list(_pool("gpu", limits={"nvidia.com/gpu": 0}, taints=[{"key": "nvidia.com/gpu"}])))


@pytest.mark.parametrize("known", [None, False, True])
def test_GPU_풀을_꺼_두면_칸을_만들지_않고_GPU_여부도_덮지_않는다(known):
    """앱이 "모델을 시작하면 노드가 켜진다" 고 안내하면 모델이 영구 Pending 이다. 그렇다고 "GPU 풀 없음" 도 아니다 —
    밤에 상한을 0 으로 내린 GPU 클러스터가 false 로 덮여 목록에서 빠지면 안 된다 (agent#139 리뷰)."""
    run, _ = _served_models_cluster(_result(stderr=NO_CRD, success=False), nodes="nodes.json", pools=OFF_POOLS)
    assert clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"pool-off:{known}", known=known,
                                     clock=lambda: 0.0) == (None, None)


def test_GPU_풀을_꺼_둔_곳도_ServedModel이_읽히면_node_pools는_빈_배열이다():
    """Karpenter 방식 안내를 띄우지 않는다 — 앱은 빈 배열을 "GPU 풀 없음" 으로 읽는다."""
    run, _ = _served_models_cluster(_result('{"items": []}'), nodes="nodes.json", pools=OFF_POOLS)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] == [] and body["served_models"] == []


@pytest.mark.parametrize("found", ["pools", "waiting"])
def test_GPU_클러스터를_처음_찾은_라운드도_ServedModel을_읽을_예산이_남는다(monkeypatch, found):
    """known 이 아직 true 가 아니어도 — 탐색과 NodePool 이 다 쓰면 첫 응답의 served_models 가 null 이다 (agent#139 Codex 리뷰).
    탐색이 남긴 NodePool 몫 5초를 둘이 나눈다."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    served: list[int] = []

    def run(args, timeout):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("llm", *(f"ns-{i}" for i in range(20))))
        if args == gpu.NODE_POOLS_COMMAND:
            now[0] += timeout
            return GPU_POOLS if found == "pools" else _result(stderr="timed out", success=False)
        if args == gpu.SERVED_MODELS_COMMAND:
            served.append(timeout)
            return _result(fixture("served_models.json"))
        if args[:4] == ["get", "pods", "-n", "llm"] and found == "waiting":
            return _result(_list(_vllm_pending()))
        now[0] += timeout
        return _result(stderr="timed out", success=False)

    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"first-round:{found}", known=False,
                                              clock=lambda: now[0])
    assert served == [3]
    assert has_gpu is True and len(body["served_models"]) == 4


def test_일반_클러스터의_namespace_탐색은_NodePool_몫만_남긴다(monkeypatch):
    """ServedModel 을 읽지 않는 곳이다 — 그 몫까지 떼면 탐색이 끝까지 못 가 hasGpu 를 false 로 확정하지 못한다 (agent#139 리뷰)."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]

    def run(args, timeout):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces("a", "b", "c"))
        if args == gpu.NODE_POOLS_COMMAND:
            return _result(stderr=NO_POOL_CRD, success=False)
        now[0] += clusters_api.GPU_PODS_TIMEOUT                # 셋 다 시간 제한까지 걸리지만 응답은 왔다 — 15초 안에 끝난다
        return _result('{"items": []}')

    assert clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="plain-search:fp:ns", known=None,
                                     clock=lambda: now[0]) == (None, False)


def test_파드_조회가_매달려_모자란_라운드도_NodePool은_읽는다():
    """GPU 풀만 있는 클러스터를 찾는 길이라 ServedModel 몫을 남기느라 건너뛰지 않는다 — 5초 남으면 2초, 나머지가 ServedModel 몫이다."""
    now = [0.0]
    timeouts: dict[str, int] = {}

    def run(args, timeout):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            now[0] = clusters_api.GPU_BUDGET - 5
            return _result(stderr="timed out", success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(stderr="timed out", success=False)
        timeouts[args[1]] = timeout
        now[0] += timeout
        return GPU_POOLS if args == gpu.NODE_POOLS_COMMAND else _result(stderr=NO_CRD, success=False)

    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="slow-pods:fp:ns", known=False, clock=lambda: now[0])
    assert timeouts == {"nodepools.karpenter.sh": 2, "servedmodels.serving.kukie.io": 3}
    assert has_gpu is True and body["node_pools"] == [{"name": "gpu", "gpu_limit": 2}]


def test_NodePool과_ServedModel이_함께_매달려도_지표_몫은_남은_예산의_절반이다(monkeypatch):
    """둘이 따로 절반씩 깎으면 지표 몫이 네 번째로 줄어 서빙 중인 모델이 빠진다 (agent#139 리뷰)."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    timeouts: dict[str, int] = {}
    left_for_metrics: list[float] = []

    def run(args, timeout):
        if args == gpu.PODS_COMMAND:
            now[0] = clusters_api.GPU_BUDGET - 16
        if args in (gpu.NODE_POOLS_COMMAND, gpu.SERVED_MODELS_COMMAND):
            timeouts[args[1]] = timeout
            now[0] += timeout
            return _result(stderr="timed out", success=False)
        if args[1] == "--raw":
            left_for_metrics.append(clusters_api.GPU_BUDGET - now[0])
        return _gpu_cluster(args)

    clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert timeouts == {"nodepools.karpenter.sh": 4, "servedmodels.serving.kukie.io": 4}
    assert left_for_metrics[0] == 8


def test_NodePool이_매달려도_ServedModel과_지표를_읽을_예산이_남는다(monkeypatch):
    """파드 조회가 매달려 4초만 남은 라운드 — 둘이 합쳐 절반, NodePool 은 그 절반까지만 쓴다."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    pool_timeouts: list[int] = []
    raw: list[int] = []

    def run(args, timeout):
        if args == gpu.PODS_COMMAND:
            now[0] = clusters_api.GPU_BUDGET - 4
        if args == gpu.NODE_POOLS_COMMAND:
            pool_timeouts.append(timeout)
            now[0] += timeout
            return _result(stderr="timed out", success=False)
        if args == gpu.SERVED_MODELS_COMMAND:
            return _result(fixture("served_models.json"))
        if args[1] == "--raw":
            raw.append(timeout)
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert pool_timeouts == [1] and raw
    assert body["node_pools"] is None and len(body["served_models"]) == 4


def test_NodePool_조회는_예산이_없으면_건너뛰고_GPU_없음으로_덮지_않는다():
    now = [0.0]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        now[0] = clusters_api.GPU_BUDGET
        return _result('{"items": []}')

    assert clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="pool-budget:fp:ns", known=False,
                                     clock=lambda: now[0]) == (None, None)
    assert gpu.NODE_POOLS_COMMAND not in seen


@pytest.mark.parametrize(("known", "expected"), [(None, 2), (True, clusters_api.GPU_NODE_POOLS_TIMEOUT)])
def test_namespace_탐색이_매달려도_NodePool을_읽을_예산이_남는다(monkeypatch, known, expected):
    """모든 namespace 파드를 못 읽는 곳 — 탐색이 예산을 다 쓰면 GPU 풀만 있는 클러스터를 찾지 못한다.

    가짜 시계는 호출마다 시간이 쌓인다고 보므로 탐색을 차례로 돌린다."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    pool_timeouts: list[int] = []

    def run(args, timeout):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces(*(f"ns-{i}" for i in range(20))))
        if args == gpu.NODE_POOLS_COMMAND:
            pool_timeouts.append(timeout)
            return GPU_POOLS
        if args == gpu.SERVED_MODELS_COMMAND:
            return _result(stderr=NO_CRD, success=False)
        now[0] += timeout
        return _result(stderr="timed out", success=False)

    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=known, clock=lambda: now[0])
    assert pool_timeouts == [expected]                       # known 이 true 가 아니면 남은 5초를 ServedModel 과 나눈다
    assert has_gpu is True and body["node_pools"] == [{"name": "gpu", "gpu_limit": 2}]


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


# ── 최근 구간 p95 의 기억 (DURO-124) ───────────────────────

def _serving_memory(monkeypatch):
    """(수집 함수, 시계, 가짜 클러스터 상태) — GPU 노드 위 vLLM 파드 하나의 지표가 round 마다 바뀐다. 지표는 vllm.txt 로 시작."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    state = {"metrics": fixture("vllm.txt"), "pods": fixture("gpu_pods.json"), "fail": False}

    def run(args, timeout):
        if args[1] == "--raw":
            now[0] += state.get("slow", {}).get(args[2].split("/pods/")[1].split(":")[0], 0)   # 그 파드 읽기에 걸린 시간
            if "vllm-gpt-oss" not in args[2] or state["fail"]:
                return _result(stderr="timed out", success=False)
            return _result(state["metrics"])
        if args == gpu.PODS_COMMAND:
            return _result(state["pods"])
        return _gpu_cluster(args)

    def collect(key="c:fp:ns"):
        body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=key, clock=lambda: now[0])
        if not body["serving"]:
            return None                                      # 지표를 못 읽은 라운드
        [row] = body["serving"]
        return row["ttft_p95_ms"], row["itl_p95_ms"], row["latency_window_seconds"]

    return collect, now, state


def test_개요는_파드마다_직전_수집을_기억해_최근_구간_p95를_낸다(monkeypatch):
    collect, now, state = _serving_memory(monkeypatch)
    assert collect() == (None, None, None)                   # 첫 수집 — 기억이 없다
    now[0] += 20
    assert collect() == (None, None, 20.0)                   # 그대로 — 구간에 요청이 없었다
    now[0] += 20
    state["metrics"] = fixture("vllm_later.txt")
    assert collect() == (975.0, 50.0, 20.0)                  # 직전 20초의 p95 (누적이면 892.9)
    now[0] += 20
    assert collect() == (None, None, 20.0)
    assert set(clusters_api._latency_samples) == {"c:fp:ns"}
    assert set(clusters_api._latency_samples["c:fp:ns"]) == {"llm/vllm-gpt-oss-20b-7c9d5b8f6-q2k8m/:0"}


def test_구간의_시각은_그_파드를_읽은_시각이다(monkeypatch):
    """다른 파드의 읽기가 느려 라운드가 늦게 끝나도 이 파드의 구간은 안 늘어난다 (agent#137 리뷰). 대상은 이름순이라
    zzz-vllm 이 vllm-gpt-oss 뒤에 읽힌다 — 두 번째 라운드에만 그 파드가 있고 읽기에 7초 걸린다."""
    collect, now, state = _serving_memory(monkeypatch)
    collect()
    pods = json.loads(state["pods"])
    pods["items"].append({"metadata": {"name": "zzz-vllm", "namespace": "llm"}, "status": {"phase": "Running"},
                          "spec": {"nodeName": L40S_1, "containers": [{"name": "vllm", "image": "vllm/vllm-openai"}]}})
    state["pods"], state["slow"] = json.dumps(pods), {"zzz-vllm": 7}
    now[0] += 20
    state["metrics"] = fixture("vllm_later.txt")
    assert collect() == (975.0, 50.0, 20.0)                  # 라운드 끝으로 재면 27


def test_지표를_한_번_못_읽어도_기억은_두고_다음_구간이_그만큼_길다(monkeypatch):
    collect, now, state = _serving_memory(monkeypatch)
    collect()
    now[0] += 20
    state["fail"] = True
    assert collect() is None                                 # 못 읽은 라운드
    now[0] += 20
    state["fail"] = False
    state["metrics"] = fixture("vllm_later.txt")
    assert collect() == (975.0, 50.0, 40.0)                  # 두 라운드 만큼의 구간


def test_파드가_다시_뜨면_기억을_잇지_않는다(monkeypatch):
    """같은 이름의 새 파드(uid 가 다르다)·컨테이너 재시작(restartCount)은 처음부터 다시 세는 프로세스다."""
    collect, now, state = _serving_memory(monkeypatch)
    collect()
    now[0] += 20
    pods = json.loads(state["pods"])
    for item in pods["items"]:
        if item["metadata"]["name"].startswith("vllm-gpt-oss"):
            item["status"]["containerStatuses"] = [{"name": "vllm", "restartCount": 1}]
    state["pods"] = json.dumps(pods)
    state["metrics"] = fixture("vllm_later.txt")             # 값은 늘었지만 다른 프로세스다
    assert collect() == (None, None, None)
    now[0] += 20
    assert collect() == (None, None, 20.0)                   # 새 정체로 다시 잇는다
    assert "llm/vllm-gpt-oss-20b-7c9d5b8f6-q2k8m/:1" in clusters_api._latency_samples["c:fp:ns"]


def test_값이_줄면_기억을_버리고_이번은_null이다(monkeypatch):
    collect, now, state = _serving_memory(monkeypatch)
    state["metrics"] = fixture("vllm_later.txt")
    collect()
    now[0] += 20
    state["metrics"] = fixture("vllm.txt")                   # 같은 파드인데 카운터가 줄었다 (uid·재시작 수가 그대로인 재기동)
    assert collect() == (None, None, None)
    now[0] += 20
    state["metrics"] = fixture("vllm_later.txt")
    assert collect() == (975.0, 50.0, 20.0)                  # 줄어든 값부터 다시 잇는다


def test_직전_수집이_너무_오래됐으면_기억을_버린다(monkeypatch):
    """대시보드를 닫았다 한참 뒤에 열면 "최근" 이 아니다 — LATENCY_WINDOW_MAX."""
    collect, now, state = _serving_memory(monkeypatch)
    collect()
    now[0] += clusters_api.LATENCY_WINDOW_MAX
    state["metrics"] = fixture("vllm_later.txt")
    assert collect() == (None, None, None)
    now[0] += 20
    assert collect() == (None, None, 20.0)                   # 버린 뒤의 수집부터 다시 잇는다


def test_연결_지문이_없으면_기억하지_않아_p95는_늘_null이다(monkeypatch):
    collect, now, state = _serving_memory(monkeypatch)
    assert collect(None) == (None, None, None)
    now[0] += 20
    state["metrics"] = fixture("vllm_later.txt")
    assert collect(None) == (None, None, None)
    assert clusters_api._latency_samples == {}


def test_오래된_다른_연결의_기억도_치운다(monkeypatch):
    collect, now, state = _serving_memory(monkeypatch)
    collect("old:fp:ns")
    now[0] += clusters_api.LATENCY_WINDOW_MAX - 1
    collect("live:fp:ns")
    assert set(clusters_api._latency_samples) == {"old:fp:ns", "live:fp:ns"}
    now[0] += 1
    collect("live:fp:ns")
    assert set(clusters_api._latency_samples) == {"live:fp:ns"}


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
