"""측정 기록 파싱과 개요의 측정 기록 조회·기억 (DURO-188)"""
from __future__ import annotations

import json

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
    fixture,
    pods,
)
from helpers import USER
from kukie_agent import clusters_api
from kukie_agent.clusters import gpu
from kukie_agent.kubectl import KubectlResult


MEASURED = {"ok": True, "reason": "Measured", "partial": False, "message": "동시 16까지 SLA 안, 그때 출력 787 토큰/초",
            "sla": {"ttftMs": 1000.0, "itlMs": 50.0, "inputTokens": 1000, "outputTokens": 200},
            "steps": [{"c": 1, "done": 40, "total": 40, "ttft95": 167, "itl95": 5.0, "tps": 173, "waiting": 0, "kv": 0, "sla": True},
                      {"c": 16, "done": 128, "total": 128, "ttft95": 402, "itl95": 18.3, "tps": 787, "waiting": 0, "kv": 12, "sla": True}]}


def _measure_cm(name: str = "kukie-gpu-measure-m-20261105010000", *, baseline: object = MEASURED, **data) -> dict:
    """오븐이 측정마다 남기는 ConfigMap. data 칸은 모두 문자열이다."""
    fields = {"baseline.json": baseline if isinstance(baseline, str) else json.dumps(baseline), "model": "openai/gpt-oss-20b",
              "gpus": "2", "engineImage": VLLM_IMAGE, "engineArgs": '["--max-model-len","8192"]',
              "measuredAt": "2026-11-05T01:00:00Z", "partial": "false", "maxConcurrency": "16", **data}
    return {"metadata": {"name": name, "namespace": "llm", "labels": {"serving.kukie.io/measure-result": "true"}},
            "data": {key: value for key, value in fields.items() if value is not None}}


def test_측정_기록_ConfigMap을_정해진_칸만_응답_줄로_바꾼다():
    assert gpu.benchmarks(_list(_measure_cm(secret="값"))) == [{
        "id": "kukie-gpu-measure-m-20261105010000", "model": "openai/gpt-oss-20b", "at": "2026-11-05T01:00:00Z",
        "input_tokens": 1000, "output_tokens": 200, "sla": {"ttft_p95_ms": 1000.0, "itl_p95_ms": 50.0},
        "steps": [{"concurrency": 1, "ttft_p95_ms": 167, "itl_p95_ms": 5.0, "output_tokens_per_s": 173},
                  {"concurrency": 16, "ttft_p95_ms": 402, "itl_p95_ms": 18.3, "output_tokens_per_s": 787}],
        "gpus": 2, "partial": False, "max_concurrency": 16,
    }]


def test_측정_기록의_명령은_약속대로_llm의_라벨만_읽는다():
    assert gpu.BENCHMARKS_COMMAND == ["get", "configmaps", "-n", "llm", "-l", "serving.kukie.io/measure-result=true", "-o", "json"]


def test_측정_기록은_최근_순이고_시간대가_달라도_시각으로_견준다():
    rows = gpu.benchmarks(_list(_measure_cm("old", measuredAt="2026-11-05T00:00:00Z"),
                                _measure_cm("new", measuredAt="2026-11-05T02:00:00Z"),
                                _measure_cm("offset", measuredAt="2026-11-05T10:00:00+09:00"),     # 01:00Z. 글자로 견주면 맨 앞이다
                                _measure_cm("fraction", measuredAt="2026-11-05T00:30:00.5Z")))
    assert [row["id"] for row in rows] == ["new", "offset", "fraction", "old"]
    assert rows[1]["at"] == "2026-11-05T10:00:00+09:00"                                            # 적힌 그대로 싣는다


def test_측정_기록은_나노초_차이도_순서를_지키고_같은_시각은_id로_놓는다():
    """datetime 은 마이크로초 아래를 버린다 — 그 아래 자리만 다른 기록이 id 순으로 놓이면 50개 상한에서 새 기록이 잘린다 (agent#144 리뷰)."""
    rows = gpu.benchmarks(_list(_measure_cm("z-older", measuredAt="2026-11-05T01:00:00.000000001Z"),
                                _measure_cm("a-newer", measuredAt="2026-11-05T01:00:00.000000009Z"),
                                _measure_cm("b-same", measuredAt="2026-11-05T01:00:00.000000005Z"),
                                _measure_cm("c-same", measuredAt="2026-11-05T01:00:00.000000005Z"),
                                _measure_cm("no-fraction", measuredAt="2026-11-05T01:00:00Z")))
    assert [row["id"] for row in rows] == ["a-newer", "c-same", "b-same", "z-older", "no-fraction"]


