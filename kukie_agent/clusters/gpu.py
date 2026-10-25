"""클러스터 개요의 gpu 칸 — GPU 노드, 그 위의 DCGM 지표, 서빙 중인 vLLM 모델 (DURO-118).

overview.py 처럼 kubectl 출력(문자열)을 받아 응답을 만드는 순수 함수만 둔다. kubectl 은 clusters_api.py 가 돌린다.

읽는 순서 (clusters_api.cluster_overview)
  1. NODES_COMMAND   노드 목록. 못 읽으면(고객 EKS 는 namespace 범위 권한뿐이라 Forbidden) 여기서 멈추고 gpu 는 null.
                     GPU 노드가 없어도 null — 앱은 null 이면 GPU 화면을 띄우지 않는다
  2. PODS_COMMAND    모든 namespace 의 파드. GPU 노드 위의 DCGM exporter·vLLM 파드를 찾는다. 못 읽으면 NAMESPACES_COMMAND 로
                     namespace 를 읽고 namespace 마다 파드를 읽어 읽힌 것만 모은다 — Kukie GPU 읽기 RBAC
                     (kukie-infra customer-templates/gpu-read-rbac-v1.yaml)는 namespace 목록은 클러스터 전체에, 파드 목록은 고객이
                     고른 namespace 에만 준다. namespace 목록도 못 읽으면 개요가 이미 읽은 클러스터 namespace 의 파드에서만 찾는다
                     (running_pods 로 한 번만 파싱한다)
  3. proxy_command   찾은 파드의 /metrics 를 API 서버 **파드 프록시**로 읽는다 (Prometheus 없이 kubectl 권한만으로).
                     서비스 프록시가 아니라 파드 프록시인 이유: DCGM exporter 는 노드마다 하나인 DaemonSet 이라 서비스로
                     부르면 아무 파드 하나에 붙어 다른 노드의 값을 받는다. vLLM 도 레플리카마다 값이 다르다
  지표를 못 읽으면 그 노드의 실시간 값만 null, 그 파드만 serving 에서 빠진다 — 개요는 그대로 200.

GPU 노드 — nodes
  status.allocatable 에 GPU_RESOURCES 중 하나가 1 이상인 노드. allocatable 에 그 자원 칸이 아예 없을 때만 capacity 를 본다 —
  device plugin 이 죽으면 kubelet 이 capacity 는 두고 allocatable 만 0 으로 내리는데, 그 노드는 쓸 수 있는 GPU 가 없다.
  count    라벨 nvidia.com/gpu.count(GPU Feature Discovery 가 붙이는 물리 GPU 수)가 있으면 그것, 없으면 자원 수.
           time-slicing·MPS 를 켜면 자원 수가 나눈 조각 수로 불어나기 때문이다
  product  라벨 nvidia.com/gpu.product (AMD 는 amd.com/gpu.product-name), 없으면 인스턴스 종류 라벨, 그것도 없으면 ""
  memory_mib  노드 전체 GPU 메모리 = GPU 하나 메모리(라벨 nvidia.com/gpu.memory, MiB) × count.
              라벨이 없으면 DCGM 의 FB_USED + FB_FREE + FB_RESERVED 합, 그것도 없으면 0
  실시간 값(DCGM) — GPU 마다 나오는 줄을 노드 하나로 모은다: 사용률은 평균, 메모리 사용·전력은 합, 온도는 가장 높은 것.
  MIG 인스턴스는 부모 GPU 의 UUID 를 같이 쓰고 GPU_I_ID 로만 갈리므로 (UUID, GPU_I_ID) 로 묶는다.
  AMD 는 DCGM 이 없어 실시간 값이 늘 null 이다.

서빙 — serving
  GPU 노드 위의 vLLM 파드(컨테이너 이미지나 명령에 vllm 이 있는 것)의 /metrics 에서 model_name 라벨마다 한 줄.
  GPU 노드로 좁히는 것은 `vllm bench` Job 같은 클라이언트 파드가 지표 대상 칸(MAX_METRIC_TARGETS)을 먹지 않게 하려는 것이다.
  아래 지표만 본다 — vllm:cache_config_info 처럼 model_name 이 없는 info 지표로 유령 줄이 생기지 않게.
    running·waiting   vllm:num_requests_running·waiting (엔진이 여럿이면 합)
    kv_cache_pct      vllm:kv_cache_usage_perc (옛 vLLM 은 gpu_cache_usage_perc) × 100, 엔진이 여럿이면 평균
    ttft_p95_ms       vllm:time_to_first_token_seconds 히스토그램의 p95
    itl_p95_ms        vllm:inter_token_latency_seconds (옛 vLLM 은 time_per_output_token_seconds) 히스토그램의 p95
  p95 는 Prometheus histogram_quantile 처럼 버킷 안에서 선형으로 나눈 값이다. 히스토그램은 서빙 프로세스가 뜬 뒤 누적이라
  "지금 5분" 이 아니라 "뜬 뒤 전체" 의 p95 다 — 한 번 읽기로는 구간을 자를 수 없다. 요청이 없었으면 null.
  vLLM 지표가 하나도 없는 파드(라우터 등)는 빠진다.

측정 기록 — benchmarks
  측정 Job 도구가 아직 없어 늘 빈 배열이다. 계약(api-spec §3)대로 채울 때는 에이전트가 거른다 — 우리 GPU 클러스터의 팀 Admin 만.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

#: 노드 목록 (클러스터 범위). 부르는 쪽이 `-n` 을 붙이지 않는다
NODES_COMMAND: list[str] = ["get", "nodes", "-o", "json"]
#: DCGM exporter·vLLM 파드를 찾는 파드 목록 (모든 namespace). 부르는 쪽이 `-n` 을 붙이지 않는다
PODS_COMMAND: list[str] = ["get", "pods", "-A", "-o", "json"]

#: 모든 namespace 파드를 못 읽을 때 namespace 마다 파드를 읽으려고 보는 namespace 목록 (클러스터 범위)
NAMESPACES_COMMAND: list[str] = ["get", "namespaces", "-o", "json"]
#: namespace 마다 파드를 읽는 수 상한. 넘으면 GPU 쪽 이름(NAMESPACE_HINTS)이 들어간 것부터 고른다
MAX_POD_NAMESPACES = 24
#: DCGM exporter·vLLM 이 흔히 도는 namespace 이름 조각 — 상한을 넘을 때 먼저 고른다. kube-prometheus-stack 으로 깐 DCGM 은 보통 monitoring 에 있다
NAMESPACE_HINTS: tuple[str, ...] = ("gpu", "nvidia", "dcgm", "vllm", "llm", "serving", "inference",
                                    "monitor", "metric", "observ", "prom")

#: GPU 로 보는 확장 자원 (위에 있는 것이 먼저)
GPU_RESOURCES: tuple[str, ...] = ("nvidia.com/gpu", "amd.com/gpu")

#: 지표를 읽는 파드 수 상한 (DCGM·vLLM 따로). 한 번 읽기가 시간 제한까지 걸려도 개요가 끝없이 늘지 않게
MAX_METRIC_TARGETS = 8

DCGM_PORT = 9400
VLLM_PORT = 8000


# ── 노드 ─────────────────────────────────────────────────

def gpu_nodes(stdout: str) -> list[dict[str, Any]]:
    """노드 목록에서 GPU 노드만 (모듈 설명 "GPU 노드"). 실시간 값은 아직 null 이다 — apply_dcgm 이 채운다.

    모양이 다르면 ValueError — 부르는 쪽이 gpu 를 null 로 둔다. 다른 목록(_items)과 달리 items 칸이 없거나 객체가 아닌
    항목이 섞여도 ValueError 다: 빈 목록은 "GPU 노드 없음" 으로 server 의 hasGpu 를 false 로 덮기 때문이다 (PR #119 리뷰).
    """
    data = json.loads(stdout or "")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("노드 목록 모양이 아니다")
    rows: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        labels = meta.get("labels") or {}
        resource, amount = _gpu_resource(status)
        if resource is None:
            continue
        count = _positive_int(labels.get("nvidia.com/gpu.count")) or amount
        per_gpu = _memory_label_mib(labels)
        rows.append({
            "node": meta.get("name", ""),
            "product": _product(labels, resource),
            "count": count,
            "memory_mib": per_gpu * count if per_gpu else 0,
            "utilization_pct": None,
            "memory_used_mib": None,
            "temperature_c": None,
            "power_w": None,
        })
    rows.sort(key=lambda r: r["node"])
    return rows


def _gpu_resource(status: dict[str, Any]) -> tuple[str | None, int]:
    allocatable = status.get("allocatable") or {}
    capacity = status.get("capacity") or {}
    for name in GPU_RESOURCES:
        amount = _positive_int(allocatable[name] if name in allocatable else capacity.get(name))
        if amount:
            return name, amount
    return None, 0


def _product(labels: dict[str, Any], resource: str) -> str:
    keys = ("nvidia.com/gpu.product",) if resource == "nvidia.com/gpu" else (
        "amd.com/gpu.product-name", "beta.amd.com/gpu.product-name")
    for key in (*keys, "node.kubernetes.io/instance-type"):
        value = labels.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _memory_label_mib(labels: dict[str, Any]) -> int:
    """GPU 하나 메모리(MiB). NVIDIA GFD 는 MiB 숫자, AMD node labeller 는 `64G` 처럼 단위를 붙인다."""
    nvidia = _positive_int(labels.get("nvidia.com/gpu.memory"))
    if nvidia:
        return nvidia
    amd = labels.get("amd.com/gpu.vram") or labels.get("beta.amd.com/gpu.vram")
    match = re.fullmatch(r"(\d+)\s*([GM])i?B?", amd.strip(), re.IGNORECASE) if isinstance(amd, str) else None
    if match:
        return int(match[1]) * (1024 if match[2].upper() == "G" else 1)
    return 0


# ── 지표를 읽을 파드 ───────────────────────────────────────

def running_pods(stdout: str) -> list[dict[str, Any]]:
    """파드 목록에서 Running 이고 이름이 있는 것. 모양이 다르면 ValueError."""
    rows = []
    for item in _items(stdout):
        meta = item.get("metadata")
        if isinstance(meta, dict) and meta.get("name") and (item.get("status") or {}).get("phase") == "Running":
            rows.append(item)
    return rows


def dcgm_targets(pods: list[dict[str, Any]], gpu_node_names: set[str]) -> dict[str, tuple[str, str, int]]:
    """GPU 노드마다 그 위의 DCGM exporter 파드 하나 — {노드: (namespace, 파드, 포트)}. pods 는 running_pods."""
    targets: dict[str, tuple[str, str, int]] = {}
    for item in pods:
        node = (item.get("spec") or {}).get("nodeName")
        if node not in gpu_node_names or node in targets or not _is_dcgm(item):
            continue
        meta = item["metadata"]
        targets[node] = (meta.get("namespace") or "default", meta["name"],
                         _port(item, names=("metrics", "gpu-metrics"), default=DCGM_PORT, match=_dcgm_container))
        if len(targets) >= MAX_METRIC_TARGETS:
            break
    return targets


def vllm_targets(pods: list[dict[str, Any]], gpu_node_names: set[str]) -> list[tuple[str, str, int, str]]:
    """GPU 노드 위의 vLLM 파드 — [(namespace, 파드, 포트, 노드)]. 이름순. pods 는 running_pods."""
    targets: list[tuple[str, str, int, str]] = []
    for item in pods:
        if (item.get("spec") or {}).get("nodeName") not in gpu_node_names:
            continue
        if _is_dcgm(item) or not any(_vllm_container(c) for c in _containers(item)):
            continue
        meta = item["metadata"]
        targets.append((meta.get("namespace") or "default", meta["name"], _vllm_port(item),
                        (item.get("spec") or {}).get("nodeName") or ""))
    targets.sort(key=lambda t: (t[0], t[1]))
    return targets[:MAX_METRIC_TARGETS]


def namespace_names(stdout: str, *, skip: str | None = None, prefer: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """파드를 읽어 볼 namespace — kube-* 와 skip(개요가 이미 읽은 클러스터 namespace)은 뺀다(Kukie 역할은 시스템
    namespace 를 읽지 않는다). prefer(GPU 지표 파드가 나온 적 있는 것)와 GPU 쪽 이름이 먼저, 그다음 이름순으로
    MAX_POD_NAMESPACES 개까지 — 상한이 지표 namespace 를 자르지 않게. 모양이 다르면 ValueError."""
    names = sorted({name for item in _items(stdout)
                    if isinstance(name := (item.get("metadata") or {}).get("name"), str) and name
                    and not name.startswith("kube-") and name != skip})
    names.sort(key=lambda name: not (name in prefer or hinted_namespace(name)))
    return names[:MAX_POD_NAMESPACES]


def hinted_namespace(name: str) -> bool:
    """GPU 지표·서빙 쪽 이름(NAMESPACE_HINTS)이 들어간 namespace."""
    return any(hint in name for hint in NAMESPACE_HINTS)


def namespace_pods_command(namespace: str) -> list[str]:
    """namespace 하나의 파드 목록 kubectl 인자 (모듈 설명 "읽는 순서" 2의 대체 길)."""
    return ["get", "pods", "-n", namespace, "-o", "json"]


def has_gpu_targets(stdout: str, gpu_node_names: set[str]) -> bool:
    """파드 목록에 GPU 노드 위의 DCGM exporter 나 vLLM 파드가 있는가. 모양이 다르면 False (개요를 실패시키지 않는다)."""
    try:
        pods = running_pods(stdout)
        return bool(dcgm_targets(pods, gpu_node_names) or vllm_targets(pods, gpu_node_names))
    except (ValueError, AttributeError, TypeError):
        return False


def merge_pod_lists(outputs: list[str]) -> str:
    """파드 목록 출력 여럿을 하나로 — (namespace, 이름)이 같은 파드는 한 번만. 깨진 출력 하나는 건너뛴다."""
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for stdout in outputs:
        try:
            rows = _items(stdout)
        except ValueError:      # JSON 이 아니거나 items 가 목록이 아니다 (json.JSONDecodeError 도 ValueError)
            continue
        for item in rows:
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            key = (str(meta.get("namespace") or ""), str(meta.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return json.dumps({"items": items})


def proxy_command(namespace: str, pod: str, port: int) -> list[str]:
    """API 서버 파드 프록시로 /metrics 를 읽는 kubectl 인자 (모듈 설명 "읽는 순서" 3)."""
    return ["get", "--raw", f"/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy/metrics"]


def _containers(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (item.get("spec") or {}).get("containers") or [] if isinstance(c, dict)]


def _dcgm_container(container: dict[str, Any]) -> bool:
    return "dcgm-exporter" in str(container.get("image") or "").lower()


def _is_dcgm(item: dict[str, Any]) -> bool:
    labels = (item.get("metadata") or {}).get("labels") or {}
    return (labels.get("app") in ("nvidia-dcgm-exporter", "dcgm-exporter")
            or labels.get("app.kubernetes.io/name") == "dcgm-exporter"
            or any(_dcgm_container(c) for c in _containers(item)))


def _vllm_container(container: dict[str, Any]) -> bool:
    words = [str(container.get("image") or ""), *map(str, container.get("command") or []),
             *map(str, container.get("args") or [])]
    return any("vllm" in word.lower() for word in words)


def _vllm_port(item: dict[str, Any]) -> int:
    """`--port N`·`--port=N` 인자가 있으면 그것, 없으면 http 라는 이름의 포트, 첫 포트, vLLM 기본 8000 순."""
    for container in _containers(item):
        if not _vllm_container(container):
            continue
        words = [str(w) for w in [*(container.get("command") or []), *(container.get("args") or [])]]
        for i, word in enumerate(words):
            if word == "--port" and i + 1 < len(words) and words[i + 1].isdigit():
                return int(words[i + 1])
            if word.startswith("--port=") and word[7:].isdigit():
                return int(word[7:])
    return _port(item, names=("http",), default=VLLM_PORT, match=_vllm_container)


def _port(item: dict[str, Any], *, names: tuple[str, ...], default: int, match) -> int:
    ports = [p for c in _containers(item) if match(c) for p in c.get("ports") or [] if isinstance(p, dict)]
    for port in ports:
        if port.get("name") in names and _positive_int(port.get("containerPort")):
            return int(port["containerPort"])
    for port in ports:
        if _positive_int(port.get("containerPort")):
            return int(port["containerPort"])
    return default


# ── DCGM ─────────────────────────────────────────────────

def apply_dcgm(row: dict[str, Any], text: str) -> None:
    """DCGM exporter /metrics 로 노드 한 줄의 실시간 값을 채운다 (모듈 설명 "GPU 노드")."""
    per_gpu: dict[tuple[str, str], dict[str, float]] = {}
    for name, labels, value in parse_metrics(text):
        if not name.startswith("DCGM_FI_DEV_"):
            continue
        gpu = (labels.get("UUID") or labels.get("gpu") or "", labels.get("GPU_I_ID") or "")
        per_gpu.setdefault(gpu, {})[name] = value
    if not per_gpu:
        return

    def values(metric: str) -> list[float]:
        return [gpu[metric] for gpu in per_gpu.values() if metric in gpu]

    util, used, temp, power = (values(m) for m in (
        "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_POWER_USAGE"))
    row["utilization_pct"] = round(sum(util) / len(util)) if util else None
    row["memory_used_mib"] = round(sum(used)) if used else None
    row["temperature_c"] = round(max(temp)) if temp else None
    row["power_w"] = round(sum(power)) if power else None
    if not row["memory_mib"]:
        total = sum(values("DCGM_FI_DEV_FB_USED") + values("DCGM_FI_DEV_FB_FREE") + values("DCGM_FI_DEV_FB_RESERVED"))
        row["memory_mib"] = round(total)


# ── vLLM ─────────────────────────────────────────────────

def serving_rows(text: str, *, pod: str, node: str) -> list[dict[str, Any]]:
    """vLLM /metrics 에서 모델마다 한 줄 (모듈 설명 "서빙"). vLLM 지표가 없으면 빈 목록."""
    models: dict[str, dict[str, Any]] = {}
    for name, labels, value in parse_metrics(text):
        metric = name[len("vllm:"):] if name.startswith("vllm:") else ""
        if metric not in _VLLM_METRICS:
            continue
        model = models.setdefault(labels.get("model_name") or pod, {
            "running": 0.0, "waiting": 0.0, "kv": [], "kv_old": [], "ttft": {}, "itl": {}, "itl_old": {}})
        if metric == "num_requests_running":
            model["running"] += value
        elif metric == "num_requests_waiting":
            model["waiting"] += value
        elif metric == "kv_cache_usage_perc":
            model["kv"].append(value)
        elif metric == "gpu_cache_usage_perc":
            model["kv_old"].append(value)
        elif metric == "time_to_first_token_seconds_bucket":
            _add_bucket(model["ttft"], labels, value)
        elif metric == "inter_token_latency_seconds_bucket":
            _add_bucket(model["itl"], labels, value)
        elif metric == "time_per_output_token_seconds_bucket":
            _add_bucket(model["itl_old"], labels, value)

    rows = []
    for model_name, m in sorted(models.items()):
        kv = m["kv"] or m["kv_old"]
        rows.append({
            "model": model_name,
            "pod": pod,
            "node": node,
            "running": round(m["running"]),
            "waiting": round(m["waiting"]),
            "kv_cache_pct": round(sum(kv) / len(kv) * 100, 1) if kv else 0,
            "ttft_p95_ms": _ms(histogram_quantile(0.95, m["ttft"])),
            "itl_p95_ms": _ms(histogram_quantile(0.95, m["itl"] or m["itl_old"])),
        })
    return rows


_VLLM_METRICS = frozenset({
    "num_requests_running", "num_requests_waiting", "kv_cache_usage_perc", "gpu_cache_usage_perc",
    "time_to_first_token_seconds_bucket", "inter_token_latency_seconds_bucket", "time_per_output_token_seconds_bucket",
})


def _add_bucket(buckets: dict[float, float], labels: dict[str, str], value: float) -> None:
    """같은 le 는 더한다 — 엔진이 여럿(데이터 병렬)이면 모델 하나에 히스토그램이 여럿 나온다."""
    le = _float(labels.get("le"))
    if le is not None:
        buckets[le] = buckets.get(le, 0.0) + value


def histogram_quantile(q: float, buckets: dict[float, float]) -> float | None:
    """누적 버킷 {상한: 개수} 의 q 분위수 — Prometheus histogram_quantile 과 같은 선형 보간. 요청이 없으면 None.

    +Inf 버킷에 걸리면 가장 큰 유한 상한을 준다 (Prometheus 와 같다).
    """
    if math.inf not in buckets or buckets[math.inf] <= 0:
        return None
    total = buckets[math.inf]
    rank = q * total
    lower_bound, lower_count = 0.0, 0.0
    for bound in sorted(buckets):
        count = buckets[bound]
        if count >= rank:
            if bound == math.inf:
                return lower_bound
            if count == lower_count:
                return bound
            return lower_bound + (bound - lower_bound) * (rank - lower_count) / (count - lower_count)
        lower_bound, lower_count = bound, count
    return None


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


# ── 합치기 ───────────────────────────────────────────────

def build(nodes: list[dict[str, Any]], serving: list[dict[str, Any]]) -> dict[str, Any] | None:
    """ClusterGpu. GPU 노드가 없으면 None — 앱은 GPU 화면을 띄우지 않는다."""
    if not nodes:
        return None
    return {"nodes": nodes, "serving": serving, "benchmarks": []}


# ── Prometheus 글 형식 ────────────────────────────────────

_LINE = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Prometheus 글 형식(exposition)을 (이름, 라벨, 값) 목록으로. 읽지 못하는 줄은 건너뛴다.

    부가 정보라 깨진 출력도 오류로 올리지 않는다 — 개요 전체를 502 로 만들 까닭이 없다.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match is None:
            continue
        value = _float(match[3])
        if value is None or not math.isfinite(value):
            continue            # ±Inf 값은 round() 에서 OverflowError 다. 버킷의 le="+Inf" 는 라벨이라 따로 읽는다
        labels = {key: raw.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
                  for key, raw in _LABEL.findall(match[2] or "")}
        rows.append((match[1], labels, value))
    return rows


def _float(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return float(value)        # "+Inf"·"Inf" 도 읽는다
    except ValueError:
        return None


# ── 공통 ─────────────────────────────────────────────────

def _items(stdout: str) -> list[dict[str, Any]]:
    data = json.loads(stdout or "{}")
    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError("kubectl 출력의 items 가 목록이 아니다")
    return [item for item in items if isinstance(item, dict)]


def _positive_int(value: Any) -> int:
    """자원 수량·라벨 숫자. 정수가 아니거나 0 이하면 0 (`500m` 같은 소수 수량은 GPU 에 없다)."""
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
