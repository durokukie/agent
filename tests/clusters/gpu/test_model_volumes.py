"""모델 볼륨 — 오븐이 만든 모델 PVC 를 개요의 gpu 칸에 싣는다 (앱의 "받아 둠" 배지)"""
from __future__ import annotations

import pytest

from gpu_support import (
    FORBIDDEN,
    NO_CRD,
    _list,
    _result,
    _served_models_cluster,
    fixture,
)
from kukie_agent import clusters_api
from kukie_agent.clusters import gpu
from kukie_agent.kubectl import KubectlResult


def _pvc(name: str = "gpt-oss-120b", *, capacity: str | None = "100Gi", request: str | None = "100Gi",
         phase: str | None = "Bound", owned: bool = False, labels: object = None) -> dict:
    """오븐이 만드는 모델 PVC. retainOnDelete 면 주인이 없다."""
    meta: dict = {"name": f"{name}-models", "namespace": "llm", "creationTimestamp": "2026-11-04T10:00:00Z",
                  "labels": labels if labels is not None else {
                      "app.kubernetes.io/managed-by": "kukie-oven", "serving.kukie.io/servedmodel": name}}
    if owned:
        meta["ownerReferences"] = [{"apiVersion": "serving.kukie.io/v1alpha1", "kind": "ServedModel", "name": name}]
    item: dict = {"metadata": meta, "spec": {"resources": {"requests": {"storage": request}} if request else {}},
                  "status": {}}
    if capacity:
        item["status"]["capacity"] = {"storage": capacity}
    if phase:
        item["status"]["phase"] = phase
    return item


def test_모델_PVC를_정해진_칸만_응답_줄로_바꾼다():
    assert gpu.model_volumes(_list(_pvc())) == [{
        "name": "gpt-oss-120b", "pvc": "gpt-oss-120b-models", "size_bytes": 100 * 1024**3, "phase": "Bound",
        "retained": True, "created_at": "2026-11-04T10:00:00Z",
    }]


def test_모델_볼륨의_명령은_llm의_라벨_달린_PVC만_읽는다():
    assert gpu.MODEL_VOLUMES_COMMAND == ["get", "persistentvolumeclaims", "-n", "llm", "-l", "serving.kukie.io/servedmodel",
                                         "-o", "json"]


def test_돌고_있는_ServedModel의_PVC는_retained가_아니다():
    [row] = gpu.model_volumes(_list(_pvc(owned=True)))
    assert row["retained"] is False


def test_모델_볼륨은_ServedModel_이름순이고_상한까지만_싣는다():
    rows = gpu.model_volumes(_list(_pvc("b"), _pvc("a")))
    assert [row["name"] for row in rows] == ["a", "b"]
    many = gpu.model_volumes(_list(*(_pvc(f"m-{i:03}") for i in range(gpu.MAX_MODEL_VOLUMES + 3))))
    assert len(many) == gpu.MAX_MODEL_VOLUMES and many[0]["name"] == "m-000"


@pytest.mark.parametrize(("capacity", "requested", "expected"), [
    ("100Gi", "40Gi", 100 * 1024**3),                       # 늘린 PVC 는 capacity 가 실제 크기다
    (None, "40Gi", 40 * 1024**3),                           # 아직 Pending 이면 capacity 가 없다
    ("40G", None, 40 * 1000**3),
    ("1.5Ti", None, int(1.5 * 1024**4)),
    ("1073741824", None, 1024**3),
    ("1e3", None, None),
    ("Gi", None, None),
    (None, None, None),
])
def test_크기는_capacity를_먼저_보고_모르는_모양은_null이다(capacity, requested, expected):
    [row] = gpu.model_volumes(_list(_pvc(capacity=capacity, request=requested)))
    assert row["size_bytes"] == expected


def test_phase가_없으면_빈_문자열이다():
    [row] = gpu.model_volumes(_list(_pvc(phase=None)))
    assert row["phase"] == ""


