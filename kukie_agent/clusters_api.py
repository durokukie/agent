"""클러스터 연결 확인 엔드포인트 (기획 04 §8, issue #61).

  POST /clusters/{id}/test       연결 확인 (kubectl version + auth can-i)
  GET  /clusters/{id}/overview   개요 — 웹 대시보드의 클러스터 지도·통계 (issue #102, kubectl get × 3 + CNI 조회 1 + GPU 조회)

등록·목록·수정·삭제는 Kukie API 서버(kukie-server)의 공개 API 다 (DURO-113) — kubeconfig 검사와 자격증명
암호화도 그쪽에 있다. 연결 확인만 agent 에 남은 이유는 kubectl 이 여기서 돌기 때문이다.

권한은 server 가 본다. 연결 정보를 사용자 토큰으로 받으므로(clusters/access.py) 못 보는 클러스터는 그 호출이
404 를 준다. 팀 구성원도 부를 수 있고, 결과(status·last_checked_at)를 `PATCH /internal/clusters/{id}/status` 로
남긴다 — 연결 확인의 부산물이라 "쓰기는 Admin" 경계에서 일부러 빼 뒀다 (자동 리뷰 지적).
GPU 노드 유무(hasGpu)도 같은 까닭으로 연결 확인·개요가 노드 목록을 읽었을 때 `PATCH /internal/clusters/{id}/gpu` 로
남긴다 (DURO-118). 앱은 클러스터 목록의 그 값으로 GPU 아이콘을 띄운다 — 개요를 부르지 않고도.

**자격증명은 어떤 응답에도 실리지 않는다** (기획 04 §4).
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException

import kukie_agent.server as _server   # 순환 import: 이름은 호출 시점에만 쓴다
from kukie_agent import server_api
from kukie_agent.auth import User, current_user
from kukie_agent.clusters import access, gpu, overview
from kukie_agent.kubectl import KubectlResult, run_kubectl, thread_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["clusters"])

#: 공용 워커 풀을 통째로 물지 않도록 이 모듈이 쓰는 스레드 수를 따로 묶는다 (자동 리뷰 지적, 이유는 thread_limiter).
#: kubectl 은 30초씩 잡는다.
PROBE_WORKERS = 4            # 연결 확인 (kubectl × 3 — version·can-i 30초씩 + 노드 OVERVIEW_TIMEOUT, 최대 75초)
OVERVIEW_WORKERS = 4         # 개요 (kubectl × 5, 노드를 읽고 GPU 가 없다고 이미 안 클러스터가 아니면 GPU_BUDGET 안에서 몇 번 더). 대시보드가 띄울 때마다 부른다 — 연결 확인과 따로 묶는다
#: 개요의 kubectl 한 번 시간 제한(초). 기본 30초 × 4 면 한 요청이 슬롯을 2분 잡는다 — 닿지 않으면 첫 번째에서 15초로 끝난다 (자동 리뷰 지적)
OVERVIEW_TIMEOUT = 15
#: GPU 지표(/metrics 파드 프록시) 한 번 시간 제한(초). 파드마다 한 번씩이라 개요보다 더 짧게 끊는다
GPU_METRICS_TIMEOUT = 5
#: 모든 namespace 파드를 못 읽을 때 namespace 하나의 파드 목록 시간 제한(초). namespace 마다 한 번이라 지표처럼 짧게 끊는다
GPU_PODS_TIMEOUT = 5
#: 파드를 읽을 수 있었던 namespace 를 연결마다 기억하는 시간(초). 그동안은 그 namespace 만 읽는다.
#: 고객이 새 namespace 나 RoleBinding 을 더하면 이만큼 늦게 보인다 — 짧게 둔다
READABLE_NAMESPACES_TTL = 120
#: GPU 칸(노드·파드·지표 전부)에 쓰는 시간(초). 지표 파드가 16개 다 매달려도 개요 슬롯을 이 이상 더 잡지 않는다 (PR #118 리뷰).
#: 다 쓰면 남은 조회는 건너뛰고 그 값만 null 이다
GPU_BUDGET = 20
#: 지표를 동시에 읽는 수. 개요 슬롯(OVERVIEW_WORKERS) 하나 안에서 잠깐 쓰는 스레드라 limiter 밖이다 — 많아야 4 × 4
GPU_METRICS_WORKERS = 4
#: server 에 GPU 가 없다고 남은 클러스터도 이 간격(초)마다 한 번은 GPU 를 기다리는 파드를 다시 찾는다 — 노드 그룹을 0대로 둔 채
#: vLLM 같은 GPU 워크로드를 새로 올리면 노드가 뜨기 전에도 GPU 클러스터로 바뀌게 (agent#121 Codex 리뷰). 그 사이에는 찾지 않는다.
#: 프로세스 안 시각이라 복제본마다·재시작마다 처음 본 때부터 센다
GPU_ABSENT_RECHECK = 600


@router.post("/{cluster_id}/test")
async def test_cluster(
    cluster_id: str,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """연결 확인 (기획 04 §8). 서버 버전을 읽고 변경 권한이 있는지 물어본다."""
    conn = await access.connection(cluster_id, user)

    def probe() -> tuple[Any, Any, bool | None]:
        with access.kubeconfig(conn) as path:
            version = run_kubectl(["version", "-o", "json"], context=conn.context_name, kubeconfig=path)
            can_edit = run_kubectl(
                ["auth", "can-i", "update", "deployments", "-n", conn.namespace],
                context=conn.context_name, kubeconfig=path,
            )
            has_gpu = None
            if version.success:
                # 노드 목록만 본다 (지표는 개요의 일). 고객 EKS 는 Forbidden 이라 None — 남기지 않는다
                nodes = run_kubectl(gpu.NODES_COMMAND, context=conn.context_name, kubeconfig=path,
                                    timeout=OVERVIEW_TIMEOUT)
                has_gpu = _has_gpu(nodes)
                # GPU 노드가 없어도 0대로 내려 둔 GPU 클러스터일 수 있다 — GPU 를 기다리는 파드를 보는 개요가 정한다.
                # 없다고 이미 안 클러스터만 그대로 false
                if has_gpu is False and conn.has_gpu is not False:
                    has_gpu = None
            return version, can_edit, has_gpu

    # run_kubectl 은 subprocess.run 을 그대로 부른다. 여기서 직접 부르면 닿지 않는 주소일 때
    # 최대 75초(version + can-i + 노드) 동안 서버 전체가 멈춘다 (자동 리뷰 지적). 스레드로 넘긴다.
    version, can_edit, has_gpu = await to_thread.run_sync(probe, limiter=thread_limiter("probe", PROBE_WORKERS))
    status = "connected" if version.success else _failure_status(version.stderr)
    await server_api.update_cluster_status(cluster_id, status=status, checked_at=datetime.now(timezone.utc))
    recorded = await _record_gpu(conn, has_gpu)
    return {
        "status": status,
        "reachable": version.success,
        # 실패 사유는 그대로 보여줘야 고칠 수 있다. 자격증명은 원래 stderr 에 실리지 않는다
        "detail": (version.stderr or version.stdout).strip()[:500],
        "can_change": can_edit.success and can_edit.stdout.strip() == "yes",
        # 노드 목록으로 본 GPU 클러스터 여부. 못 읽었거나 server 에 남기지 못했으면 null — 앱은 이 값이 목록 값과 다를 때
        # 클러스터 목록을 다시 받으므로, 못 남긴 값을 주면 다시 받아도 아이콘이 안 바뀐다 (PR #119 리뷰).
        # GPU 노드가 없어도 0대로 내려 둔 GPU 클러스터일 수 있어, server 값이 false 가 아니면 null 이다(개요가 정한다)
        "has_gpu": has_gpu if recorded else None,
    }


@router.get("/{cluster_id}/overview")
async def cluster_overview(
    cluster_id: str,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """개요 (issue #102). 클러스터 namespace 의 워크로드·파드·최근 이벤트를 읽는다 — 응답 모양과 판정은 clusters/overview.py.

    권한·자격증명은 연결 확인과 같은 길이다 — 못 보는 클러스터는 server 의 404, 팀 구성원 누구나 부른다.
    읽기 전용이고 namespace 범위(`-n`)로만 조회한다. 상태(status)는 남기지 않는다 — 그건 연결 확인의 일이다.
    CNI(network)는 세 조회가 다 된 뒤 kube-system DaemonSet 을 한 번 더 읽어 알아본다 (overview.NETWORK_COMMAND).
    그 조회는 실패해도(Forbidden·시간 초과) 개요를 실패로 만들지 않고 network 만 null 로 둔다.
    GPU(gpu)는 그 뒤에 노드 → 모든 파드 → 지표 순으로 읽는다 (clusters/gpu.py). 노드를 못 읽으면 gpu 만 null 이고 그 뒤는
    읽지 않는다. GPU 노드가 없으면 GPU 를 기다리는 파드를 찾는다 — 있으면 nodes 가 빈 gpu 칸(꺼진 GPU 클러스터), 없으면 null.
    server 에 GPU 가 없다고 남은 클러스터는 이 파드를 GPU_ABSENT_RECHECK 초에 한 번만 찾는다. 알아낸 GPU 클러스터 여부를
    server 에 남긴다(_record_gpu).

    kubectl 이 실패하면 연결 확인과 같은 기준(_failure_status)으로 갈라 502 로 답한다.
      CLUSTER_AUTH_EXPIRED  인증 실패·권한 없음 (연결 확인의 auth_expired)
      CLUSTER_UNREACHABLE   그 밖의 실패 — 닿지 않음·시간 초과 (연결 확인의 disconnected)
      CLUSTER_BAD_RESPONSE  kubectl 은 성공했는데 JSON 으로 읽지 못했다
    401 이 아니라 502 인 이유: 앱은 401 을 사용자 토큰 만료로 읽고 로그아웃한다. 클러스터 자격증명 문제는 그게 아니다.
    """
    conn = await access.connection(cluster_id, user)

    def collect() -> tuple[dict[str, Any], bool | None]:
        outputs: dict[str, str] = {}
        with access.kubeconfig(conn) as path:
            for name, args in overview.READ_COMMANDS.items():
                result = run_kubectl([*args, "-n", conn.namespace], context=conn.context_name,
                                     kubeconfig=path, timeout=OVERVIEW_TIMEOUT)
                if not result.success:
                    _raise_kubectl_failure(result.stderr or result.stdout)   # 첫 실패에서 멈춘다
                outputs[name] = result.stdout
            # 앞의 세 조회가 다 된 뒤에만 돈다 — 닿지 않는 클러스터에서 시간을 더 쓰지 않는다
            network = run_kubectl(overview.NETWORK_COMMAND, context=conn.context_name,
                                  kubeconfig=path, timeout=OVERVIEW_TIMEOUT)

            def run(args: list[str], timeout: int) -> KubectlResult:
                return run_kubectl(args, context=conn.context_name, kubeconfig=path, timeout=timeout)

            cluster_gpu, has_gpu = _collect_gpu(run, namespace_pods=outputs["pods"], namespace=conn.namespace,
                                                 cache_key=(f"{cluster_id}:{conn.fingerprint}:{conn.namespace}"
                                                            if conn.fingerprint else None),   # 지문이 없으면 연결이 바뀐 걸 모른다
                                                 known=conn.has_gpu)
        if not network.success:
            # 고객 클러스터는 kube-system 을 못 읽는 게 보통이라 대시보드가 부를 때마다 난다 — debug 로 둔다 (PR #117 리뷰).
            # 원문(사용자·ARN 이 실린다)은 남기지 않고 갈래만 남긴다
            logger.debug("개요의 CNI 조회를 건너뛴다 (kube-system DaemonSet): %s",
                         _failure_status(network.stderr or network.stdout))
        try:
            return overview.build(context=conn.context_name, namespace=conn.namespace,
                                  now=datetime.now(timezone.utc),
                                  network=network.stdout if network.success else None, gpu=cluster_gpu,
                                  **outputs), has_gpu
        except ValueError as exc:
            # JSON 이 아니거나 items 가 목록이 아닐 때만 (json.JSONDecodeError 도 ValueError). 우리 코드의 버그는 500 으로 둔다.
            # 원문은 클러스터 내용이라 남기지 않고 예외 종류·위치만 남긴다
            logger.warning("개요의 kubectl 출력을 읽지 못했다: %s", type(exc).__name__, exc_info=True)
            raise HTTPException(502, {"code": "CLUSTER_BAD_RESPONSE",
                                      "message": f"kubectl 출력을 읽지 못했다: {type(exc).__name__}"}) from None

    # 연결 확인과 같은 이유로 스레드로 넘긴다 — kubectl 이 도는 동안 서버 전체가 멈추지 않게
    body, has_gpu = await to_thread.run_sync(collect, limiter=thread_limiter("overview", OVERVIEW_WORKERS))
    await _record_gpu(conn, has_gpu)
    return body


def _collect_gpu(
    run, *, namespace_pods: str, namespace: str | None = None, cache_key: str | None = None,
    known: bool | None = None, clock=time.monotonic,
) -> tuple[dict[str, Any] | None, bool | None]:
    """(개요의 gpu 칸, GPU 클러스터인가) — clusters/gpu.py 의 "읽는 순서". 어느 단계가 실패해도 오류로 올리지 않는다.

    GPU 노드가 있거나, 노드는 없어도 GPU 를 기다리는 파드가 있으면(0대로 내려 둔 GPU 클러스터) True 다.
    노드 목록을 못 읽었으면 None(모른다)이다. 노드가 없을 때 False 는 파드를 끝까지 읽어 기다리는 파드가 없다고 확인했을
    때만이다 — 모든 namespace 파드를 읽었거나, namespace 마다 읽은 것이 전부 성공·거부로 끝났을 때. 시간 초과·예산 부족·
    파드 목록 파싱 실패로 못 본 곳이 있으면 None 이다(꺼진 GPU 클러스터를 한 번의 실패로 "GPU 없음" 으로 덮지 않게).
    known 은 server 에 남은 값이다. False(GPU 가 없다고 이미 안 클러스터)면 노드가 없을 때 파드를 찾지 않는다 — 다만
    GPU_ABSENT_RECHECK 초마다 한 번은 다시 찾는다(_absent_recheck_due).

    run(args, timeout) 은 개요와 같은 임시 kubeconfig 로 kubectl 을 돌린다. 실패 원문(사용자·ARN 이 실린다)은
    남기지 않고 갈래만 debug 로 남긴다 — 고객 클러스터는 노드를 못 읽는 게 보통이라 대시보드가 부를 때마다 난다.
    전체를 GPU_BUDGET 초 안에 끝낸다 — 조회마다 남은 시간만큼만 기다리고, 1초도 안 남으면 건너뛴다.
    """
    deadline = clock() + GPU_BUDGET

    def budget(limit: int) -> int:
        return min(limit, int(deadline - clock()))

    nodes_result = run(gpu.NODES_COMMAND, budget(OVERVIEW_TIMEOUT))
    if not nodes_result.success:
        logger.debug("개요의 GPU 조회를 건너뛴다 (노드): %s", _failure_status(nodes_result.stderr or nodes_result.stdout))
        return None, None
    try:
        nodes = gpu.gpu_nodes(nodes_result.stdout)
    except (ValueError, AttributeError, TypeError):
        logger.debug("개요의 GPU 조회를 건너뛴다 (노드 목록을 읽지 못했다)")
        return None, None
    if not nodes and known is False and not _absent_recheck_due(cache_key, clock):
        return None, False

    if budget(OVERVIEW_TIMEOUT) < 1:
        return gpu.build(nodes, []), (True if nodes else None)
    gpu_node_names = {row["node"] for row in nodes}
    pods_result = run(gpu.PODS_COMMAND, budget(OVERVIEW_TIMEOUT))
    if pods_result.success:
        pods_stdout, pods_complete = pods_result.stdout, True
    else:
        logger.debug("GPU 지표 파드를 namespace 마다 찾는다: %s", _failure_status(pods_result.stderr or pods_result.stdout))
        # 탐색은 남은 예산의 절반까지만 — 지표를 읽을 시간을 남긴다 (agent#120 리뷰). GPU 노드가 없으면 지표를 읽지 않으니
        # 다 쓴다 — 기다리는 파드를 찾는 길이 이것뿐이다 (agent#121 리뷰)
        search_deadline = clock() + max(0.0, deadline - clock()) / 2 if nodes else deadline

        def search_budget(limit: int) -> int:
            return min(limit, int(search_deadline - clock()))

        pods_stdout, pods_complete = _pods_by_namespace(
            run, search_budget, namespace_pods, namespace=namespace, gpu_node_names=gpu_node_names,
            cache_key=cache_key, clock=clock)
    try:
        pods = gpu.running_pods(pods_stdout)
        dcgm = gpu.dcgm_targets(pods, gpu_node_names)
        vllm = gpu.vllm_targets(pods, gpu_node_names)
    except (ValueError, AttributeError, TypeError):
        logger.debug("GPU 지표 파드를 찾지 못했다 (파드 목록을 읽지 못했다)")
        dcgm, vllm = {}, []
    waiting: list[dict[str, Any]] | None
    try:
        waiting = gpu.waiting_pods(pods_stdout)
    except (ValueError, AttributeError, TypeError):
        logger.debug("GPU 를 기다리는 파드를 찾지 못했다 (파드 목록을 읽지 못했다)")
        waiting = None                                      # 못 읽은 것과 "없다" 는 다르다 — 아래에서 None(모른다)
    if not nodes:
        if waiting:
            return gpu.build([], [], waiting), True
        return None, (False if waiting is not None and pods_complete else None)

    def metrics(target: tuple[str, str, int]) -> str | None:
        namespace, pod, port = target
        timeout = budget(GPU_METRICS_TIMEOUT)
        if timeout < 1:
            return None
        result = run(gpu.proxy_command(namespace, pod, port), timeout)
        if not result.success:
            logger.debug("GPU 지표를 읽지 못했다 (%s/%s): %s", namespace, pod,
                         _failure_status(result.stderr or result.stdout))
            return None
        return result.stdout

    dcgm_rows = [row for row in nodes if row["node"] in dcgm]
    targets = [dcgm[row["node"]] for row in dcgm_rows] + [(ns, pod, port) for ns, pod, port, _ in vllm]
    with ThreadPoolExecutor(max_workers=GPU_METRICS_WORKERS) as pool:
        texts = list(pool.map(metrics, targets))
    for row, text in zip(dcgm_rows, texts):
        if text is not None:
            gpu.apply_dcgm(row, text)
    serving: list[dict[str, Any]] = []
    for (_, pod, _, node), text in zip(vllm, texts[len(dcgm_rows):]):
        if text is not None:
            serving.extend(gpu.serving_rows(text, pod=pod, node=node))
    return gpu.build(nodes, serving, waiting or []), True


@dataclass(frozen=True)
class _NamespaceMemo:
    """연결 하나의 namespace 기억 — _pods_by_namespace."""
    at: float                          # 목록을 마지막으로 읽어 본 시각
    names: tuple[str, ...]             # 다시 읽을 namespace (Forbidden 은 빠진다)
    productive: frozenset[str]         # GPU 지표 파드(DCGM·vLLM)나, GPU 노드 0대 라운드의 기다리는 파드가 나온 namespace
    cursors: tuple[int, int] = (0, 0)  # 앞 줄·뒤 줄에서 다음에 읽기 시작할 자리
    whole: bool = True                 # names 가 목록 전부다 — MAX_POD_NAMESPACES 로 자르지 않았다


#: 연결(클러스터 id·연결 지문·클러스터 namespace)마다의 기억. 프로세스 안 기억이라 복제본마다 따로 채운다.
#: 연결을 바꾸면 키가 달라져 옛 기억을 쓰지 않는다
_readable_namespaces: dict[str, _NamespaceMemo] = {}
_readable_lock = threading.Lock()
#: 연결마다 GPU 없는 클러스터를 마지막으로 다시 찾은(또는 처음 본) 시각 — _absent_recheck_due
_gpu_absent_checked: dict[str, float] = {}


def _absent_recheck_due(cache_key: str | None, clock=time.monotonic) -> bool:
    """GPU 가 없다고 남은 연결을 지금 다시 찾을 때인가. 처음 본 때부터 GPU_ABSENT_RECHECK 초마다 한 번 True.
    연결 지문이 없으면(cache_key None) 다시 찾지 않는다 — 연결이 바뀐 걸 모르는 클러스터라 지금처럼 멈춘다."""
    if cache_key is None:
        return False
    now = clock()
    with _readable_lock:
        for key in [key for key, at in _gpu_absent_checked.items()
                    if key != cache_key and now - at >= 2 * GPU_ABSENT_RECHECK]:
            del _gpu_absent_checked[key]
        last = _gpu_absent_checked.setdefault(cache_key, now)
        if now - last < GPU_ABSENT_RECHECK:
            return False
        _gpu_absent_checked[cache_key] = now
        return True


def _rotate(names: list[str], start: int) -> list[str]:
    if not names:
        return names
    start %= len(names)
    return names[start:] + names[:start]


def _pods_by_namespace(run, budget, namespace_pods: str, *, namespace: str | None, gpu_node_names: set[str],
                       cache_key: str | None, clock=time.monotonic) -> tuple[str, bool]:
    """(파드 목록, 끝까지 읽었나) — 모든 namespace 파드를 못 읽을 때의 길, clusters/gpu.py "읽는 순서" 2.

    끝까지 읽었다는 것은 namespace 목록 전부를 알았고(TTL 안의 기억도 된다) 그 전부가 성공이나 거부(Forbidden)로 끝났다는
    것이다. 거부된 곳은 Kukie 가 볼 수 없는 곳이라 "못 본 곳" 으로 치지 않는다. 시간 초과·예산 부족이 하나라도 있거나,
    목록이 MAX_POD_NAMESPACES 로 잘렸거나, 목록을 못 읽어 TTL 지난 기억으로 읽었으면 False 다 (agent#121 리뷰).

    namespace 목록을 읽고 namespace 마다 파드를 읽어(동시에, 하나에 GPU_PODS_TIMEOUT 초) 읽힌 것만 개요의 클러스터
    namespace 파드와 합친다. 클러스터 namespace(개요가 이미 읽었다)는 다시 읽지 않는다. 고객이 지표 RoleBinding 을
    걸지 않은 namespace 는 Forbidden 이라 조용히 빠진다. 찾은 것으로 일찍 멈추지 않는다 — vLLM 은 몇 개인지 정해져
    있지 않고, vLLM·DCGM 이 없는 클러스터(학습용, AMD)도 있다.

    거부되는 조회가 대시보드를 부를 때마다 고객 감사 로그에 쌓이지 않게, 연결마다 READABLE_NAMESPACES_TTL 초 기억한다
    (agent#120 리뷰). 그동안은 목록을 다시 읽지 않고 기억한 namespace 만 읽고, Forbidden 으로 확인된 것은 그때그때 뺀다.
    읽는 순서는 두 줄이다 — 앞 줄: GPU 지표 파드(노드 0대 라운드는 기다리는 파드)가 나온 적 있는 것과 GPU 쪽 이름, 뒤 줄: 나머지. 줄마다 지난번에 멈춘
    자리부터 돌아가며 읽고(라운드 로빈), 앞 줄 첫째 다음에 뒤 줄 하나를 끼운다 — 예산이 모자라도 어느 줄도 굶지 않는다.
    목록을 다시 읽다 실패하면 지난 기억으로 읽는다. 연결 지문이 없으면(cache_key None) 기억하지 않는다.
    namespace 목록도 기억도 없으면(GPU 읽기 RBAC 를 적용하지 않은 클러스터) 클러스터 namespace 파드만 쓴다.
    """
    def read(name: str) -> tuple[str | None, str]:
        """(파드 목록, 결과) — 결과는 ok · forbidden · unknown(시간 초과 등) · skipped(예산 부족)."""
        timeout = budget(GPU_PODS_TIMEOUT)
        if timeout < 1:
            return None, "skipped"
        result = run(gpu.namespace_pods_command(name), timeout)
        if result.success:
            return result.stdout, "ok"
        return None, "forbidden" if "forbidden" in (result.stderr or result.stdout or "").lower() else "unknown"

    now = clock()
    with _readable_lock:
        memo = _readable_namespaces.get(cache_key) if cache_key else None
    productive = set(memo.productive) if memo else set()
    names, listed_at = None, memo.at if memo else now
    whole = False                                           # names 가 지금 목록 전부인가 — 끝까지 읽었나의 조건
    if memo and now - memo.at < READABLE_NAMESPACES_TTL:
        names, whole = list(memo.names), memo.whole
    elif budget(OVERVIEW_TIMEOUT) >= 1:
        listed_at = now                                     # 목록을 실제로 읽어 본 라운드만 시각을 새로 한다
        names_result = run(gpu.NAMESPACES_COMMAND, budget(OVERVIEW_TIMEOUT))
        if names_result.success:
            try:
                every = gpu.all_namespace_names(names_result.stdout, skip=namespace, prefer=productive)
                names, whole = every[:gpu.MAX_POD_NAMESPACES], len(every) <= gpu.MAX_POD_NAMESPACES
            except (ValueError, AttributeError, TypeError):
                logger.debug("namespace 목록을 읽지 못했다")
        else:
            logger.debug("namespace 목록을 읽지 못했다: %s", _failure_status(names_result.stderr or names_result.stdout))
    if names is None:
        if not memo:
            return namespace_pods, False
        names = list(memo.names)                            # 목록만 못 읽었으면 지난 기억으로 읽는다

    cursors = memo.cursors if memo else (0, 0)
    front = [name for name in names if name in productive or gpu.hinted_namespace(name)]
    back = [name for name in names if name not in front]
    # 줄마다 돌린 뒤 앞 줄 첫째 다음에 뒤 줄 하나를 끼운다 — 앞 줄이 예산을 다 써도 뒤 줄이 매 라운드 한 칸은 돈다
    first, second = _rotate(front, cursors[0]), _rotate(back, cursors[1])
    order = first[:1] + second[:1] + first[1:] + second[1:]
    with ThreadPoolExecutor(max_workers=GPU_METRICS_WORKERS) as pool:
        results = dict(zip(order, pool.map(read, order)))

    if cache_key:
        tried = [sum(results[name][1] != "skipped" for name in line) for line in (front, back)]
        for name, (stdout, outcome) in results.items():
            if outcome == "ok" and (gpu.has_gpu_targets(stdout, gpu_node_names)
                                    or (not gpu_node_names and gpu.has_waiting_pods(stdout))):
                # GPU 노드가 0대인 라운드는 기다리는 파드가 나온 곳을 기억한다 — 노드가 뜨면 거기서 지표 파드가 돈다
                productive.add(name)
            elif outcome == "forbidden" or (outcome == "ok" and gpu_node_names):
                # 읽었는데 GPU 파드가 없거나 거부 — 한 번 느렸다고 빼지 않는다. GPU 노드가 0대인 라운드(꺼진 GPU
                # 클러스터)는 지표 파드가 없는 게 당연하니 지난 기억을 지우지 않는다
                productive.discard(name)
        keep = tuple(name for name in names if results[name][1] != "forbidden")
        next_memo = _NamespaceMemo(at=listed_at, names=keep, productive=frozenset(productive & set(keep)),
                                   cursors=(cursors[0] + tried[0], cursors[1] + tried[1]), whole=whole)
        with _readable_lock:
            # 지운 클러스터·바뀐 연결의 옛 기억이 쌓이지 않게. TTL 두 배까지는 남겨 목록을 못 읽을 때 쓴다
            for key in [key for key, old in _readable_namespaces.items() if now - old.at >= 2 * READABLE_NAMESPACES_TTL]:
                del _readable_namespaces[key]
            _readable_namespaces[cache_key] = next_memo
    complete = whole and all(outcome in ("ok", "forbidden") for _, outcome in results.values())
    return gpu.merge_pod_lists([namespace_pods, *(stdout for stdout, _ in results.values() if stdout is not None)]), complete


def _has_gpu(nodes: KubectlResult) -> bool | None:
    """연결 확인의 노드 목록으로 본 GPU 노드 유무. 못 읽었으면 None."""
    if not nodes.success:
        return None
    try:
        return bool(gpu.gpu_nodes(nodes.stdout))
    except (ValueError, AttributeError, TypeError):
        return None


async def _record_gpu(conn: server_api.ClusterConnection, has_gpu: bool | None) -> bool:
    """알아낸 GPU 노드 유무가 server 에 남은 값과 다를 때만 남긴다 — 개요는 대시보드가 띄울 때마다 부른다.

    모르면(None) 남기지 않는다 — 권한이 없어진 클러스터의 마지막 값을 지우지 않는다. 읽는 사이 kubeconfig 가 바뀌었으면
    server 가 지문으로 가려 409 CLUSTER_CHANGED 다 (kukie-server#82 리뷰). 남기지 못해도 오류로 올리지 않는다:
    부가 정보라 개요·연결 확인을 실패로 만들 까닭이 없고, 다음 호출이 다시 남긴다.
    server 의 값이 이제 has_gpu 와 같은가(이미 같았거나 남겼다)를 준다.
    """
    if has_gpu is None:
        return False
    if has_gpu == conn.has_gpu:
        return True
    if not conn.fingerprint:
        # server 가 늘 409 로 거절할 호출이다 — 부르지 않되 동기화가 멈춘 것은 남긴다
        logger.warning("클러스터 %s 의 GPU 여부를 남기지 못했다: 연결 정보에 지문이 없다", conn.cluster_id)
        return False
    try:
        await server_api.update_cluster_gpu(conn.cluster_id, has_gpu=has_gpu, fingerprint=conn.fingerprint)
    except server_api.ServerError as exc:
        logger.warning("클러스터 %s 의 GPU 여부를 남기지 못했다: %s", conn.cluster_id, exc.code)
        return False
    return True


def _raise_kubectl_failure(stderr: str) -> None:
    # 실패 사유는 그대로 보여줘야 고칠 수 있다 (연결 확인의 detail 과 같다). 자격증명은 원래 stderr 에 실리지 않는다
    reason = stderr.strip()[:500]
    if _failure_status(stderr) == "auth_expired":
        raise HTTPException(502, {"code": "CLUSTER_AUTH_EXPIRED",
                                  "message": f"클러스터 인증에 실패했거나 권한이 없다: {reason}"})
    raise HTTPException(502, {"code": "CLUSTER_UNREACHABLE", "message": f"클러스터에 연결할 수 없다: {reason}"})


def _failure_status(stderr: str) -> str:
    lowered = stderr.lower()
    if "unauthorized" in lowered or "forbidden" in lowered or "credential" in lowered:
        return "auth_expired"
    return "disconnected"


_server.app.include_router(router)

__all__ = ["router"]
