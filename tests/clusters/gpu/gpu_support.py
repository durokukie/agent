"""tests/clusters/gpu 의 테스트 파일이 함께 쓰는 가짜 값과 만드는 함수"""
from __future__ import annotations

import json
from pathlib import Path

from kukie_agent.clusters import gpu
from kukie_agent.kubectl import KubectlResult


FIXTURES = Path(__file__).parents[2] / "fixtures" / "overview"
L40S_1, L40S_4, CPU = "ip-10-60-43-165", "ip-10-60-50-12", "ip-10-60-1-7"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _list(*items: dict) -> str:
    return json.dumps({"items": list(items)})


VLLM_IMAGE = 'vllm/vllm-openai:v0.32.0@sha256:0000000000000000000000000000000000000000000000000000000000000000'


def pods(name: str = "gpu_pods.json") -> list[dict]:
    return gpu.running_pods(fixture(name))


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
    if args == gpu.BENCHMARKS_COMMAND:
        return _result('{"items": []}')
    return _result(fixture({"deployments,statefulsets,daemonsets": "workloads.json", "pods": "pods.json",
                            "events": "events.json", "daemonsets": "daemonsets.json"}[args[1]]))


def _vllm_pending(namespace: str = "llm", name: str = "vllm-gpt-oss-20b-b897b5d97-kkjsq") -> dict:
    return {"metadata": {"name": name, "namespace": namespace}, "status": {"phase": "Pending"},
            "spec": {"containers": [{"name": "vllm", "image": "vllm/vllm-openai:v0.32.0",
                                     "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}


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


def _namespaces(*names: str) -> str:
    return _list(*({"metadata": {"name": name}} for name in names))


def _gpu_calls(fake_server) -> list[dict]:
    return [body for name, body in fake_server.calls if name == "update_cluster_gpu"]