def test_측정_기록은_최근_50개까지만_싣는다():
    items = [_measure_cm(f"run-{i:03}", measuredAt=f"2026-11-05T{i // 60:02}:{i % 60:02}:00Z") for i in range(gpu.MAX_BENCHMARKS + 5)]
    rows = gpu.benchmarks(_list(*items))
    assert gpu.MAX_BENCHMARKS == 50 and len(rows) == 50
    assert (rows[0]["id"], rows[-1]["id"]) == ("run-054", "run-005")


@pytest.mark.parametrize(("data", "expected"), [
    ({"partial": "true"}, True), ({"partial": "false"}, False), ({"partial": None}, False), ({"partial": "yes"}, False),
])
def test_부분_측정은_문자열_true만_참이다(data, expected):
    assert gpu.benchmarks(_list(_measure_cm(**data)))[0]["partial"] is expected


@pytest.mark.parametrize(("value", "expected"), [
    ("4", 4), ("1", 1), (None, None), ("", None), ("0", None), ("-1", None), ("1.5", None), ("abc", None), (" 4", None),
    ("٣", None), ("9" * 5000, None), (4, None), (True, None),
])
def test_측정_기록의_GPU_수와_최대_동시는_문자열로_적힌_1_이상_정수만_싣는다(value, expected):
    [row] = gpu.benchmarks(_list(_measure_cm(gpus=value, maxConcurrency=value)))
    assert (row["gpus"], row["max_concurrency"]) == (expected, expected)


def test_p95가_null인_단계는_그_단계만_뺀다():
    """오븐은 그 단계의 p95 를 못 얻으면 null 을 쓴다. 앞 단계는 멀쩡한 측정이고, 앱은 숫자만 그린다."""
    step = {"c": 32, "done": 0, "total": 256, "ttft95": None, "itl95": None, "tps": 0, "waiting": None, "kv": None, "sla": False}
    [row] = gpu.benchmarks(_list(_measure_cm(baseline={**MEASURED, "steps": [*MEASURED["steps"], step]})))
    assert [s["concurrency"] for s in row["steps"]] == [1, 16]


def _step(**fields) -> dict:
    return {**MEASURED["steps"][0], **fields}


@pytest.mark.parametrize("broken", [
    {"metadata": None},
    {"metadata": {"name": ""}},
    {"data": None},
    {"data": "x"},
    {"data": {**_measure_cm()["data"], "model": ""}},
    {"data": {**_measure_cm()["data"], "model": 3}},
    {"data": {**_measure_cm()["data"], "model": "m" * (gpu.MAX_BENCHMARK_MODEL + 1)}},
    {"data": {key: value for key, value in _measure_cm()["data"].items() if key != "model"}},
    {"data": {**_measure_cm()["data"], "baseline.json": "not json"}},
    {"data": {**_measure_cm()["data"], "baseline.json": "[]"}},
    {"data": {**_measure_cm()["data"], "baseline.json": "[" * 200000}},        # 재귀 한도
    {"data": {**_measure_cm()["data"], "baseline.json": {"steps": []}}},       # data 는 문자열이어야 한다
    {"data": {key: value for key, value in _measure_cm()["data"].items() if key != "baseline.json"}},
    *({"data": {**_measure_cm()["data"], "measuredAt": at}} for at in (
        None, "", "어제", 1730682000, "2026-11-05", "2026-11-05T01:00:00", "2026-13-05T01:00:00Z", "2026-11-05T01:00:00+99:00")),
])
def test_모양이_다른_측정_기록은_그_줄만_건너뛴다(broken):
    good = _measure_cm("good")
    rows = gpu.benchmarks(_list({**_measure_cm("bad"), **broken}, good, "not an object"))
    assert [row["id"] for row in rows] == ["good"]


