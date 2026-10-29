"""PR 길 가드레일 — propose_git_change 호출이 트리거인 승인·기록 파이프라인 (DURO-134).

kubectl 변경 4종의 훅(guardrail/hook.py)과 같은 틀이다: Plan 은 server 가 원본이고, 각 단계는 server 의 상태 전이가
기록된 뒤에야 다음으로 넘어간다. 다른 점은 셋이다.

  - **PR 은 승인 전에 연다** (결정 2026-10-30). dry-run 자리에 렌더 비교가, 승인 카드에 PR 링크·git diff·렌더 diff 가 온다
  - **승인 = 머지** 다. 클러스터는 ArgoCD 가 sync 한 뒤에 바뀐다 — 승인 뒤 sync·health 를 지켜보고 결과를 Plan 에 싣는다
  - **승인 순간 길을 다시 판단한다.** 레포 연결이 끊겼거나 Application·PR 이 바뀌었으면 실행하지 않고 STALE 로 닫는다

승인 전 흐름
    길 판단(PR 길이 아니면 ModelRetry — kubectl 변경 툴로) → 토큰 → 고칠 곳 찾기·렌더 비교 → Plan DRAFT
    → 브랜치·커밋·PR → 예행 기록(렌더 비교 통과) → 판단 가이드 → offer(PR) → 승인 카드
승인 뒤 흐름
    길 다시 판단 + PR 다시 보기(달라졌으면 STALE·PR 닫기) → decision·start → 머지
    → 브랜치 보호로 막히면 PR 을 남기고 FAILED("GitHub 에서 리뷰·머지가 필요해요")
    → sync·health 지켜보기 → result(머지 커밋·sync 결과). 실패면 revert 제안을 툴 결과에 싣는다
거절은 server.py 의 _approve 가 기록하고 close_rejected 로 PR 을 닫는다 (거절된 호출에는 훅이 돌지 않는다).
"""
from __future__ import annotations

import logging
import re
import uuid
from functools import partial
from typing import Any

from anyio import to_thread
from pydantic import TypeAdapter
from pydantic_ai import ApprovalRequired, ModelRetry, ToolFailed

from kukie_agent.gitops import pull_requests, route as routing, watch
from kukie_agent.gitops.github import GitHubError
from kukie_agent.guardrail.action_plan import ActionPlan, PlanTarget
from kukie_agent.guardrail.decision_guidance import generate_decision_guidance
from kukie_agent.guardrail.hook import hooks
from kukie_agent.guardrail.mutation_request import canonicalize_mutation_args
from kukie_agent.kubectl import thread_limiter
from kukie_agent.manifests import Change, Replicas
from kukie_agent.tools.gitops import RISK, TOOL_NAME, GitOpsResult

logger = logging.getLogger(__name__)

_CHANGE = TypeAdapter(Change)
#: 머지 뒤 지켜보기(watch, 최대 watch.SYNC_WAIT 초) 동시 상한
WATCH_WORKERS = 8

#: 승인 카드·Plan 에 싣는 "이 승인이 뜻하는 것" — kubectl 카드와 뜻이 다르다는 것을 분명히 (허브 문서 4절)
NOTICE = "승인하면 Kukie 가 이 PR 을 머지해요. 클러스터는 ArgoCD 가 sync 한 뒤에 바뀌어요."
BLOCKED = "GitHub 에서 리뷰·머지가 필요해요"


def branch_for(kind: str, name: str) -> str:
    """새 PR 브랜치 이름. Plan 을 만들기 전에 정해 Plan 의 gitops 칸에 남긴다 — 승인 순간 그 브랜치의 PR 인지 맞춰 본다."""
    slug = re.sub(r"[^a-z0-9-]+", "-", f"{kind}-{name}".lower()).strip("-")[:40] or "change"
    return f"kukie/{slug}-{uuid.uuid4().hex[:8]}"


def command_of(repository: str) -> list[str]:
    """Plan 의 command — 실제로 도는 kubectl 이 없어 표시용이다. 승인 대조(decision 의 call)에 들어간다."""
    return ["github", "pr", "merge", repository]


