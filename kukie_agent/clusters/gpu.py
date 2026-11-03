"""클러스터 개요의 gpu 칸 — GPU 노드, 그 위의 DCGM 지표, 서빙 중인 vLLM 모델 (DURO-118), ServedModel 상태 (DURO-182).

overview.py 처럼 kubectl 출력(문자열)을 받아 응답을 만드는 순수 함수만 둔다. kubectl 은 clusters_api.py 가 돌린다.

읽는 순서 (clusters_api.cluster_overview)
  1. NODES_COMMAND   노드 목록. 못 읽으면(고객 EKS 는 namespace 범위 권한뿐이라 Forbidden) 여기서 멈추고 gpu 는 null.
                     GPU 노드가 없으면 GPU 를 기다리는 파드(waiting)를 찾는다 — 아래 "꺼진 GPU 클러스터"
  2. PODS_COMMAND    모든 namespace 의 파드. GPU 노드 위의 DCGM exporter·vLLM 파드를 찾는다. 못 읽으면 NAMESPACES_COMMAND 로
                     namespace 를 읽고 namespace 마다 파드를 읽어 읽힌 것만 모은다 — Kukie GPU 읽기 RBAC
                     (kukie-infra customer-templates/gpu-read-rbac-v1.yaml)는 namespace 목록은 클러스터 전체에, 파드 목록은 고객이
                     고른 namespace 에만 준다. namespace 목록도 못 읽으면 개요가 이미 읽은 클러스터 namespace 의 파드에서만 찾는다
                     (running_pods 로 한 번만 파싱한다)
  3. SERVED_MODELS_COMMAND  모든 namespace 의 ServedModel. 지표보다 먼저 읽는다 — 지표 파드가 매달려도 예산이 남게.
                     GPU 노드가 없으면 기다리는 파드가 있거나 server 값이 GPU 클러스터(true)일 때만 읽는다 — 아래 "ServedModel"
  4. proxy_command   찾은 파드의 /metrics 를 API 서버 **파드 프록시**로 읽는다 (Prometheus 없이 kubectl 권한만으로).
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
    ttft_p95_ms       vllm:time_to_first_token_seconds 히스토그램의 **최근 구간** p95
    itl_p95_ms        vllm:inter_token_latency_seconds (옛 vLLM 은 time_per_output_token_seconds) 히스토그램의 최근 구간 p95
    latency_window_seconds  그 구간의 길이(초) — 직전 수집부터 이번 수집까지
  p95 는 Prometheus histogram_quantile 처럼 버킷 안에서 선형으로 나눈 값이다. 히스토그램은 서빙 프로세스가 뜬 뒤 누적이라
  한 번 읽은 값으로 내면 "뜬 뒤 전체" 의 p95 다 — 부하가 끝나도 안 내려가고 방금 느려진 것도 안 보인다 (DURO-124).
  그래서 파드마다 직전 수집의 누적 히스토그램(LatencySample — 버킷·count·sum)을 기억하고 두 수집의 차이로 p95 를 낸다
  (최근 구간 p95). 기억은 clusters_api 가 연결·파드마다 메모리에 둔다 (pod_identity 로 같은 프로세스인지 가린다).
  구간의 시각은 그 파드의 /metrics 를 읽은 시각이다 — 라운드의 끝이 아니다 (다른 파드가 느려도 구간이 안 늘어난다).
    기억이 없다(첫 수집·파드가 바뀜·직전이 너무 오래됨)  p95 null, latency_window_seconds null
    값이 줄었다(프로세스가 다시 떠 처음부터 센다)         기억을 버리고 이번은 둘 다 null — 다음 수집부터 다시 잇는다
    구간에 요청이 없었다                                    p95 null, latency_window_seconds 는 있다 (앱은 "최근 요청 없음")
  vLLM 지표가 하나도 없는 파드(라우터 등)는 빠진다.

꺼진 GPU 클러스터 — waiting
  GPU 노드 그룹을 0대로 내려 둔 클러스터(우리 kukie-gpu 처럼 평소 0대)는 노드 목록에 GPU 노드가 없다. 그래도 GPU 를 요청한
  파드(vLLM 등)가 남아 Pending 이면 "GPU 클러스터인데 지금 꺼짐" 이다. 그 파드를 waiting 에 싣고 nodes 는 빈 배열로 둔다.
  끝난 파드(Succeeded·Failed)는 세지 않는다. 파드는 2와 같은 길로 찾는다(이때는 지표를 읽지 않으니 남은 예산을 다 쓴다). 다만 server 의 hasGpu 가 false 인 클러스터
  (GPU 가 없다고 이미 안 곳)는 찾지 않는다 — GPU 없는 클러스터마다 개요 때 파드 조회가 늘지 않게. 그런 곳도 몇 분에 한 번은
  다시 찾는다(clusters_api.GPU_ABSENT_RECHECK — 0대인 채 GPU 워크로드를 새로 올린 경우). GPU 노드가 있을 때도
  같은 파드 목록에서 GPU 를 기다리는 파드를 waiting 에 싣는다(늘어나는 중인 레플리카 등).

ServedModel — served_models
  kukie-gpu-operator 의 CRD. 한 줄이 ServedModel 하나다: namespace·name(metadata), model(spec.source.huggingface),
  phase·reason·message(status), download_percent(status.download.percent), max_concurrency(status.baseline.maxConcurrency).
  namespace·이름순. 오퍼레이터가 아직 status 를 쓰지 않았으면 문자열 칸은 "", 숫자 칸은 null 이다.
    null  CRD 가 없거나(애드온을 안 깐 곳) 거부·시간 초과·예산 부족·모양이 다른 출력으로 못 읽었다. 개요는 그대로 200
    []    읽었는데 하나도 없다
  GPU 노드가 없어도 읽는 곳: 기다리는 파드가 있거나 server 값이 true 인 곳(꺼진 GPU 클러스터). 그 밖의 GPU 노드 없는 곳은
  읽지 않는다 — GPU 없는 일반 클러스터마다 개요 때 kubectl 이 늘지 않게. 꺼진 GPU 클러스터에서 읽혔으면 하나도 없어도(애드온이
  깔린 곳이다) 빈 gpu 칸(nodes 빈 배열)을 주고 GPU 클러스터로 둔다 — 모델을 지우고 노드를 끈 뒤에도 GPU 화면이 이어지게.

측정 기록 — benchmarks
  측정 Job 도구가 아직 없어 늘 빈 배열이다. 계약(api-spec §3)대로 채울 때는 에이전트가 거른다 — 우리 GPU 클러스터의 팀 Admin 만.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

#: 노드 목록 (클러스터 범위). 부르는 쪽이 `-n` 을 붙이지 않는다
NODES_COMMAND: list[str] = ["get", "nodes", "-o", "json"]
#: DCGM exporter·vLLM 파드를 찾는 파드 목록 (모든 namespace). 부르는 쪽이 `-n` 을 붙이지 않는다
PODS_COMMAND: list[str] = ["get", "pods", "-A", "-o", "json"]

#: 모든 namespace 의 ServedModel (모듈 설명 "ServedModel"). 부르는 쪽이 `-n` 을 붙이지 않는다
SERVED_MODELS_COMMAND: list[str] = ["get", "servedmodels.serving.kukie.io", "-A", "-o", "json"]

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
    rows: list[dict[str, Any]] = []
    for item in _strict_items(stdout):
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


# ── ServedModel ──────────────────────────────────────────

def served_models(stdout: str) -> list[dict[str, Any]]:
    """ServedModel 목록을 응답 줄로 (모듈 설명 "ServedModel"). 이름 없는 항목은 뺀다. 모양이 다르면 ValueError —
    부르는 쪽이 served_models 를 null 로 둔다. 빈 목록은 "없다" 라는 뜻이라 gpu_nodes 처럼 엄격하게 읽는다.
    download_percent·max_concurrency 는 CRD 스키마가 integer 라 오퍼레이터가 정수로만 쓴다 — 다른 값은 null 이다."""
    rows: list[dict[str, Any]] = []
    for item in _strict_items(stdout):
        meta = item.get("metadata")
        if not isinstance(meta, dict) or not meta.get("name"):
            continue
        status = item.get("status") or {}
        rows.append({
            "namespace": meta.get("namespace") or "default",
            "name": meta["name"],
            "model": ((item.get("spec") or {}).get("source") or {}).get("huggingface") or "",
            "phase": status.get("phase") or "",
            "reason": status.get("reason") or "",
            "message": status.get("message") or "",
            "download_percent": _int_or_none((status.get("download") or {}).get("percent")),
            "max_concurrency": _int_or_none((status.get("baseline") or {}).get("maxConcurrency")),
        })
    rows.sort(key=lambda r: (r["namespace"], r["name"]))
    return rows


def served_models_absent(stderr: str) -> bool:
    """ServedModel 조회 실패가 "CRD 가 없다" 나 "볼 수 없다" 라고 확인된 것인가 — 시간 초과처럼 모르는 실패와 가른다.
    거부(Forbidden)는 _pods_by_namespace 와 같이 Kukie 가 볼 수 없는 곳이라 못 본 곳으로 치지 않는다.
    server 의 hasGpu 를 false 로 덮는 근거라 kubectl 문구로 좁힌다 — 사이 프록시의 `403 Forbidden` 본문은 모르는 실패다.
    그 밖의 문구(NotFound 등)도 모르는 실패다: hasGpu 를 덮지 않는 안전한 쪽이다."""
    lowered = stderr.lower()
    return "doesn't have a resource type" in lowered or "(forbidden)" in lowered or " is forbidden" in lowered


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


def vllm_targets(pods: list[dict[str, Any]], gpu_node_names: set[str]) -> list[tuple[str, str, int, str, str]]:
    """GPU 노드 위의 vLLM 파드 — [(namespace, 파드, 포트, 노드, 정체)]. 이름순. pods 는 running_pods. 정체는 pod_identity."""
    targets: list[tuple[str, str, int, str, str]] = []
    for item in pods:
        if (item.get("spec") or {}).get("nodeName") not in gpu_node_names:
            continue
        if _is_dcgm(item) or not any(_vllm_container(c) for c in _containers(item)):
            continue
        meta = item["metadata"]
        targets.append((meta.get("namespace") or "default", meta["name"], _vllm_port(item),
                        (item.get("spec") or {}).get("nodeName") or "", pod_identity(item)))
    targets.sort(key=lambda t: (t[0], t[1]))
    return targets[:MAX_METRIC_TARGETS]


def pod_identity(item: dict[str, Any]) -> str:
    """파드의 정체 — uid(없으면 시작 시각)와 vLLM 컨테이너의 재시작 수. 최근 구간 p95 의 기억(LatencySample)을 같은 서빙
    프로세스에만 잇는다: 같은 이름으로 다시 뜬 파드(StatefulSet 의 qwen-0)는 uid 가, 컨테이너만 다시 뜬 파드는 재시작 수가
    다르다. 둘 다 히스토그램을 처음부터 다시 세는 프로세스라 옛 기억과 빼면 안 된다.
    재시작 수는 vLLM 컨테이너(_vllm_container, spec.containers — vllm_targets 가 찾는 것과 같은 범위)의 것만 센다 —
    사이드카(메시·로그)가 다시 떠도 vLLM 프로세스와 히스토그램은 그대로라 기억을 버릴 까닭이 없다 (agent#137 Codex 리뷰).
    native sidecar(initContainers + restartPolicy Always)로 띄운 vLLM 은 애초에 대상이 아니다 (_containers)."""
    meta = item.get("metadata") or {}
    status = item.get("status") or {}
    vllm_names = {c.get("name") for c in _containers(item) if _vllm_container(c)}
    restarts = sum(_positive_int(c.get("restartCount")) for c in status.get("containerStatuses") or []
                   if isinstance(c, dict) and c.get("name") in vllm_names)
    return f"{meta.get('uid') or status.get('startTime') or ''}:{restarts}"


def namespace_names(stdout: str, *, skip: str | None = None,
                    prefer: set[str] | frozenset[str] = frozenset()) -> tuple[list[str], bool]:
    """(파드를 읽어 볼 namespace, 목록 전부인가) — kube-* 와 skip(개요가 이미 읽은 클러스터 namespace)은 뺀다(Kukie 역할은
    시스템 namespace 를 읽지 않는다). prefer(GPU 지표·기다리는 파드가 나온 적 있는 것)와 GPU 쪽 이름이 먼저, 그다음
    이름순으로 MAX_POD_NAMESPACES 개까지 — 상한이 지표 namespace 를 자르지 않게. 잘랐으면 두 번째 값이 False 다
    (끝까지 읽었다고 말할 수 없다). 모양이 다르면 ValueError."""
    names = sorted({name for item in _items(stdout)
                    if isinstance(name := (item.get("metadata") or {}).get("name"), str) and name
                    and not name.startswith("kube-") and name != skip})
    names.sort(key=lambda name: not (name in prefer or hinted_namespace(name)))
    return names[:MAX_POD_NAMESPACES], len(names) <= MAX_POD_NAMESPACES


def hinted_namespace(name: str) -> bool:
    """GPU 지표·서빙 쪽 이름(NAMESPACE_HINTS)이 들어간 namespace."""
    return any(hint in name for hint in NAMESPACE_HINTS)


def namespace_pods_command(namespace: str) -> list[str]:
    """namespace 하나의 파드 목록 kubectl 인자 (모듈 설명 "읽는 순서" 2의 대체 길)."""
    return ["get", "pods", "-n", namespace, "-o", "json"]


def waiting_pods(stdout: str) -> list[dict[str, Any]]:
    """GPU 를 요청했는데 아직 돌지 않는 파드 — [{namespace, pod, gpus, phase}]. 끝난 파드(Succeeded·Failed)와 Running 은 뺀다.
    이름순으로 MAX_METRIC_TARGETS 개까지. 모양이 다르면 ValueError."""
    rows: list[dict[str, Any]] = []
    for item in _items(stdout):
        meta = item.get("metadata")
        phase = (item.get("status") or {}).get("phase") or "Pending"
        if not isinstance(meta, dict) or not meta.get("name") or phase in ("Running", "Succeeded", "Failed"):
            continue
        gpus = sum(_requested_gpus(c) for c in _containers(item))
        if gpus:
            rows.append({"namespace": meta.get("namespace") or "default", "pod": meta["name"],
                         "gpus": gpus, "phase": phase})
    rows.sort(key=lambda r: (r["namespace"], r["pod"]))
    return rows[:MAX_METRIC_TARGETS]


def _requested_gpus(container: dict[str, Any]) -> int:
    """컨테이너가 요청한 GPU 수. 확장 자원은 limits 만 적어도 requests 가 같은 값이 된다 — 둘 중 큰 것."""
    resources = container.get("resources") or {}
    return max((_positive_int((resources.get(kind) or {}).get(name))
                for kind in ("limits", "requests") for name in GPU_RESOURCES), default=0)


def has_waiting_pods(stdout: str) -> bool:
    """파드 목록에 GPU 를 기다리는 파드가 있는가. 모양이 다르면 False (기억을 고칠 때만 쓴다)."""
    try:
        return bool(waiting_pods(stdout))
    except (ValueError, AttributeError, TypeError):
        return False


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
    """API 서버 파드 프록시로 /metrics 를 읽는 kubectl 인자 (모듈 설명 "읽는 순서" 4)."""
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

@dataclass(frozen=True)
class Histogram:
    """Prometheus 히스토그램 하나 — 누적 버킷 {상한: 개수} 와 _count·_sum. 엔진이 여럿이면 같은 le 끼리 더한 값."""
    buckets: dict[float, float]
    count: float = 0.0
    sum: float = 0.0


@dataclass(frozen=True)
class LatencySample:
    """파드 하나의 /metrics 를 한 번 읽어 둔 누적 히스토그램 — (모델, 종류) 마다 하나. 종류는 _HISTOGRAM_KINDS 의 값.

    at 은 읽은 시각(부르는 쪽의 시계 — clusters_api 는 time.monotonic). 다음 수집이 이것과의 차이로 최근 구간 p95 를 낸다.
    """
    at: float
    histograms: dict[tuple[str, str], Histogram]


#: 히스토그램 지표 이름(vllm: 과 _bucket·_count·_sum 을 뗀 것) → 종류
_HISTOGRAM_KINDS = {
    "time_to_first_token_seconds": "ttft",
    "inter_token_latency_seconds": "itl",
    "time_per_output_token_seconds": "itl_old",      # 옛 vLLM
}
_GAUGE_METRICS = frozenset({"num_requests_running", "num_requests_waiting", "kv_cache_usage_perc", "gpu_cache_usage_perc"})


def serving_rows(text: str, *, pod: str, node: str, previous: LatencySample | None = None,
                 at: float = 0.0) -> tuple[list[dict[str, Any]], LatencySample]:
    """(vLLM /metrics 에서 모델마다 한 줄, 이번에 읽은 누적 히스토그램) — 모듈 설명 "서빙". vLLM 지표가 없으면 빈 목록.

    p95 는 previous(같은 파드의 직전 수집)와의 차이로 낸다. previous 가 없으면 null 이고, 어느 히스토그램이든 값이 줄었으면
    (서빙 프로세스가 다시 떴다) 그 파드의 모든 줄이 null 이다 — 부르는 쪽은 돌려준 LatencySample 을 다음 기억으로 두면 된다.
    둘 다 latency_window_seconds 가 null 이다. 구간은 있는데 요청이 없었으면 p95 만 null 이다.
    """
    models: dict[str, dict[str, Any]] = {}
    histograms: dict[tuple[str, str], dict[str, Any]] = {}
    for name, labels, value in parse_metrics(text):
        metric = name[len("vllm:"):] if name.startswith("vllm:") else ""
        base, _, part = metric.rpartition("_")
        kind = _HISTOGRAM_KINDS.get(base) if part in ("bucket", "count", "sum") else None
        if metric not in _GAUGE_METRICS and kind is None:
            continue
        model_name = labels.get("model_name") or pod
        model = models.setdefault(model_name, {"running": 0.0, "waiting": 0.0, "kv": [], "kv_old": []})
        if metric == "num_requests_running":
            model["running"] += value
        elif metric == "num_requests_waiting":
            model["waiting"] += value
        elif metric == "kv_cache_usage_perc":
            model["kv"].append(value)
        elif metric == "gpu_cache_usage_perc":
            model["kv_old"].append(value)
        else:
            histogram = histograms.setdefault((model_name, kind), {"buckets": {}, "count": 0.0, "sum": 0.0})
            if part == "bucket":
                _add_bucket(histogram["buckets"], labels, value)
            else:
                histogram[part] += value

    sample = LatencySample(at=at, histograms={key: Histogram(**h) for key, h in histograms.items()})
    recent = recent_histograms(sample, previous) or {}
    rows = []
    for model_name, m in sorted(models.items()):
        kv = m["kv"] or m["kv_old"]
        # 이 모델의 히스토그램이 직전 수집에도 있었을 때만 구간이 있다 — 새로 나타난 모델은 다음 수집부터
        mine = {kind: histogram for (model, kind), histogram in recent.items() if model == model_name}

        def p95(*kinds: str) -> float | None:
            histogram = next((mine[kind] for kind in kinds if kind in mine), None)
            return None if histogram is None else _ms(histogram_quantile(0.95, histogram.buckets))

        rows.append({
            "model": model_name,
            "pod": pod,
            "node": node,
            "running": round(m["running"]),
            "waiting": round(m["waiting"]),
            "kv_cache_pct": round(sum(kv) / len(kv) * 100, 1) if kv else 0,
            "ttft_p95_ms": p95("ttft"),
            "itl_p95_ms": p95("itl", "itl_old"),
            "latency_window_seconds": round(at - previous.at, 1) if mine and previous is not None else None,
        })
    return rows, sample


def recent_histograms(current: LatencySample, previous: LatencySample | None) -> dict[tuple[str, str], Histogram] | None:
    """두 수집 사이에 쌓인 히스토그램 — current 에 있고 previous 에도 있는 (모델, 종류)만. 기억이 없으면 None.

    어느 하나라도 줄었으면(버킷·count·sum — 서빙 프로세스가 다시 떠 처음부터 센다) 파드 전체가 None 이다: 히스토그램은
    한 프로세스의 것이라 하나만 줄어도 나머지도 옛 기억과 이어지지 않는다.
    le 집합이 달라진 키(버킷 설정이 다른 vLLM 으로 바뀜)는 잇지 않고 건너뛴다 — 직전에 없던 le 를 0 부터 빼면 차이 버킷이
    누적 순서를 어겨(위 le 보다 커져) p95 가 실제보다 낮게 나온다 (agent#137 리뷰). 엔진이 늘어 같은 le 의 합만 뛴 것은
    그만큼 요청이 있었던 것으로 본다.
    """
    if previous is None:
        return None
    recent: dict[tuple[str, str], Histogram] = {}
    for key, now in current.histograms.items():
        before = previous.histograms.get(key)
        if before is None or set(before.buckets) != set(now.buckets):
            continue
        if now.count < before.count or now.sum < before.sum:
            return None
        buckets = {}
        for le, value in now.buckets.items():
            buckets[le] = value - before.buckets[le]
            if buckets[le] < 0:
                return None
        recent[key] = Histogram(buckets, now.count - before.count, now.sum - before.sum)
    return recent


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

def build(nodes: list[dict[str, Any]], serving: list[dict[str, Any]],
          waiting: list[dict[str, Any]] | None = None,
          served_models: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """ClusterGpu. GPU 노드도 GPU 를 기다리는 파드도 없고 ServedModel 도 못 읽었으면 None — 앱은 "GPU 노드가 없어요" 를 띄운다.
    노드는 없고 기다리는 파드나 읽어 낸 ServedModel(빈 배열 포함)이 있으면 꺼진 GPU 클러스터다(nodes 빈 배열).
    served_models 의 null 은 "못 읽었다", 빈 배열은 "없다" 다 — 모듈 설명 "ServedModel"."""
    if not nodes and not waiting and served_models is None:
        return None
    return {"nodes": nodes, "serving": serving, "benchmarks": [], "waiting": waiting or [],
            "served_models": served_models}


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


def _strict_items(stdout: str) -> list[dict[str, Any]]:
    """_items 와 달리 items 칸이 없거나 객체가 아닌 항목이 섞여도 ValueError 다 — 빈 목록이 "없다" 로 읽히는 목록에 쓴다."""
    data = json.loads(stdout or "")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("kubectl 목록 모양이 아니다")
    return items


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_int(value: Any) -> int:
    """자원 수량·라벨 숫자. 정수가 아니거나 0 이하면 0 (`500m` 같은 소수 수량은 GPU 에 없다)."""
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