@pytest.mark.parametrize("baseline", [
    {"steps": MEASURED["steps"]},                                       # sla 가 없다
    {**MEASURED, "sla": "x"},
    {**MEASURED, "sla": {**MEASURED["sla"], "ttftMs": None}},
    {**MEASURED, "sla": {**MEASURED["sla"], "itlMs": "50"}},
    {**MEASURED, "sla": {**MEASURED["sla"], "inputTokens": 0}},
    {**MEASURED, "sla": {**MEASURED["sla"], "outputTokens": 200.5}},
    {"sla": MEASURED["sla"]},                                           # steps 가 없다 (오븐이 4096바이트를 넘기면 뺀다)
    {**MEASURED, "steps": []},
    {**MEASURED, "steps": "x"},
    {**MEASURED, "steps": ["x"]},
    {**MEASURED, "steps": [{"c": 32, "ttft95": None, "itl95": None}]},  # 남는 단계가 없다
    {**MEASURED, "steps": [_step(), {"c": 16, "tps": 787}]},            # 키가 없는 단계는 null 과 다르다 — 잘린 단계를 조용히 빼지 않는다
    {**MEASURED, "steps": [_step(), {"c": 16, "ttft95": 402, "tps": 787}]},
    {**MEASURED, "steps": [_step(), {"c": 16, "itl95": 18.3, "tps": 787}]},
    {**MEASURED, "steps": [_step(c=None)]},
    {**MEASURED, "steps": [_step(c=0)]},
    {**MEASURED, "steps": [_step(c=True)]},
    {**MEASURED, "steps": [_step(ttft95="167")]},
    {**MEASURED, "steps": [_step(itl95=-1)]},
    {**MEASURED, "steps": [_step(tps=None)]},
    {**MEASURED, "steps": [_step()] * (gpu.MAX_BENCHMARK_STEPS + 1)},
    '{"sla": {"ttftMs": NaN, "itlMs": 50, "inputTokens": 1, "outputTokens": 1}, "steps": [{"c": 1, "ttft95": 1, "itl95": 1, "tps": 1}]}',
    '{"sla": {"ttftMs": 1, "itlMs": 50, "inputTokens": 1, "outputTokens": 1}, "steps": [{"c": 1, "ttft95": Infinity, "itl95": 1, "tps": 1}]}',
    '{"sla": {"ttftMs": 1, "itlMs": 50, "inputTokens": 1, "outputTokens": 1}, "steps": [{"c": 1, "ttft95": 1, "itl95": 1e999, "tps": 1}]}',
    '{"sla": {"ttftMs": 1, "itlMs": 50, "inputTokens": 1, "outputTokens": 1}, "steps": [{"c": 1, "ttft95": 1, "itl95": 1, "tps": 1' + "0" * 400 + '}]}',
])
def test_baseline_json의_모양이_다르면_그_줄만_건너뛴다(baseline):
    rows = gpu.benchmarks(_list(_measure_cm("bad", baseline=baseline), _measure_cm("good")))
    assert [row["id"] for row in rows] == ["good"]
    json.dumps(rows, allow_nan=False)                                   # 응답을 만들 때 예외가 나지 않는다


@pytest.mark.parametrize("broken", ["not json", '{"items": {}}'])
def test_측정_기록_목록이_깨졌으면_ValueError다(broken):
    """부르는 쪽(_read_benchmarks)이 빈 배열로 둔다."""
    with pytest.raises(ValueError):
        gpu.benchmarks(broken)


def test_build는_측정_기록을_싣되_그것만으로_gpu_칸을_만들지_않는다():
    row = {"id": "x"}
    assert gpu.build([{"node": "n"}], [], benchmarks=[row])["benchmarks"] == [row]
    assert gpu.build([{"node": "n"}], [])["benchmarks"] == []
    assert gpu.build([], [], benchmarks=[row]) is None


