"""GPU 노드 목록을 응답 줄로 바꾸는 파싱"""
from __future__ import annotations

import pytest

from gpu_support import L40S_1, L40S_4, _list, fixture
from kukie_agent.clusters import gpu


def _node(name: str, *, labels: dict | None = None, allocatable: dict | None = None,
          capacity: dict | None = None) -> dict:
    return {"metadata": {"name": name, "labels": labels or {}},
            "status": {"allocatable": allocatable or {}, "capacity": capacity or {}}}


def test_GPU_자원이_있는_노드만_싣고_실시간_값은_아직_null이다():
    rows = gpu.gpu_nodes(fixture("gpu_nodes.json"))
    assert [r["node"] for r in rows] == [L40S_1, L40S_4]                      # CPU 노드는 빠진다, 이름순
    assert rows[0] == {"node": L40S_1, "product": "NVIDIA-L40S", "count": 1, "memory_mib": 46068,
                       "instance_type": "g6e.xlarge", "zone": "ap-northeast-2b", "capacity_type": "on-demand",
                       "utilization_pct": None, "memory_used_mib": None, "temperature_c": None, "power_w": None}


@pytest.mark.parametrize(("labels", "expected"), [
    ({"karpenter.sh/capacity-type": "spot"}, "spot"),
    ({"karpenter.sh/capacity-type": "on-demand"}, "on-demand"),
    ({"eks.amazonaws.com/capacityType": "ON_DEMAND"}, "on-demand"),
    ({"eks.amazonaws.com/capacityType": "SPOT"}, "spot"),
    ({}, None),
    ({"karpenter.sh/capacity-type": ""}, None),
])
def test_요금_종류를_한_가지_말로_맞춘다(labels, expected):
    """온디맨드 단가를 스팟 노드에 말하지 않게 앱이 보는 값이다."""
    stdout = _list(_node("n1", labels=labels, allocatable={"nvidia.com/gpu": "1"}))
    assert gpu.gpu_nodes(stdout)[0]["capacity_type"] == expected


def test_time_slicing으로_불어난_자원_수보다_물리_GPU_수_라벨을_믿는다():
    """allocatable nvidia.com/gpu 16 은 4장을 4조각씩 나눈 것이다 — 메모리도 물리 4장 기준."""
    row = gpu.gpu_nodes(fixture("gpu_nodes.json"))[1]
    assert (row["count"], row["memory_mib"]) == (4, 4 * 46068)


def test_라벨이_없으면_자원_수와_인스턴스_종류로_메모리는_0():
    stdout = _list(_node("n1", labels={"node.kubernetes.io/instance-type": "g6e.xlarge"},
                         allocatable={"nvidia.com/gpu": "2"}))
    assert gpu.gpu_nodes(stdout)[0] == {
        "node": "n1", "product": "g6e.xlarge", "count": 2, "memory_mib": 0,
        "instance_type": "g6e.xlarge", "zone": None, "capacity_type": None,
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
                                              "served_models": None, "node_pools": None, "counter": None, "model_volumes": None}
    waiting = [{"namespace": "llm", "pod": "vllm-0", "gpus": 1, "phase": "Pending"}]
    assert gpu.build([], [], waiting) == {"nodes": [], "serving": [], "benchmarks": [], "waiting": waiting,
                                          "served_models": None, "node_pools": None, "counter": None, "model_volumes": None}
    # ServedModel 을 읽었으면 하나도 없어도 칸이 있다 — null(못 읽음)과 빈 배열(없음)은 다르다
    assert gpu.build([], [], [], []) == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [], "served_models": [],
                                         "node_pools": None, "counter": None, "model_volumes": None}
    # GPU 풀이 있으면 그것만으로 칸이 있다. 빈 배열은 "GPU 풀 없음" 이라 칸을 만들지 않는다
    pool = [{"name": "gpu", "gpu_limit": 2}]
    assert gpu.build([], [], [], None, pool) == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [],
                                                 "served_models": None, "node_pools": pool, "counter": None, "model_volumes": None}
    assert gpu.build([], [], [], None, []) is None


def test_노드_목록이_깨졌으면_ValueError():
    with pytest.raises(ValueError):
        gpu.gpu_nodes("not json")
    with pytest.raises(ValueError):
        gpu.gpu_nodes('{"items": {}}')