def _display(repository: str, number: int | None) -> str:
    return f"github pr merge {repository}#{number}" if number else f"github pr merge {repository}"


def _again(ctx, message: str) -> Exception:
    """모델에게 다시 하게 한다. 이 툴의 재시도를 이미 썼으면 ToolFailed — 또 ModelRetry 를 던지면 재시도 한도(1)를
    넘어 run 이 통째로 실패한다. ToolFailed 도 같은 문구를 모델에게 보여 주고 한도를 쓰지 않는다."""
    return ToolFailed(message) if getattr(ctx, "retry", 0) >= 1 else ModelRetry(message)


def _kubectl_retry(ctx, reason: str) -> Exception:
    return _again(ctx, (
        f"이 리소스는 지금 kubectl 길이다. 이유: {reason} "
        "propose_git_change 대신 kubectl 변경 툴(scale_resource·apply_manifest 등)로 다시 하라 — "
        "ArgoCD 관리 리소스면 승인 카드에 경고가 같이 뜬다."
    ))


def _result(plan: ActionPlan) -> GitOpsResult:
    """이미 실행된 계획의 저장 결과 (DURO-66 재시도) — 첫 실행과 같은 결과를 돌려준다."""
    recorded = plan.execution_result or {}
    pull = plan.pull_request or {}
    repository = (plan.gitops or {}).get("repository", "")
    return GitOpsResult(
        command=_display(repository, pull.get("number")),
        stdout=str(recorded.get("stdout", "")), stderr=str(recorded.get("stderr", "")),
        success=bool(recorded.get("success")), exit_code=recorded.get("exit_code"),  # type: ignore[arg-type]
        pull_request=plan.pull_request, merge_commit=recorded.get("merge_commit"),
        sync_result=recorded.get("sync_result"),
        note=_follow_up(recorded.get("sync_result"), plan),
    )


def _follow_up(sync: dict[str, Any] | None, plan: ActionPlan) -> str | None:
    """sync 가 실패했으면 revert 제안 — 모델이 사용자에게 되돌릴지 묻게 한다. 되돌리기도 새 PR·새 승인이다."""
    if not sync or sync.get("status") not in ("sync_failed", "degraded"):
        return None
    return ("머지는 됐지만 ArgoCD 반영이 실패했다. 이유를 설명하고, 되돌리려면 같은 칸을 원래 값(렌더 diff 의 - 줄)으로 "
            "propose_git_change 해 revert PR 을 올리자고 제안하라. 사용자가 원하기 전에는 부르지 마라.")


async def _decide(ctx, kind: str, name: str, namespace: str, change: Change) -> routing.Route:
    return await routing.decide(
        kind=kind, name=name, namespace=namespace, context=ctx.deps.context, kubeconfig=ctx.deps.kubeconfig,
        cluster_id=ctx.deps.cluster_id, change=change,
    )