def test_개요_gpu_칸에_측정_기록을_싣는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(namespace="shop")
    seen: list[tuple[list[str], int]] = []

    def fake(args, *, context, timeout=30, kubeconfig=None, **kwargs):
        seen.append((args, timeout))
        if args == gpu.SERVED_MODELS_COMMAND:
            return _result(fixture("served_models.json"))
        if args == gpu.BENCHMARKS_COMMAND:
            return _result(_list(_measure_cm()))
        return _gpu_cluster(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    [run] = r.json()["gpu"]["benchmarks"]
    assert (run["id"], run["gpus"], run["partial"], run["max_concurrency"]) == ("kukie-gpu-measure-m-20261105010000", 2, False, 16)
    assert "engineImage" not in run and "engineArgs" not in run
    assert [args for args, _ in seen].count(gpu.BENCHMARKS_COMMAND) == 1
    assert dict((tuple(args), t) for args, t in seen)[tuple(gpu.BENCHMARKS_COMMAND)] == clusters_api.GPU_BENCHMARKS_TIMEOUT


def _benchmarks_cluster(benchmarks: KubectlResult, *, served: KubectlResult = _result('{"items": []}'),
                        nodes: str = "gpu_nodes.json", pods: str = "{}"):
    """(run, 부른 명령) — ServedModel·측정 기록 조회 결과를 정해 둔 가짜 클러스터."""
    inner, seen = _served_models_cluster(served, nodes=nodes, pods=pods)

    def run(args, timeout):
        if args == gpu.BENCHMARKS_COMMAND:
            seen.append((args, timeout))
            return benchmarks
        return inner(args, timeout)

    return run, seen


def test_모델이_돌지_않는_꺼진_GPU_클러스터에도_측정_기록이_보인다():
    """결과는 모델을 지워도 남는다 — 노드 0대·ServedModel 0개여도 기록을 싣는다."""
    run, _ = _benchmarks_cluster(_result(_list(_measure_cm())), nodes="nodes.json")
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert has_gpu is True and body["nodes"] == [] and body["served_models"] == []
    assert [row["id"] for row in body["benchmarks"]] == ["kukie-gpu-measure-m-20261105010000"]


@pytest.mark.parametrize("benchmarks", [
    _result(stderr=FORBIDDEN, success=False),
    _result(stderr="context deadline exceeded", success=False),
    _result("not json"),
    _result('{"items": {}}'),
    _result("[" * 200000),                                              # 재귀 한도
])
def test_측정_기록을_못_읽어도_그_칸만_빈_배열이고_나머지는_그대로다(benchmarks):
    run, _ = _benchmarks_cluster(benchmarks, served=_result(fixture("served_models.json")), pods=fixture("gpu_pods.json"))
    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert has_gpu is True and body["benchmarks"] == []
    assert len(body["served_models"]) == 4 and [n["node"] for n in body["nodes"]] == [L40S_1, L40S_4]


@pytest.mark.parametrize("served", [
    _result(stderr=NO_CRD, success=False),
    _result(stderr=FORBIDDEN, success=False),
    _result(stderr="context deadline exceeded", success=False),
    _result("not json"),
])
def test_ServedModel을_못_읽은_라운드는_측정_기록을_조회하지_않는다(served):
    run, seen = _benchmarks_cluster(_result(_list(_measure_cm())), served=served)
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert body["benchmarks"] == [] and gpu.BENCHMARKS_COMMAND not in [args for args, _ in seen]


@pytest.mark.parametrize("known", [None, False])
def test_GPU_없는_일반_클러스터는_측정_기록을_조회하지_않는다(known):
    run, seen = _benchmarks_cluster(_result(_list(_measure_cm())), nodes="nodes.json")
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=None, known=known, clock=lambda: 0.0)
    assert body is None
    commands = [args for args, _ in seen]
    assert gpu.SERVED_MODELS_COMMAND not in commands and gpu.BENCHMARKS_COMMAND not in commands


