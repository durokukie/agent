"""Karpenter GPU 풀 파싱과 개요의 NodePool 조회 (DURO-184)"""
from __future__ import annotations

import pytest

from gpu_support import (
    FORBIDDEN,
    NO_CRD,
    NO_POOL_CRD,
    _gpu_calls,
    _gpu_cluster,
    _list,
    _namespaces,
    _result,
    _served_models_cluster,
    _vllm_pending,
    fixture,
)
from helpers import USER
from kukie_agent import clusters_api
from kukie_agent.clusters import gpu


def _pool(name: str = "p", *, limits: dict | None = None, taints: list | None = None,
          requirements: list | None = None) -> dict:
    return {"metadata": {"name": name},
            "spec": {"limits": limits or {}, "template": {"spec": {"taints": taints or [], "requirements": requirements or []}}}}


FAMILY, INSTANCE_TYPE = "karpenter.k8s.aws/instance-family", "node.kubernetes.io/instance-type"


def _picks(key: str, *values: str, operator: str = "In") -> list[dict]:
    return [{"key": key, "operator": operator, "values": list(values)}]


def test_NodePool_목록에서_GPU_풀만_이름순으로_싣는다():
    assert gpu.node_pools(fixture("node_pools.json")) == ([
        {"name": "batch-gpu", "gpu_limit": None, "instance_types": None},      # 계열로만 알아본 풀 — limits 가 없고 인스턴스 제한도 모른다
        {"name": "gpu", "gpu_limit": 2, "instance_types": ["g6e.xlarge"]},
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
    assert gpu.node_pools(_list(_pool(limits={"nvidia.com/gpu": limit}))) == ([{"name": "p", "gpu_limit": expected, "instance_types": None}], False)


@pytest.mark.parametrize("limits", [{"nvidia.com/gpu": 0}, {"nvidia.com/gpu": "0"}, {"cpu": 0}, {"cpu": "0"},
                                    {"memory": "0Gi"}, {"cpu": "0.0", "nvidia.com/gpu": 2}])
def test_limits의_어느_자원이든_0이면_꺼_둔_풀이라_응답에서_뺀다(limits):
    """Karpenter 는 limits 를 넘는 노드를 띄우지 않는다 — 풀을 멈춰 두는 흔한 방법이다 (agent#139 리뷰)."""
    off = _pool("off", limits=limits, requirements=_picks(FAMILY, "g6e"))
    assert gpu.node_pools(_list(off)) == ([], True)
    assert gpu.node_pools(_list(off, _pool("on", limits={"nvidia.com/gpu": 2}))) == ([{"name": "on", "gpu_limit": 2, "instance_types": None}], True)


def test_CPU_풀을_꺼_둔_것은_GPU_풀과_상관없다():
    assert gpu.node_pools(_list(_pool("cpu", limits={"cpu": 0}))) == ([], False)
    assert gpu.node_pools(_list(_pool("p", limits={"cpu": "10", "memory": "100Gi", "nvidia.com/gpu": 1}))) == (
        [{"name": "p", "gpu_limit": 1, "instance_types": None}], False)


def test_instance_type_In_값을_적힌_순서대로_싣는다():
    """kukie-gpu 의 NodePool gpu 와 같은 모양이다 — 다른 키의 조건이 같이 있어도 된다."""
    requirements = [*_picks("karpenter.sh/capacity-type", "on-demand"), *_picks(INSTANCE_TYPE, "g6e.xlarge", "g6e.12xlarge")]
    pool = _pool(limits={"nvidia.com/gpu": "5"}, taints=[{"key": "nvidia.com/gpu"}], requirements=requirements)
    assert gpu.node_pools(_list(pool)) == (
        [{"name": "p", "gpu_limit": 5, "instance_types": ["g6e.xlarge", "g6e.12xlarge"]}], False)


def test_instance_types는_적힌_순서를_지키며_중복을_뺀다():
    requirements = _picks(INSTANCE_TYPE, "g6e.12xlarge", "g6e.xlarge", "g6e.12xlarge", "g6e.xlarge")
    [row], _ = gpu.node_pools(_list(_pool(limits={"nvidia.com/gpu": 2}, requirements=requirements)))
    assert row["instance_types"] == ["g6e.12xlarge", "g6e.xlarge"]


TWO_TYPES = _picks(INSTANCE_TYPE, "g6e.xlarge", "g6e.12xlarge")


@pytest.mark.parametrize("other", [
    _picks("karpenter.sh/capacity-type", "on-demand"),
    _picks("karpenter.sh/capacity-type", "spot", operator="NotIn"),
    _picks("topology.kubernetes.io/zone", "ap-northeast-2a", "ap-northeast-2c"),
    _picks("topology.k8s.aws/zone-id", "apne2-az1"),
    _picks("kubernetes.io/arch", "amd64"),
    _picks("kubernetes.io/os", "linux"),
    [{"key": "topology.kubernetes.io/zone", "operator": "Exists"}],
])
def test_GPU_장수를_안_바꾸는_조건은_같이_있어도_instance_types를_싣는다(other):
    [row], _ = gpu.node_pools(_list(_pool(limits={"nvidia.com/gpu": 2}, requirements=[*other, *TWO_TYPES])))
    assert row["instance_types"] == ["g6e.xlarge", "g6e.12xlarge"]


@pytest.mark.parametrize("other", [
    _picks("karpenter.k8s.aws/instance-gpu-count", "1"),                     # 12xlarge(4장)를 뺀다 — 앱이 4장 칸을 보여 주면 Unschedulable 에 머문다
    _picks("karpenter.k8s.aws/instance-gpu-count", "2", operator="Lt"),
    _picks("karpenter.k8s.aws/instance-size", "12xlarge", operator="NotIn"),
    _picks(FAMILY, "g6"),
    _picks(FAMILY, "g6", operator="NotIn"),
    _picks("karpenter.k8s.aws/instance-category", "g"),
    _picks("karpenter.k8s.aws/instance-memory", "100000", operator="Lt"),
    _picks(INSTANCE_TYPE, "g6e.12xlarge", operator="NotIn"),
    _picks("karpenter.k8s.aws/capacity-reservation-id", "cr-1"),             # instance- 로 시작하지 않아도 좁힌다
    _picks("example.com/team", "ml"),                                        # 좁히는지 모르는 키는 제한을 모르는 것이다
    [{"operator": "In", "values": ["x"]}],                                   # 키가 없거나 문자열이 아니다
    [{"key": None, "operator": "In", "values": ["x"]}],
    [{"key": ["x"], "operator": "In", "values": ["x"]}],
    [{"key": {"x": 1}, "operator": "In", "values": ["x"]}],
])
def test_고를_범위를_좁힐_수_있는_조건이_같이_있으면_instance_types는_null이고_풀은_그대로다(other):
    """Karpenter 는 모든 조건의 교집합만 띄운다. 키가 앞에 있어도 뒤에 있어도 같다."""
    for requirements in ([*TWO_TYPES, *other], [*other, *TWO_TYPES]):
        pool = _pool(limits={"nvidia.com/gpu": 2}, taints=[{"key": "nvidia.com/gpu"}], requirements=requirements)
        assert gpu.node_pools(_list(pool)) == ([{"name": "p", "gpu_limit": 2, "instance_types": None}], False)


TEMPLATE_TAINT = [{"key": "nvidia.com/gpu", "effect": "NoSchedule"}]


@pytest.mark.parametrize("requirements", [
    [],
    _picks(FAMILY, "g6e"),                                                   # 다른 조건만 있다
    _picks("karpenter.k8s.aws/instance-gpu-count", "0", operator="Gt"),
    _picks(INSTANCE_TYPE, "m5.large", operator="NotIn"),
    [{"key": INSTANCE_TYPE, "operator": "Exists"}],
    _picks(INSTANCE_TYPE, "g6e.xlarge") + _picks(INSTANCE_TYPE, "g6e.xlarge", "g6e.12xlarge"),    # 키가 여러 번
    _picks(INSTANCE_TYPE, "g6e.xlarge") + _picks(INSTANCE_TYPE, "m5.large", operator="NotIn"),
    _picks(INSTANCE_TYPE),                                                   # 값이 비었다
    [{"key": INSTANCE_TYPE, "operator": "In"}],
    [{"key": INSTANCE_TYPE, "operator": "In", "values": "g6e.xlarge"}],
    [{"key": INSTANCE_TYPE, "operator": "In", "values": ["g6e.xlarge", 3]}],
    [{"key": INSTANCE_TYPE, "operator": "In", "values": ["g6e.xlarge", ""]}],
    [{"key": INSTANCE_TYPE, "operator": "In", "values": ["g6e.xlarge", None]}],
])
def test_instance_types는_In_하나로_고른_것이_아니면_null이고_풀은_그대로_있다(requirements):
    pool = _pool(limits={"nvidia.com/gpu": 2}, taints=TEMPLATE_TAINT, requirements=requirements)
    assert gpu.node_pools(_list(pool)) == ([{"name": "p", "gpu_limit": 2, "instance_types": None}], False)


@pytest.mark.parametrize("template", [
    None, "x", 3, [], {}, {"spec": None}, {"spec": "x"}, {"spec": []}, {"spec": {"requirements": None}},
    {"spec": {"requirements": "x"}}, {"spec": {"requirements": 3}}, {"spec": {"requirements": {}}},
    {"spec": {"requirements": ["x"]}}, {"spec": {"requirements": [None, *_picks(INSTANCE_TYPE, "g6e.xlarge")]}},
])
def test_GPU_상한이_있는_풀은_템플릿_모양이_이상해도_ValueError_없이_instance_types만_null이다(template):
    """이 풀은 GPU 풀 판별이 템플릿을 읽지 않아 지금까지 읽혔다. 새 칸 때문에 node_pools 째 null 이 되면 hasGpu 판단이 바뀐다."""
    pool = {"metadata": {"name": "p"}, "spec": {"limits": {"nvidia.com/gpu": 2}, "template": template}}
    assert gpu.node_pools(_list(pool)) == ([{"name": "p", "gpu_limit": 2, "instance_types": None}], False)


@pytest.mark.parametrize("template", [
    {"taints": TEMPLATE_TAINT, "requirements": "x"},                         # taint 로 판별이 끝나 requirements 를 읽지 않는다
    {"taints": TEMPLATE_TAINT, "requirements": [None]},
    {"requirements": [*_picks(FAMILY, "g6e"), None]},                        # 앞쪽 조건으로 판별이 끝나 뒤를 읽지 않는다
])
def test_GPU_판별이_읽지_않은_requirements_모양이_이상해도_instance_types만_null이다(template):
    pool = {"metadata": {"name": "p"}, "spec": {"template": {"spec": template}}}
    assert gpu.node_pools(_list(pool)) == ([{"name": "p", "gpu_limit": None, "instance_types": None}], False)


@pytest.mark.parametrize("requirements", [
    [{"key": INSTANCE_TYPE, "operator": "In", "values": ["g6e.xlarge", 3]}],    # 값이 이상해도 GPU 계열을 고른 풀이다
    _picks(INSTANCE_TYPE, "g6e.xlarge") + _picks(INSTANCE_TYPE, "g6e.12xlarge"),
    _picks(FAMILY, "g6e"),
])
def test_instance_types가_null이어도_GPU_풀_판별은_그대로다(requirements):
    assert gpu.node_pools(_list(_pool(requirements=requirements))) == (
        [{"name": "p", "gpu_limit": None, "instance_types": None}], False)


@pytest.mark.parametrize("pool", [
    _pool(requirements=_picks(INSTANCE_TYPE, "m5.large", "c7i.xlarge")),     # GPU 풀이 아니면 instance-type 이 있어도 안 싣는다
    _pool(requirements=_picks(INSTANCE_TYPE, "g6e.xlarge", operator="NotIn")),
])
def test_instance_type을_읽어도_GPU_풀이_아닌_것은_빈_목록_그대로다(pool):
    assert gpu.node_pools(_list(pool)) == ([], False)


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


GPU_POOLS = _result(_list(_pool("gpu", limits={"nvidia.com/gpu": 2})))
GPU_POOL_ROW = {"name": "gpu", "gpu_limit": 2, "instance_types": None}


@pytest.mark.parametrize("known", [None, False, True])
def test_노드도_대기_파드도_모델도_없어도_GPU_풀이_있으면_GPU_클러스터다(known):
    """모델을 시작하기 전의 Karpenter 클러스터 — 이 길이 없으면 GPU 화면으로 들어가지 못해 모델을 시작할 수 없다."""
    run, seen = _served_models_cluster(_result(stderr=NO_CRD, success=False), nodes="nodes.json", pools=GPU_POOLS)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"pool-only:{known}", known=known,
                                              clock=lambda: 0.0)
    assert has_gpu is True
    assert body == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [], "served_models": None,
                    "node_pools": [GPU_POOL_ROW], "counter": None}
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
    assert r.json()["gpu"]["node_pools"] == [
        {"name": "batch-gpu", "gpu_limit": None, "instance_types": None},
        {"name": "gpu", "gpu_limit": 2, "instance_types": ["g6e.xlarge"]}]
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
    assert has_gpu is True and body["node_pools"] == [GPU_POOL_ROW]


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