@hooks.on.tool_execute(tools=[TOOL_NAME])
async def gitops_guardrail(ctx, *, call, tool_def, args, handler):
    """PR 을 승인 전에 열고, 승인되면 머지해 sync 까지 지켜본다. handler(툴 본체)는 부르지 않는다 — tools/gitops.py."""
    if not call.tool_call_id:
        raise ToolFailed(f"missing tool_call_id for mutation tool: {TOOL_NAME}")
    intent = args["intent"]
    if not intent.strip():
        raise ModelRetry("intent must not be blank")
    if not args["expected_effects"]:
        raise ModelRetry("expected_effects must not be empty")
    if not args["side_effects"]:
        raise ModelRetry("side_effects must not be empty")

    canonical = canonicalize_mutation_args(TOOL_NAME, args, ctx.deps.namespace)
    plan_args = canonical.plan
    change = _CHANGE.validate_python(plan_args["change"])
    kind, name, namespace = plan_args["kind"], plan_args["name"], plan_args["namespace"]
    target: PlanTarget = {"context": ctx.deps.context, "namespace": namespace, "kind": kind, "name": name}
    risk = RISK.name.lower()

    if ctx.tool_call_approved:
        return await _execute(ctx, call, plan_args, target, risk, kind, name, namespace, change)

    found = await _decide(ctx, kind, name, namespace, change)
    if found.path != "pr":
        raise _kubectl_retry(ctx, found.reason)
    assert found.repository and found.target
    try:
        token = await pull_requests.token_for(ctx.deps.cluster_id, found.repository)
        hpa = None
        if isinstance(change, Replicas):
            hpa = await to_thread.run_sync(partial(
                routing.find_hpa, found.target.kind, found.target.name, found.target.namespace,
                context=ctx.deps.context, kubeconfig=ctx.deps.kubeconfig,
            ), limiter=thread_limiter("gitops_route", routing.LOOKUP_WORKERS))
        draft = await pull_requests.draft(found, change, token, hpa=hpa)
    except pull_requests.Refuse as refused:
        if refused.kubectl:
            raise _kubectl_retry(ctx, refused.message) from None
        if refused.retry:
            raise _again(ctx, refused.message) from None
        raise ToolFailed(refused.message) from None
    except GitHubError as exc:
        raise ToolFailed(f"매니페스트 레포를 읽지 못했다 ({exc}). 잠시 뒤 다시 하거나 kubectl 변경 툴로 하라.") from None

    application = pull_requests.application_summary(found)
    command = command_of(found.repository)
    branch = branch_for(found.target.kind, found.target.name)
    gitops: dict[str, Any] = {
        "repository": found.repository,
        "base": draft.base,
        "branch": branch,
        "base_sha": draft.base_sha,
        "application": application,
        "route": found.fingerprint(),
        "files": draft.file_entries(),
        "file_diff": draft.file_diff,
        "render_diff": draft.render_diff,
        "warnings": draft.warnings,
        "conflict_keys": draft.conflict_keys(),
        "merge_methods": list(draft.merge_methods),
        "notice": NOTICE,
    }
    plan = await ActionPlan.create_draft(
        run_id=ctx.deps.run_id, call_id=call.tool_call_id, tool=TOOL_NAME, args=plan_args, command=command,
        risk=risk, skill=ctx.deps.skill.name, target=target, intent=intent,
        expected_effects=args["expected_effects"], side_effects=args["side_effects"], gitops=gitops,
    )
    code = plan.code or plan.id
    title = f"[Kukie] {intent.strip()[:80]}"
    try:
        pull = await pull_requests.open_pull(
            draft, token, branch=branch, title=title,
            body=pull_requests.pull_body(intent=intent, expected_effects=args["expected_effects"],
                                         side_effects=args["side_effects"], draft=draft,
                                         application=application, plan_code=code, plan_id=plan.id),
        )
    except GitHubError as exc:
        # PR 을 못 열었다 = 승인 전 준비가 떨어졌다. kubectl 의 dry-run 실패와 같은 자리로 닫는다 (FAILED + DRY_RUN_FAILED)
        await plan.record_dry_run("failed", "", f"PR 을 만들지 못했다: {exc}")
        raise ToolFailed(f"PR 을 만들지 못했다 ({exc}).") from None

    pull_request = {"number": pull.number, "url": pull.url}
    try:
        # diff 는 gitops 칸 한 곳에만 둔다 — 판단 가이드 프롬프트에 같은 diff 가 여러 벌 들어가지 않게 (agent#126 리뷰)
        await plan.record_dry_run("succeeded", f"렌더 비교 통과 — PR #{pull.number} {pull.url}", "")
        guidance = "guidance unavailable"
        try:
            guidance = await generate_decision_guidance(plan)
        except Exception:
            logger.exception("decision guidance unavailable: plan_id=%s", plan.id)
        await plan.offer(guidance, pull_request=pull_request)
    except BaseException:
        # 카드가 못 나갔다 — 열어 둔 PR 이 고아로 남지 않게 닫는다
        await pull_requests.close(ctx.deps.cluster_id, plan.gitops, pull_request,
                                  "Kukie 가 승인 카드를 띄우지 못해 이 PR 을 닫았어요.")
        raise
    raise ApprovalRequired({"plan_id": plan.id})


