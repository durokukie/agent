"""카운터 429 비율 (DURO-173)"""
from __future__ import annotations

import json

from gpu_support import CPU, FORBIDDEN, NO_CRD, _gpu_cluster, _list, _result, _served_models_cluster, fixture
from kukie_agent import clusters_api
from kukie_agent.clusters import gpu


def _counter_pod(namespace: str = "kukie-gpu") -> dict:
    return {"metadata": {"name": "counter-0", "namespace": namespace, "labels": {"app.kubernetes.io/name": "kukie-counter"}},
            "spec": {"nodeName": CPU, "containers": [{"name": "counter", "image": "durokukie/kukie-counter:0.3.0"}]},
            "status": {"phase": "Running"}}


def _counter_metrics(requests: dict[tuple[str, str, str], float], start: float = 1000) -> str:
    """kukie_counter_requests_total{model,key,code} 줄과, 카운터가 0 으로 미리 만드는 rejected_total 줄."""
    models = {model for model, _, _ in requests} or {"qwen"}
    lines = [f"process_start_time_seconds {start}"]
    lines += [f'kukie_counter_requests_total{{code="{c}",key="{k}",model="{m}"}} {v}' for (m, k, c), v in requests.items()]
    lines += [f'kukie_counter_rejected_total{{model="{m}",reason="limit"}} 0' for m in models]
    return "\n".join(lines) + "\n"


def _window(text: str, previous: gpu.CounterSample | None = None, at: float = 0.0):
    sample = gpu.counter_sample(text, at=at)
    return gpu.counter_window(sample, previous), sample


def test_카운터는_지표_Service를_서비스_프록시로_읽는다():
    assert gpu.counter_proxy_command("kukie-gpu") == [
        "get", "--raw", "/api/v1/namespaces/kukie-gpu/services/kukie-counter-metrics/proxy/metrics"]


def test_모델마다_최근_구간_429_비율을_낸다():
    """requests 는 key·code 를 더한 차이라 429 도 들어 있다. rejected 는 code="429" 줄의 차이. key 는 싣지 않는다."""
    _, first = _window(_counter_metrics({("qwen", "k1", "200"): 100, ("qwen", "k2", "429"): 10, ("gpt", "k1", "200"): 5}))
    rows, _ = _window(_counter_metrics({("qwen", "k1", "200"): 130, ("qwen", "k2", "429"): 20, ("qwen", "k2", "503"): 5,
                                        ("gpt", "k1", "200"): 5}), first, at=20.0)
    assert rows == [
        {"model": "gpt", "requests": 0, "rejected": 0, "rejected_ratio": None, "window_seconds": 20.0},
        {"model": "qwen", "requests": 45, "rejected": 10, "rejected_ratio": 0.2222, "window_seconds": 20.0},
    ]


def test_카운터가_다시_떴거나_직전에_없던_모델은_null이다():
    null = {"requests": None, "rejected": None, "rejected_ratio": None, "window_seconds": None}
    _, before = _window(_counter_metrics({("a", "k", "200"): 100}))
    [row], _ = _window(_counter_metrics({("a", "k", "200"): 5}), before, at=20.0)                 # 값이 줄었다
    assert row == {"model": "a", **null}
    [row], _ = _window(_counter_metrics({("a", "k", "200"): 150}, start=2000), before, at=20.0)   # 프로세스 시작 시각이 바뀌었다
    assert row == {"model": "a", **null}
    rows, _ = _window(_counter_metrics({("a", "k", "200"): 110, ("b", "k", "200"): 3}), before, at=20.0)
    assert [(r["model"], r["requests"]) for r in rows] == [("a", 10), ("b", None)]


