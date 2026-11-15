"""PR 머지 뒤 — sync·health 지켜보기를 요청과 떼어 돌리고, 앞 프로세스가 끝내지 못한 계획을 닫는다 (DURO-158).

머지는 gitops_hook._after_merge 가 sync_result.status=syncing 과 함께 먼저 Plan 에 남긴다(APPLIED). 그 뒤의 일이 여기다.

지켜보기
    프로세스 안의 작업 하나가 watch.watch 를 돌려 결과를 Plan 에 얹는다 (server 의 sync-result). 어떻게 끝나든 syncing 을 벗어난다 —
    시간 초과(timeout), 못 읽음(unreadable), 멈춤(interrupted). 요청의 임시 kubeconfig 는 요청과 함께 지워지므로 같은 연결 정보로 자기 파일을 만든다.

재시작 복구
    프로세스가 죽으면 지켜보기도, 메모리의 승인 티켓도 사라진다. 기동할 때 server 에 끝나지 않은 PR 길 계획을 물어 닫는다.
      - 머지 기록 있음(syncing) → interrupted. 사용자 토큰이 없어 클러스터 연결 정보를 다시 받을 수 없어 이어 지켜볼 수 없다
      - 머지 기록 없음(APPROVED·EXECUTING) → GitHub 에 묻는다. 머지됐으면 그 사실을 남기고, 안 됐으면 FAILED(PR 은 남김),
        알 수 없으면 UNKNOWN. run 이 열려 있으면 닫는다 — 이어 갈 티켓이 없다. 응답 칸은 그대로 둬 카드가 기록에 남아 결과를 보인다
    server 가 아직 안 떴을 수 있어 뒤에서 몇 번 다시 시도한다. 그동안 이 프로세스가 시작한 계획은 건드리지 않는다 (claim).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import nullcontext
from functools import partial
from typing import Any

from anyio import to_thread

from kukie_agent import server_api
from kukie_agent.clusters import access
from kukie_agent.gitops import pull_requests, watch
from kukie_agent.gitops.github import GitHubError
from kukie_agent.guardrail.action_plan import ActionPlan
from kukie_agent.kubectl import thread_limiter
from kukie_agent.tools.gitops import TOOL_NAME

logger = logging.getLogger(__name__)

#: 지켜보기 동시 상한 — 대부분 잠들어 있지만 조회마다 kubectl 을 띄운다. 하나가 SYNC_WAIT(10분)까지 자리를 쥘 수 있어 넉넉히 둔다
WATCH_WORKERS = 32
#: sync 결과를 남기는 재시도 사이 시간(초)
SAVE_RETRY_SECONDS = (1, 5, 15, 30, 60)
#: 재시작 복구의 재시도 사이 시간(초) — server 가 같이 재시작하는 중일 수 있다
RECOVERY_RETRY_SECONDS = (5, 15, 30, 60, 120, 300)
#: 끌 때 떼어 돌던 승인·지켜보기가 끝나기를 기다리는 시간(초). 넘으면 취소한다 — 다음 기동의 복구가 닫는다.
#: 취소를 기다리는 시간과 합쳐 컨테이너 종료 유예(기본 10초)보다 짧아야 한다
STOP_WAIT_SECONDS = 7.0
#: 취소한 작업이 내려가기를 기다리는 시간(초). 도는 kubectl 은 취소되지 않아 끝까지 기다리지 않는다
CANCEL_WAIT_SECONDS = 1.0

_tasks: set[asyncio.Task[None]] = set()
_approvals: set[asyncio.Task[Any]] = set()
_stopping = threading.Event()
_recovering = False
_claimed: set[str] = set()


class _Stopped(Exception):
    """프로세스를 끈다 — 지켜보기를 그만둔다."""


def _sleep(seconds: float) -> None:
    """watch 의 잠. 끄는 신호가 오면 깨어나 그만둔다 — 그냥 재우면 스레드가 끝날 때까지 프로세스가 내려가지 못한다."""
    if _stopping.wait(seconds):
        raise _Stopped


def _keep(task: asyncio.Task[None]) -> None:
    _tasks.add(task)                    # asyncio 는 약한 참조만 든다
    task.add_done_callback(_tasks.discard)


def keep_approval(task: asyncio.Task[Any]) -> None:
    """응답이 먼저 나가고 뒤에서 마저 도는 승인을 맡아 둔다 — 끌 때 server 연결을 닫기 전에 정리한다 (stop)."""
    _approvals.add(task)
    task.add_done_callback(_approvals.discard)


def claim(plan_id: str) -> None:
    """이 프로세스가 실행하기 시작한 계획이다 — 복구가 앞 프로세스의 것으로 보고 닫지 않게 한다."""
    if _recovering:
        _claimed.add(plan_id)


def start_watch(
    plan: ActionPlan, application: dict[str, Any], merge_commit: str, *, context: str,
    connection: server_api.ClusterConnection | None,
) -> None:
    """뒤에서 지켜보기를 시작한다. 돌고 있는 이벤트 루프 안에서 부른다."""
    _keep(asyncio.create_task(_watch_and_save(plan, application, merge_commit, context, connection)))


async def _watch_and_save(
    plan: ActionPlan, application: dict[str, Any], merge_commit: str, context: str,
    connection: server_api.ClusterConnection | None,
) -> None:
    try:
        # 연결 정보가 없으면 flat 엔드포인트(로컬 개발) — 서버 컴퓨터의 기본 kubeconfig 를 쓴다
        with access.kubeconfig(connection) if connection is not None else nullcontext() as kubeconfig:
            sync = await to_thread.run_sync(partial(
                watch.watch, application.get("name", ""), application.get("namespace", ""), merge_commit,
                auto_sync=True, context=context, kubeconfig=kubeconfig, sleep=_sleep,
            ), limiter=thread_limiter("gitops_watch", WATCH_WORKERS))
    except _Stopped:
        sync = watch.interrupted()
    except Exception:
        logger.exception("sync 지켜보기 실패: plan_id=%s", plan.id)
        sync = watch.unreadable()
    await _save(plan, sync)


async def _save(plan: ActionPlan, sync: dict[str, Any]) -> None:
    """못 남기면 몇 번 더 — 그래도 안 되면 syncing 으로 남고 다음 기동의 복구가 닫는다."""
    for delay in (*SAVE_RETRY_SECONDS, None):
        try:
            await plan.record_sync(sync)
            return
        except ValueError:
            logger.warning("sync 결과를 받지 않았다 — 이미 닫혔다: plan_id=%s", plan.id)     # 복구가 먼저 닫은 경우. 다시 해도 같다
            return
        except Exception:
            logger.exception("sync 결과를 남기지 못했다: plan_id=%s", plan.id)
        if delay is not None and not _stopping.is_set():
            await asyncio.sleep(delay)


async def stop() -> None:
    """끌 때. 잠든 지켜보기를 깨워 interrupted 로 남긴다. 복구 재시도는 그냥 멈춘다 — 다음 기동이 다시 한다.

    떼어 돌던 승인이 먼저다 — 머지 기록과 답을 남겨야 하고, 끝나면서 지켜보기를 새로 시작할 수 있다. 이 뒤에 server 연결이 닫힌다.
    시간 안에 못 끝난 승인은 취소한다: run 은 기동 때 interrupt-orphaned 가, Plan 은 복구가 닫는다.
    """
    _stopping.set()
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    await _drain(_approvals, deadline)
    await _drain(_tasks, deadline)


async def _drain(tasks: set[Any], deadline: float) -> None:
    if tasks:
        await asyncio.wait(set(tasks), timeout=max(deadline - time.monotonic(), 0))
    left = set(tasks)
    for task in left:
        task.cancel()
    if left:
        await asyncio.wait(left, timeout=CANCEL_WAIT_SECONDS)


def start() -> None:
    """기동할 때 부른다 (server.py lifespan)."""
    _stopping.clear()


def start_recovery() -> None:
    """server 가 있을 때 기동하면서 부른다. 요청을 받기 전에 표시해 둬 그 뒤의 실행은 claim 으로 가려진다."""
    global _recovering
    _recovering = True
    _keep(asyncio.create_task(_recover_with_retries()))


async def _recover_with_retries() -> None:
    global _recovering
    runs: set[str] = set()      # 계획은 닫혔는데 run 을 못 닫은 것도 다음 차례에 이어 닫는다 — 닫힌 계획은 목록에 다시 나오지 않는다
    try:
        for delay in (*RECOVERY_RETRY_SECONDS, None):
            try:
                await recover(final=delay is None, runs=runs)
                return
            except Exception:
                logger.exception("앞 프로세스의 PR 계획을 닫지 못했다 — %s초 뒤 다시", delay)
            if delay is not None:
                await asyncio.sleep(delay)
    finally:
        _recovering = False
        _claimed.clear()


async def recover(*, final: bool = True, runs: set[str] | None = None) -> None:
    """앞 프로세스가 끝내지 못한 PR 길 계획을 닫는다. 하나라도 못 닫으면 나머지를 마친 뒤 던진다 — 닫힌 계획은 다음 번 목록에 없다.

    runs 는 닫아야 할 run 을 담아 다시 시도에 넘기는 칸이다. 닫은 것은 빠지고 못 닫은 것은 남는다."""
    failed: Exception | None = None
    runs = set() if runs is None else runs
    for item in await server_api.unsettled_plans(TOOL_NAME):
        plan = ActionPlan.from_response(item.plan)
        if plan.id in _claimed:
            continue
        try:
            if plan.execution_result is not None:
                await plan.record_sync(watch.interrupted())
            else:
                await _settle_unrecorded(plan, item.cluster_id, final=final)
        except Exception as exc:
            logger.exception("앞 프로세스의 PR 계획을 닫지 못했다: plan_id=%s", plan.id)
            failed = exc
            continue
        logger.warning("앞 프로세스가 끝내지 못한 PR 계획을 닫았다: plan_id=%s status=%s", plan.id, plan.status)
        if item.run_active:
            runs.add(plan.run_id)
    for run_id in set(runs):
        try:
            await server_api.update_run(run_id, status="interrupted")
        except server_api.ServerError as exc:
            if exc.status_code != 409:      # 409: 방을 먼저 연 요청이 이미 닫았다
                failed = exc
                continue
        runs.discard(run_id)
    if failed is not None:
        raise failed


async def _settle_unrecorded(plan: ActionPlan, cluster_id: str, *, final: bool) -> None:
    """승인은 남았는데 결과가 없다 — 머지 요청과 기록 사이에 죽었을 수 있다. GitHub 만 안다."""
    gitops = plan.gitops or {}
    pull_request = plan.pull_request or {}
    try:
        token = await pull_requests.token_for(cluster_id, gitops["repository"])
        checked = await pull_requests.recheck(gitops, pull_request, token)
    except (pull_requests.Refuse, KeyError, ValueError):
        # 레포 연결이 끊겼거나 칸이 어긋났다 — 다시 해도 알 수 없다
        logger.exception("머지 여부를 볼 수 없다: plan_id=%s", plan.id)
        await plan.mark_unknown()
        return
    except (GitHubError, server_api.ServerError):
        if not final:
            raise                       # 일시적일 수 있다 — 복구가 다시 묻는다
        logger.exception("머지 여부를 끝내 보지 못했다: plan_id=%s", plan.id)
        await plan.mark_unknown()
        return
    pull = checked.pull
    if checked.merged:
        await plan.record_execution(
            success=True, stdout=f"PR #{pull.number} 을 머지했어요 ({pull.merge_commit[:12]}).", stderr="", exit_code=0,
            merge_commit=pull.merge_commit, sync_result=watch.interrupted(),
        )
    elif not pull.merged and pull.state == "open":
        await plan.record_execution(
            success=False, stdout="", exit_code=1,
            stderr=f"머지하기 전에 에이전트가 다시 시작돼 머지하지 못했어요. PR 은 남겨 뒀어요: {pull.url}",
        )
    else:
        await plan.mark_unknown()       # 닫혔거나 승인과 다른 내용으로 머지됐다 — 사용자가 PR 을 본다