@pytest.mark.parametrize("requirements", [
    _picks(FAMILY, "g6e"),
    [{"key": INSTANCE_TYPE, "operator": "In", "values": ["g6e.xlarge", 3]}],
    _picks(INSTANCE_TYPE, "g6e.xlarge") + _picks(INSTANCE_TYPE, "g6e.12xlarge"),
    "x",
])
def test_instance_types가_null이어도_꺼진_클러스터의_hasGpu는_true다(requirements):
    """빈 node_pools 는 hasGpu 를 false 로 덮는 근거다 — 새 칸이 풀을 빈 목록이나 null 로 만들면 안 된다 (DURO-186)."""
    pools = _result(_list(_pool("gpu", limits={"nvidia.com/gpu": 2}, requirements=requirements)))
    run, _ = _served_models_cluster(_result(stderr=NO_CRD, success=False), nodes="nodes.json", pools=pools)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=f"odd-pool:{requirements}", known=None,
                                              clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] == [GPU_POOL_ROW]


def test_GPU_노드가_있는_클러스터도_node_pools를_싣고_못_읽으면_그_칸만_null이다():
    run, _ = _served_models_cluster(_result('{"items": []}'), pods=fixture("gpu_pods.json"), pools=GPU_POOLS)
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert has_gpu is True and body["node_pools"] == [GPU_POOL_ROW] and len(body["nodes"]) == 2

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
    assert has_gpu is True and body["node_pools"] == [GPU_POOL_ROW]


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
    assert has_gpu is True and body["node_pools"] == [GPU_POOL_ROW]