def _counter_cluster(monkeypatch, *, pods_ok: bool = True, with_counter: bool = True):
    """(수집 함수, 상태, 부른 명령) — GPU 클러스터에 카운터 파드 하나(CPU 노드, kukie-gpu)가 더 있다."""
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 1)
    now = [0.0]
    items = json.loads(fixture("gpu_pods.json"))["items"] + ([_counter_pod()] if with_counter else [])
    state = {"counter": _result(_counter_metrics({("qwen", "k", "200"): 100}))}
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        if args[1] == "--raw" and "/services/" in args[2]:
            return state["counter"]
        if args == gpu.PODS_COMMAND:
            return _result(_list(*items)) if pods_ok else _result(stderr=FORBIDDEN, success=False)
        if args == gpu.NAMESPACES_COMMAND:
            return _result(stderr=FORBIDDEN, success=False)
        if args == gpu.SERVED_MODELS_COMMAND:
            return _result('{"items": []}')
        return _gpu_cluster(args)

    def collect():
        body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", cache_key="c:fp:ns", clock=lambda: now[0])
        now[0] += 20
        return body

    return collect, state, seen


def _counter_reads(seen) -> list[str]:
    return [args[2] for args in seen if args[1] == "--raw" and "/services/" in args[2]]


def test_개요는_직전_수집을_기억해_두_번째부터_구간을_내고_못_읽으면_그_칸만_null이다(monkeypatch):
    collect, state, seen = _counter_cluster(monkeypatch)
    assert collect()["counter"][0]["window_seconds"] is None
    state["counter"] = _result(_counter_metrics({("qwen", "k", "200"): 130, ("qwen", "k", "429"): 10}))
    assert collect()["counter"] == [
        {"model": "qwen", "requests": 40, "rejected": 10, "rejected_ratio": 0.25, "window_seconds": 20.0}]
    state["counter"] = _result(stderr="Command 'kubectl' timed out after 5 seconds", success=False)
    body = collect()
    assert body["counter"] is None and body["serving"]                     # 카운터만 null, 서빙은 그대로
    assert set(_counter_reads(seen)) == {gpu.counter_proxy_command("kukie-gpu")[2]}


def test_모든_파드를_읽었는데_카운터가_없으면_null이고_못_읽으면_kukie_gpu만_읽는다(monkeypatch):
    collect, _, seen = _counter_cluster(monkeypatch, with_counter=False)
    assert collect()["counter"] is None and _counter_reads(seen) == []
    collect, _, seen = _counter_cluster(monkeypatch, pods_ok=False, with_counter=False)
    assert collect()["counter"][0]["model"] == "qwen"
    assert _counter_reads(seen) == [gpu.counter_proxy_command(gpu.COUNTER_DEFAULT_NAMESPACE)[2]]


def test_ServedModel을_못_읽었거나_GPU_노드가_없으면_카운터를_읽지_않는다():
    run, seen = _served_models_cluster(_result(stderr=NO_CRD, success=False), pods=_list(_counter_pod()))
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", clock=lambda: 0.0)
    assert body["counter"] is None
    run, seen2 = _served_models_cluster(_result('{"items": []}'), nodes="nodes.json", pods=_list(_counter_pod()))
    body, _ = clusters_api._collect_gpu(run, namespace_pods="{}", known=True, clock=lambda: 0.0)
    assert body["counter"] is None
    assert not any("/services/" in args[2] for args, _ in seen + seen2 if args[1] == "--raw")


def test_카운터_읽기는_지표_풀의_동시_상한을_넘지_않는다(monkeypatch):
    collect, _, _ = _counter_cluster(monkeypatch)
    monkeypatch.setattr(clusters_api, "GPU_METRICS_WORKERS", 2)
    sizes: list[int] = []
    real = clusters_api.ThreadPoolExecutor

    class Recording(real):
        def __init__(self, max_workers=None, **kwargs):
            sizes.append(max_workers)
            super().__init__(max_workers=max_workers, **kwargs)
    monkeypatch.setattr(clusters_api, "ThreadPoolExecutor", Recording)
    assert collect()["counter"] is not None and max(sizes) == 2