@pytest.mark.parametrize("broken", [
    {"metadata": None},
    {"metadata": {"name": "x-models"}},
    {"metadata": {"name": "x-models", "labels": {"serving.kukie.io/servedmodel": ""}}},
    {"metadata": {"name": "x-models", "labels": "x"}},
    {"metadata": {"labels": {"serving.kukie.io/servedmodel": "x"}}},
    {**_pvc(), "status": "x", "spec": None},
])
def test_모양이_다른_항목은_그_줄만_건너뛴다(broken):
    rows = gpu.model_volumes(_list(broken, _pvc("ok")))
    if broken.get("status") == "x":                          # 이름·라벨은 있다 — 크기·상태만 모른다
        assert [(row["name"], row["size_bytes"], row["phase"]) for row in rows] == [("gpt-oss-120b", None, ""), ("ok", 100 * 1024**3, "Bound")]
    else:
        assert [row["name"] for row in rows] == ["ok"]


def _volumes_cluster(volumes: KubectlResult, *, served: KubectlResult = _result('{"items": []}'),
                     nodes: str = "gpu_nodes.json", pods: str = "{}"):
    inner, seen = _served_models_cluster(served, nodes=nodes, pods=pods)

    def run(args, timeout):
        if args == gpu.MODEL_VOLUMES_COMMAND:
            seen.append((args, timeout))
            return volumes
        return inner(args, timeout)

    return run, seen


def test_꺼진_GPU_클러스터에도_남은_모델_볼륨이_보인다():
    """우리 kukie-gpu 처럼 노드 0대·ServedModel 0개여도 retainOnDelete 로 남은 PVC 를 싣는다."""
    run, _ = _volumes_cluster(_result(_list(_pvc(), _pvc("gpt-oss-20b", capacity="40Gi"))), nodes="nodes.json")
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert has_gpu is True and body["nodes"] == [] and body["served_models"] == []
    assert [(row["name"], row["retained"]) for row in body["model_volumes"]] == [("gpt-oss-120b", True), ("gpt-oss-20b", True)]


def test_GPU_노드가_있는_라운드에도_모델_볼륨을_싣는다():
    run, _ = _volumes_cluster(_result(_list(_pvc(owned=True))), served=_result(fixture("served_models.json")),
                              pods=fixture("gpu_pods.json"))
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert [row["retained"] for row in body["model_volumes"]] == [False]
    assert len(body["served_models"]) == 4


@pytest.mark.parametrize("volumes", [
    _result(stderr=FORBIDDEN, success=False),
    _result(stderr="context deadline exceeded", success=False),
    _result("not json"),
    _result('{"items": {}}'),
    _result("[" * 200000),
])
def test_모델_볼륨을_못_읽으면_그_칸만_null이고_나머지는_그대로다(volumes):
    run, _ = _volumes_cluster(volumes, served=_result(fixture("served_models.json")), pods=fixture("gpu_pods.json"))
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert has_gpu is True and body["model_volumes"] is None and len(body["served_models"]) == 4


@pytest.mark.parametrize("served", [
    _result(stderr=NO_CRD, success=False),
    _result(stderr=FORBIDDEN, success=False),
    _result("not json"),
])
def test_ServedModel을_못_읽은_라운드는_모델_볼륨을_조회하지_않는다(served):
    run, seen = _volumes_cluster(_result(_list(_pvc())), served=served)
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert body["model_volumes"] is None and gpu.MODEL_VOLUMES_COMMAND not in [args for args, _ in seen]


@pytest.mark.parametrize("known", [None, False])
def test_GPU_없는_일반_클러스터는_모델_볼륨을_조회하지_않는다(known):
    run, seen = _volumes_cluster(_result(_list(_pvc())), nodes="nodes.json")
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=None, known=known, clock=lambda: 0.0)
    assert body is None and gpu.MODEL_VOLUMES_COMMAND not in [args for args, _ in seen]


def test_예산이_떨어지면_모델_볼륨은_null이다():
    run, seen = _volumes_cluster(_result(_list(_pvc())))
    assert clusters_api._read_model_volumes(run, lambda limit: 0) is None
    assert seen == []