def test_GPU로_알려진_꺼진_클러스터는_namespace_탐색이_측정_기록_몫도_남긴다(monkeypatch):
    """모델을 지운 꺼진 클러스터에서 탐색·NodePool·ServedModel 이 저마다 몫을 다 써도 측정 기록을 읽는다 (agent#144 리뷰).

    가짜 시계는 호출마다 시간이 쌓인다고 보므로 탐색을 차례로 돌린다."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    timeouts: list[int] = []

    def run(args, timeout):
        if args == gpu.NODES_COMMAND:
            return _result(fixture("nodes.json"))
        if args == gpu.PODS_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(_namespaces(*(f"ns-{i}" for i in range(20))))
        if args == gpu.SERVED_MODELS_COMMAND:
            now[0] += timeout
            return _result('{"items": []}')
        if args == gpu.BENCHMARKS_COMMAND:
            timeouts.append(timeout)
            return _result(_list(_measure_cm()))
        if args == gpu.MODEL_VOLUMES_COMMAND:                           # 측정 기록과 함께(다른 스레드) 돈다 — 이 시계에 쌓지 않는다
            return _result('{"items": []}')
        now[0] += timeout                                               # namespace 파드·NodePool 은 몫을 다 쓰고 실패한다
        return _result(stderr="timed out", success=False)

    body, has_gpu = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: now[0])
    assert timeouts == [clusters_api.GPU_BENCHMARKS_TIMEOUT]
    assert has_gpu is True and len(body["benchmarks"]) == 1


KEY = "cluster:fingerprint:shop"


def _remembering_cluster(*, benchmarks: list, served: list | None = None):
    """(run, 시계, 부른 명령) — 조회마다 정해 둔 결과를 차례로 돌려주는 가짜 클러스터."""
    now = [0.0]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args == gpu.SERVED_MODELS_COMMAND and served:
            return served.pop(0)
        if args == gpu.SERVED_MODELS_COMMAND:
            return _result('{"items": []}')
        if args == gpu.BENCHMARKS_COMMAND:
            return benchmarks.pop(0)
        return _gpu_cluster(args)

    return run, now, seen


def _round(run, now, key=KEY):
    return clusters_api._collect_gpu(run, namespace_pods="{}", cache_key=key, clock=lambda: now[0])[0]["benchmarks"]


GOOD = _result(_list(_measure_cm()))
TIMED_OUT = _result(stderr="Command 'kubectl' timed out after 3 seconds", success=False)


@pytest.mark.parametrize("failure", [
    TIMED_OUT,
    _result(stderr="context deadline exceeded", success=False),
    _result(stderr="Unable to connect to the server: EOF", success=False),
    _result("not json"),
    _result('{"items": {}}'),
])
def test_측정_기록_조회가_실패하면_마지막으로_성공한_값을_쓴다(failure):
    """한 번 실패로 표가 비었다가 다시 차서 깜빡이지 않게 (agent#144 리뷰)."""
    run, now, _ = _remembering_cluster(benchmarks=[failure, GOOD, failure, _result('{"items": []}'), failure])
    assert _round(run, now) == []                                       # 처음부터 실패면 빈 배열이다 (null 이 아니다)
    first = _round(run, now)
    assert len(first) == 1
    assert _round(run, now) == first
    assert _round(run, now) == []                                       # 읽었는데 없으면 없는 것이다 — 다 지운 기록을 되살리지 않는다
    assert _round(run, now) == []


def test_ServedModel을_시간_초과로_못_읽은_라운드도_기억한_측정_기록을_쓴다():
    run, now, seen = _remembering_cluster(benchmarks=[GOOD],
                                          served=[_result('{"items": []}'), _result(stderr="timed out", success=False)])
    first = _round(run, now)
    assert len(first) == 1
    seen.clear()
    assert _round(run, now) == first and gpu.BENCHMARKS_COMMAND not in seen


def test_측정_기록을_읽을_몫이_없는_라운드도_기억한_값을_쓴다():
    now = [0.0]
    served_hangs = [False]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args == gpu.PODS_COMMAND:
            now[0] += clusters_api.GPU_BUDGET - 8
        if args == gpu.SERVED_MODELS_COMMAND:
            if served_hangs[0]:
                now[0] += timeout
            return _result('{"items": []}')
        if args == gpu.BENCHMARKS_COMMAND:
            return GOOD
        return _gpu_cluster(args)

    first = _round(run, now)
    assert len(first) == 1
    served_hangs[0] = True
    seen.clear()
    assert _round(run, now) == first and gpu.BENCHMARKS_COMMAND not in seen


@pytest.mark.parametrize(("benchmarks", "served"), [
    ([GOOD, _result(stderr=FORBIDDEN, success=False)], None),           # 측정 기록 조회가 거부됐다
    ([GOOD], [_result('{"items": []}'), _result(stderr=FORBIDDEN, success=False)]),   # ServedModel 조회가 거부됐다
    ([GOOD], [_result('{"items": []}'), _result(stderr=NO_CRD, success=False)]),      # 애드온을 지웠다
])
def test_거부와_CRD_없음은_잠깐의_실패가_아니라_기억을_지운다(benchmarks, served):
    """권한을 거둔 고객의 값을 하루 동안 계속 보이지 않게. 다시 시간 초과가 나도 되살아나지 않는다."""
    run, now, _ = _remembering_cluster(benchmarks=benchmarks, served=served)
    assert len(_round(run, now)) == 1
    assert _round(run, now) == []
    assert clusters_api._benchmarks_memory == {}


def test_측정_기록_기억은_연결마다_따로다():
    run, now, _ = _remembering_cluster(benchmarks=[GOOD, TIMED_OUT, TIMED_OUT])
    assert len(_round(run, now, "a")) == 1
    assert _round(run, now, "b") == []                                  # 다른 연결의 값을 쓰지 않는다
    assert len(_round(run, now, "a")) == 1


def test_연결_지문이_없으면_측정_기록을_기억하지_않는다():
    run, now, _ = _remembering_cluster(benchmarks=[GOOD, TIMED_OUT])
    assert len(_round(run, now, None)) == 1
    assert _round(run, now, None) == [] and clusters_api._benchmarks_memory == {}


def test_오래된_측정_기록_기억은_어느_연결의_것이든_지운다():
    """부르지 않는 연결의 기억이 쌓이지 않게. 하루 넘게 못 읽은 표도 계속 보이지 않게."""
    run, now, _ = _remembering_cluster(benchmarks=[GOOD, GOOD, TIMED_OUT])
    assert len(_round(run, now, "old")) == 1
    now[0] = clusters_api.GPU_BENCHMARKS_FORGET - 1
    assert len(_round(run, now, "fresh")) == 1
    assert set(clusters_api._benchmarks_memory) == {"old", "fresh"}
    now[0] = clusters_api.GPU_BENCHMARKS_FORGET
    assert _round(run, now, "old") == []                                # 기억이 지워져 시간 초과 라운드가 빈 배열이다
    assert "fresh" in clusters_api._benchmarks_memory


def test_측정_기록_시간_초과는_다음_성공까지_경고를_한_번만_남긴다(caplog):
    """표는 기억으로 유지되어 로그 말고는 알아챌 길이 없다. 20초 주기마다 남기면 로그만 쌓인다."""
    run, now, _ = _remembering_cluster(benchmarks=[GOOD, TIMED_OUT, TIMED_OUT, GOOD, TIMED_OUT, _result("not json")])
    with caplog.at_level("WARNING", logger="kukie_agent.clusters_api"):
        for _ in range(3):
            _round(run, now)
        assert caplog.text.count("시간 초과") == 1
        caplog.clear()
        _round(run, now)                                                # 성공하면 다시 경고할 수 있다
        _round(run, now)
        assert caplog.text.count("시간 초과") == 1 and "kubectl" not in caplog.text    # 원문은 남기지 않는다
        caplog.clear()
        _round(run, now)                                                # 깨진 출력은 시간 초과가 아니다
        assert caplog.text == ""


def test_처음부터_시간_초과여도_경고는_한_번이고_값은_빈_배열이다(caplog):
    run, now, _ = _remembering_cluster(benchmarks=[TIMED_OUT, TIMED_OUT])
    with caplog.at_level("WARNING", logger="kukie_agent.clusters_api"):
        assert _round(run, now) == [] and _round(run, now) == []
    assert caplog.text.count("시간 초과") == 1


def test_측정_기록이_매달려도_지표_몫을_쓰지_않는다(monkeypatch):
    """ServedModel 까지 읽고 몫이 2초 남은 라운드 — 측정 기록은 그 2초만 쓰고, 지표는 나머지 절반을 그대로 쓴다."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    timeouts: dict[str, int] = {}
    raw: list[int] = []

    def run(args, timeout):
        if args == gpu.PODS_COMMAND:
            now[0] = clusters_api.GPU_BUDGET - 8                        # 8초 남았다. 앞의 절반 4초가 NodePool·ServedModel·측정 기록 몫이다
        if args == gpu.SERVED_MODELS_COMMAND:
            now[0] += 2
            return _result(fixture("served_models.json"))
        if args == gpu.BENCHMARKS_COMMAND:
            timeouts["benchmarks"] = timeout
            now[0] += timeout
            return _result(stderr="timed out", success=False)
        if args[1] == "--raw":
            raw.append(timeout)
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert timeouts == {"benchmarks": 2} and body["benchmarks"] == []
    assert raw and raw[0] == 4 and len(body["served_models"]) == 4


def test_측정_기록을_읽을_몫이_안_남으면_조회_없이_빈_배열이다():
    now = [0.0]
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args == gpu.PODS_COMMAND:
            now[0] = clusters_api.GPU_BUDGET - 8
        if args == gpu.SERVED_MODELS_COMMAND:
            now[0] += timeout                                           # ServedModel 이 몫을 다 썼다
            return _result(fixture("served_models.json"))
        return _gpu_cluster(args)

    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: now[0])
    assert gpu.BENCHMARKS_COMMAND not in seen
    assert body["benchmarks"] == [] and len(body["served_models"]) == 4
