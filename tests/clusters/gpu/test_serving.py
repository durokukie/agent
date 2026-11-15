"""DCGM·vLLM 지표 파싱과 최근 구간 p95 의 기억 (DURO-124)"""
from __future__ import annotations

import json
import math

import pytest

from gpu_support import L40S_1, L40S_4, _gpu_cluster, _list, _result, fixture, pods
from kukie_agent import clusters_api
from kukie_agent.clusters import gpu


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
