"""ServedModel 파싱과 개요의 ServedModel 조회 (DURO-182)"""
from __future__ import annotations

import pytest

from gpu_support import (
    FORBIDDEN,
    L40S_1,
    L40S_4,
    NO_CRD,
    VLLM_IMAGE,
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


A_TEAM_IMAGE = 'vllm/vllm-openai:v0.32.0@sha256:0000000000000000000000000000000000000000000000000000000000000000'
JUST_CREATED_IMAGE = 'vllm/vllm-openai:v0.32.0@sha256:0000000000000000000000000000000000000000000000000000000000000000'


def test_ServedModel을_namespace_이름순으로_응답_줄로_바꾼다():
    assert gpu.served_models(fixture("served_models.json")) == [
        {"namespace": "a-team", "name": "gpt-oss-20b", "model": "openai/gpt-oss-20b", "phase": "Failed",
         "reason": "Unschedulable", "message": "0/2 nodes are available: 2 Insufficient nvidia.com/gpu.",
         "hint": "GPU 노드가 모자라요. 노드를 늘리거나 다른 모델을 멈춰 주세요.", "download_percent": 100, "download_bytes": None, "download_total_bytes": None,
         "max_concurrency": None, "engine_image": A_TEAM_IMAGE, "gpus": 1, "started_at": None, "stops_at": None,
         "measured_at": None},
        {"namespace": "llm", "name": "gpt-oss-20b", "model": "openai/gpt-oss-20b", "phase": "Ready",
         "reason": "", "message": "", "hint": "", "download_percent": 100, "download_bytes": 44452911104,
         "download_total_bytes": 44452911104, "max_concurrency": 12, "engine_image": VLLM_IMAGE, "gpus": 1,
         "started_at": "2026-11-04T01:00:00Z", "stops_at": "2026-11-04T03:00:00Z", "measured_at": "2026-11-04T01:30:00Z"},
        # 오퍼레이터가 아직 status 를 쓰지 않았다 — 문자열은 "", 숫자는 null
        {"namespace": "llm", "name": "just-created", "model": "org/just-created", "phase": "",
         "reason": "", "message": "", "hint": "", "download_percent": None, "download_bytes": None, "download_total_bytes": None,
         "max_concurrency": None, "engine_image": JUST_CREATED_IMAGE, "gpus": None,
         "started_at": None, "stops_at": None, "measured_at": None},
        # 내려받기 0% 는 값이다 — null 이 아니다. 전체 크기를 모르면 받은 용량만 있다 (약속: percent 0, totalBytes 없음)
        {"namespace": "llm", "name": "qwen3", "model": "Qwen/Qwen3.6-35B-A3B", "phase": "Downloading",
         "reason": "Downloading", "message": "download Job is running", "hint": "", "download_percent": 0,
         "download_bytes": 1073741824, "download_total_bytes": None, "max_concurrency": None, "engine_image": VLLM_IMAGE, "gpus": 1,
         "started_at": None, "stops_at": None, "measured_at": None},
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


@pytest.mark.parametrize("spec", [{}, {"engine": {}}, {"engine": {"image": ""}}, {"engine": {"image": 3}}, {"engine": None}])
def test_엔진_이미지가_없으면_null이다(spec):
    [row] = gpu.served_models(_list({"metadata": {"name": "m"}, "spec": spec}))
    assert row["engine_image"] is None


@pytest.mark.parametrize(("spec", "expected"), [
    ({"engine": {"gpus": 4}}, 4), ({"engine": {"gpus": 1}}, 1),
    ({"engine": {"gpus": 3}}, 3),                                            # CRD 는 받지만 서버 시작 요청은 안 받는 값 — 여기서는 거르지 않는다
    ({}, None), ({"engine": None}, None), ({"engine": {}}, None),
    ({"engine": {"gpus": None}}, None), ({"engine": {"gpus": "4"}}, None),
    ({"engine": {"gpus": True}}, None), ({"engine": {"gpus": 1.5}}, None),
    ({"engine": {"gpus": 0}}, None), ({"engine": {"gpus": -1}}, None),       # 앱은 null 만 1장으로 본다 — 0장으로 다시 시작하지 않게
])
def test_ServedModel의_GPU_수는_spec_engine_gpus의_1_이상_정수만_싣고_나머지는_null이다(spec, expected):
    """[다시 시작] 이 같은 GPU 수로 시작하는 값이다. null 이면 앱이 1장으로 본다 (DURO-186)."""
    [row] = gpu.served_models(_list({"metadata": {"name": "m"}, "spec": spec}))
    assert row["gpus"] == expected


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


@pytest.mark.parametrize("baseline", [
    None, {}, {"maxConcurrency": 4}, {"measuredAt": ""}, {"measuredAt": 1730682000},
    {"measuredAt": "어제"}, {"measuredAt": "2026-11-05T01:00:00"}, {"measuredAt": "2026-13-05T01:00:00Z"},
])
def test_측정한_적_없거나_형식이_다른_measured_at은_null이다(baseline):
    [row] = gpu.served_models(_list({"metadata": {"name": "m"}, "status": {"baseline": baseline}}))
    assert row["measured_at"] is None


def test_RFC3339인_measured_at은_적힌_그대로_싣는다():
    baseline = {"measuredAt": "2026-11-05T10:00:00.5+09:00"}
    [row] = gpu.served_models(_list({"metadata": {"name": "m"}, "status": {"baseline": baseline}}))
    assert row["measured_at"] == "2026-11-05T10:00:00.5+09:00"


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
    assert body == {"nodes": [], "serving": [], "benchmarks": [], "waiting": [], "served_models": [], "node_pools": [],
                    "counter": None}


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