async def _execute(ctx, call, plan_args, target, risk, kind, name, namespace, change) -> GitOpsResult:
    try:
        plan = await ActionPlan.find_by_call_id(call.tool_call_id, run_id=ctx.deps.run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise ToolFailed(str(exc)) from None
    if plan.execution_result is not None:
        return _result(plan)
    if plan.decision is not None:
        # 승인은 남았는데 결과가 없다 = 머지가 됐는지 모른다. 다시 머지하지 않는다 (kubectl 훅과 같은 규칙, DURO-83)
        try:
            await plan.mark_unknown()
        except Exception:
            logger.exception("UNKNOWN 표시 실패: plan_id=%s", plan.id)
        pull = (plan.pull_request or {}).get("url", "")
        raise ToolFailed(f"머지 여부를 확인할 수 없다 — 승인은 기록됐지만 결과가 없다. PR {pull} 을 직접 확인하라 "
                         f"(plan_id={plan.id})")
    gitops = plan.gitops or {}
    pull_request = plan.pull_request or {}
    if not gitops or not pull_request:
        raise ToolFailed(f"PR 정보가 없는 계획이다 (plan_id={plan.id})")

    # 승인 순간 다시 판단한다 — 길이 달라졌으면 실행하지 않고 다시 제안한다 (Plan STALE, 설계 1절)
    found = await _decide(ctx, kind, name, namespace, change)
    if not found.known:
        # 길을 정하지 못했다(조회 실패·시간 초과) — 길이 바뀐 증거가 아니다. 아무것도 안 했으니 재개가 같은 결정으로
        # 다시 시도하게 올린다 (RESUME_RETRYABLE). STALE 로 닫으면 일시적 실패로 승인한 변경·PR 을 버린다 (agent#126 리뷰)
        raise RuntimeError(f"승인 순간 PR 길을 다시 판단하지 못했다: {found.reason}")
    stale = None
    if found.path != "pr":
        stale = f"지금은 PR 길이 아니에요: {found.reason}"
    elif found.fingerprint() != gitops.get("route"):
        stale = "대상 ArgoCD 앱이나 레포 설정이 승인 카드를 만든 뒤 바뀌었어요."
    token = None
    checked = None
    if stale is None:
        try:
            token = await pull_requests.token_for(ctx.deps.cluster_id, gitops["repository"])
            checked = await pull_requests.recheck(gitops, pull_request, token)
            stale = checked.stale
        except pull_requests.Refuse as refused:
            stale = refused.message
        except GitHubError as exc:
            # 아직 아무것도 안 했다 — 재개 쪽이 같은 결정으로 다시 시도할 수 있게 올린다 (RESUME_RETRYABLE)
            raise RuntimeError(f"GitHub 에서 PR 을 다시 보지 못했다: {exc}") from exc
    if stale is not None:
        try:
            await plan.mark_stale()
        except Exception:
            # server 가 stale 을 모르면(옛 server) 계획은 run 이 닫힐 때 EXPIRED 로 닫힌다 — 실행하지 않은 것은 같다
            logger.exception("STALE 표시 실패: plan_id=%s", plan.id)
        if checked is None or not checked.pull.merged:      # 이미 머지된 PR 에 "머지하지 않고 닫았다" 고 남기지 않는다
            await pull_requests.close(ctx.deps.cluster_id, gitops, pull_request,
                                      f"승인하는 사이 전제가 바뀌어 Kukie 가 머지하지 않고 닫았어요: {stale}")
        raise ToolFailed(f"실행하지 않았다 — {stale} 지금 상태로 다시 판단해 사용자에게 다시 제안하라 (plan_id={plan.id}).")
    assert token is not None and checked is not None
    pull = checked.pull

    try:
        await plan.approve_for_execution(
            tool=TOOL_NAME, args=plan_args, command=command_of(gitops["repository"]), risk=risk, target=target,
            user_id=ctx.deps.user_id,
        )
        await plan.mark_executing()
    except ValueError as exc:
        raise ToolFailed(str(exc)) from None

    display = _display(gitops["repository"], pull.number)
    if checked.merged:
        # 승인한 내용 그대로 GitHub 에서 이미 머지됐다 — 머지는 건너뛰고 sync 만 지켜본다 (agent#126 리뷰)
        return await _watch_and_record(ctx, plan, gitops, pull, pull_request, token, pull.merge_commit,
                                       f"PR #{pull.number} 은 GitHub 에서 이미 머지됐어요 ({pull.merge_commit[:12]}).")
    if pull.mergeable_state == "blocked":
        return await _record(plan, GitOpsResult(
            command=display, stdout="", success=False, exit_code=1, pull_request=pull_request,
            stderr=f"브랜치 보호 규칙 때문에 Kukie 가 머지할 수 없어요 — {BLOCKED}. PR 은 남겨 뒀어요: {pull.url}",
        ))
    try:
        merge_commit = await pull_requests.merge(gitops, pull, token,
                                                 title=f"[Kukie] {plan.intent.strip()[:80]} (#{pull.number})")
    except GitHubError as exc:
        if exc.status in (405, 409):
            why = BLOCKED if exc.status == 405 else "PR 이 승인한 뒤 바뀌었어요"
            return await _record(plan, GitOpsResult(
                command=display, stdout="", success=False, exit_code=1, pull_request=pull_request,
                stderr=f"머지하지 못했어요 — {why} ({exc.message}). PR 은 남겨 뒀어요: {pull.url}",
            ))
        # 머지 요청이 닿았는지 모른다 — 결과 없이 두면 재시도가 UNKNOWN 으로 남긴다
        raise
    return await _watch_and_record(ctx, plan, gitops, pull, pull_request, token, merge_commit,
                                   f"PR #{pull.number} 을 머지했어요 ({merge_commit[:12]}).")


async def _watch_and_record(ctx, plan, gitops, pull, pull_request, token, merge_commit: str,
                            merged_line: str) -> GitOpsResult:
    await pull_requests.tidy_after_merge(gitops, token)
    display = _display(gitops["repository"], pull.number)
    application = gitops.get("application") or {}
    try:
        sync = await to_thread.run_sync(partial(
            watch.watch, application.get("name", ""), application.get("namespace", ""), merge_commit,
            auto_sync=bool(application.get("auto_sync")), context=ctx.deps.context, kubeconfig=ctx.deps.kubeconfig,
        ), limiter=thread_limiter("gitops_watch", WATCH_WORKERS))
    except Exception:
        logger.exception("sync 지켜보기 실패: plan_id=%s", plan.id)
        sync = {"status": "unreadable", "message": "ArgoCD 앱 상태를 읽지 못했어요.", "sync_status": "Unknown",
                "health_status": "Unknown", "revision": "", "waited_seconds": 0}
    stdout = f"{merged_line} {sync['message']}"
    return await _record(plan, GitOpsResult(
        command=display, stdout=stdout, stderr="", success=True, exit_code=0, pull_request=pull_request,
        merge_commit=merge_commit, sync_result=sync, note=_follow_up(sync, plan),
    ))


async def _record(plan: ActionPlan, result: GitOpsResult) -> GitOpsResult:
    """결과를 Plan 에 남긴다. 못 남기면 kubectl 훅처럼 결과에 그 사실을 붙인다 — 같은 승인으로 다시 머지하지 않는다."""
    try:
        await plan.record_execution(
            success=result.success, stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code,
            merge_commit=result.merge_commit, sync_result=result.sync_result,
        )
    except Exception:
        logger.exception("failed to record execution result: plan_id=%s", plan.id)
        stderr = result.stderr.rstrip("\n")
        stderr = (stderr + "\n\n" if stderr else "") + (
            "[guardrail] 실행 결과를 Action Plan에 기록하지 못했습니다.\n동일 요청의 재실행은 차단되었습니다."
        )
        return result.model_copy(update={"stderr": stderr})
    return result


async def close_rejected(plan: ActionPlan, cluster_id: str | None) -> None:
    """거절한 카드의 PR 을 닫는다 (server._approve 가 거절을 기록한 뒤 부른다). 실패해도 거절은 그대로다."""
    if plan.tool != TOOL_NAME:
        return
    await pull_requests.close(cluster_id, plan.gitops, plan.pull_request, "채팅에서 거절돼 Kukie 가 이 PR 을 닫았어요.")
