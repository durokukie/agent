"""가드레일 훅 — 변경 툴 호출이 트리거인 승인·기록 파이프라인.

팀 결정 (가드레일 v2):
- 위험도 판별 시스템 없음. 등급은 RISK_STICKERS(함수에 미리 붙인 스티커) 조회 한 줄.
- LLM이 호출하는 Action Plan 툴(조회/평가)은 MVP 제외 —
  Plan은 이 훅이 내부적으로 생성·기록한다.
- intent/expected_effects/side_effects는 변경 툴의 필수 인자 (스키마가 강제) —
  승인 화면과 Plan 본문에 실린다.

트리거: 변경 툴 4종의 호출 (LLM이 쓰려는 시도 자체).
LLM은 발동 여부에 관여할 수 없다. wrap 훅 하나가 전 단계를 감싼다.

대화 경로의 Plan 은 Kukie API 서버가 원본이다 (DURO-113, guardrail/action_plan.py). 각 단계는 server 의
상태 전이(dry-run·offer·decision·start·result …)가 기록된 뒤에야 다음으로 넘어간다. 특히 kubectl 직전의
decision·start 가 server 에 닿지 못하면 실행하지 않는다 — 기록 없이 돈 변경은 재시도에서 두 번 적용될 수 있다.

ArgoCD 가 관리하는 리소스면 (DURO-129, kukie_agent/argocd.py) Plan 을 만들기 전에 판별해 Plan·승인 카드에 경고를 싣는다.
막지는 않는다(결정: 경고 후 허용). 판별을 못 하면(권한 없음·ArgoCD 없음·시간 초과) 지금처럼 경고 없이 간다.
"""
from __future__ import annotations

import json
import logging
import shlex
from functools import partial

import yaml
from anyio import to_thread
from pydantic_ai import ApprovalRequired, ModelRetry, ToolFailed
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolCallPart, UserPromptPart

from kukie_agent import argocd
from kukie_agent.gitops import route as gitops_route
from kukie_agent.gitops.memo import is_memo
from kukie_agent.guardrail.action_plan import ActionPlan, PlanTarget
from kukie_agent.guardrail.approval import _manifest_preview
from kukie_agent.guardrail.decision_guidance import generate_decision_guidance
from kukie_agent.guardrail.mutation_request import canonicalize_mutation_args
from kukie_agent.kubectl import KubectlResult, assemble, run_kubectl, thread_limiter
from kukie_agent.manifests import Replicas
from kukie_agent.tools.mutate import MUTATING_TOOLS, RISK_STICKERS

logger = logging.getLogger(__name__)
hooks = Hooks()

#: 동시에 도는 dry-run 상한. 넘치면 다음 dry-run 은 기다린다 — 다른 채팅방의 툴·DNS 는 영향 없다.
#: probe(4)보다 큰 건 채팅 중에 일어나 여러 방에서 몰리기 때문이다.
DRY_RUN_WORKERS = 8
#: 동시에 도는 ArgoCD 판별(argocd.inspect, 읽기 조회 최대 argocd.LOOKUP_BUDGET 초) 상한. dry-run 과 따로 묶는다 —
#: 같이 묶으면 한 변경 호출이 dry-run 슬롯을 두 번 잡아 판별끼리도 dry-run 앞에서 줄을 선다 (agent#123 리뷰)
ARGOCD_LOOKUP_WORKERS = 8


def _recorded_result(plan: ActionPlan) -> KubectlResult:
    """이미 실행된 Plan 의 저장 결과를 툴 반환값 모양으로 되돌린다 (DURO-66 재시도).

    승인 재개(run)가 kubectl 실행 뒤에 실패하면 서버는 같은 승인으로 run 을 다시 부른다.
    그때 kubectl 을 또 돌리면 안 되므로 Plan 에 기록된 결과를 그대로 돌려준다 —
    LLM 은 첫 실행과 똑같은 결과를 보고, 화면 블록(collect_steps)도 KubectlResult 로 그려진다.
    command 는 run_kubectl 이 만드는 것과 같은 모양(kubectl --context … + 조립 args)으로 복원한다.
    """
    recorded = plan.execution_result
    assert recorded is not None
    return KubectlResult(
        command=shlex.join(["kubectl", "--context", str(plan.target["context"]), *plan.command]),
        stdout=str(recorded.get("stdout", "")),
        stderr=str(recorded.get("stderr", "")),
        success=bool(recorded.get("success")),
        exit_code=recorded.get("exit_code"),  # type: ignore[arg-type]
    )


def _dry_run_unsupported(stderr: str) -> bool:
    message = stderr.lower()
    return (
        "does not support dry run" in message
        or "unknown flag: --dry-run" in message
    )


def _guidance_manifest(manifest_yaml: str | None) -> list[dict] | None:
    """판단 가이드에 실을 매니페스트. 승인 카드와 같은 마스킹을 쓴다 (#50).

    **실패를 삼키지 않는다.** 삼키고 None 을 주면 프롬프트의 "미리보기가 없는 작업(scale·
    restart·delete)" 규칙에 걸려 모델이 apply_manifest 를 삭제·스케일 계열로 읽는다 —
    "본문이 없다" 는 말만 안 할 뿐 판단 칸이 다시 화면과 어긋난다 (PR #74 리뷰).

    호출부의 except 가 받아 "guidance unavailable" 로 둔다. 어차피 같은 입력으로
    build_approval_request 가 다시 돌다 터져 카드 자체가 안 뜨는 자리라, 거기에 유료
    모델을 한 번 더 부르지 않는 편이 낫다.
    """
    return _manifest_preview(manifest_yaml) if manifest_yaml else None


async def _argocd_ownership(ctx, target: PlanTarget) -> dict | None:
    """대상 리소스가 ArgoCD 관리인지 (argocd.inspect). 무슨 일이 있어도 올리지 않는다 — 경고를 못 붙일 뿐 승인 카드는 뜬다."""
    namespace = target.get("namespace")
    resources = target.get("resources") or [target]
    targets = [
        {
            "kind": resource["kind"],
            "name": resource["name"],
            **({"namespace": resource.get("namespace") or namespace} if resource.get("namespace") or namespace else {}),
        }
        for resource in resources
    ]
    try:
        # dry-run 과 같은 이유로 스레드로 넘긴다 (아래 rehearsal). 상한은 따로다 (ARGOCD_LOOKUP_WORKERS)
        return await to_thread.run_sync(
            partial(argocd.inspect, targets, context=ctx.deps.context, kubeconfig=ctx.deps.kubeconfig),
            limiter=thread_limiter("argocd_lookup", ARGOCD_LOOKUP_WORKERS),
        )
    except Exception:
        logger.exception("ArgoCD 판별을 건너뛴다")
        return None


#: PR 길 리소스에 kubectl 을 부르면 한 번 돌려보낼 때 쓰는 머리말 (DURO-135). 같은 호출을 다시 하면 이 말로 알아본다
PR_PATH_RETRY = "이 리소스는 지금 PR 길이다."
#: 같은 일을 PR 로도 할 수 있는 kubectl 툴 — restart·delete 는 PR 로 하는 변경이 아니다
_PR_ALTERNATIVE = frozenset({"scale_resource", "apply_manifest"})


def _already_warned(ctx, tool_name: str, args: dict) -> bool:
    """이번 턴에 같은 호출을 PR 길이라고 이미 돌려보냈나 — 그랬는데 또 부르면 사용자가 kubectl 을 고른 것으로 본다."""
    def key(value: object) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)

    wanted = key(args)
    turn = []
    for message in reversed(ctx.messages):
        turn.append(message)
        if isinstance(message, ModelRequest) and any(
            isinstance(p, UserPromptPart) and not is_memo(p) for p in message.parts
        ):
            break                                     # 이번 턴의 시작 — 그 앞의 턴은 보지 않는다
    parts = [part for message in turn for part in getattr(message, "parts", ())]
    calls = {part.tool_call_id: part for part in parts if isinstance(part, ToolCallPart) and part.tool_call_id}
    for part in parts:
        if (
            isinstance(part, RetryPromptPart) and part.tool_name == tool_name
            and isinstance(part.content, str) and part.content.startswith(PR_PATH_RETRY)
        ):
            call = calls.get(part.tool_call_id or "")
            if call is not None and key(call.args_as_dict()) == wanted:
                return True
    return False


async def _pr_path(ctx, tool_name: str, target: PlanTarget, normalized_args: dict) -> gitops_route.Route | None:
    """대상 가운데 PR 길인 리소스가 있으면 그 판단 (DURO-135). 없거나 판단을 못 하면 None — kubectl 길 그대로."""
    change = Replicas(count=normalized_args["replicas"]) if tool_name == "scale_resource" else None
    resources = target.get("resources") or [target]
    for resource in resources[: argocd.MAX_RESOURCES]:
        found = await gitops_route.decide(
            kind=str(resource["kind"]), name=str(resource["name"]),
            namespace=str(resource.get("namespace") or target.get("namespace") or "") or None,
            context=ctx.deps.context, kubeconfig=ctx.deps.kubeconfig, cluster_id=ctx.deps.cluster_id, change=change,
        )
        if found.path == "pr":
            return found
    return None


def _manifest_resources(manifest_yaml: str) -> list[dict[str, str]]:
    error = "manifest_yaml must contain resources with kind and metadata.name"
    try:
        documents = list(yaml.safe_load_all(manifest_yaml))
    except yaml.YAMLError as exc:
        raise ModelRetry("manifest_yaml must be valid YAML") from exc

    resources = []
    for document in documents:
        if document is None:
            continue
        if not isinstance(document, dict):
            raise ModelRetry(error)
        items = (
            document.get("items")
            if document.get("kind") == "List"
            else [document]
        )
        if not isinstance(items, list):
            raise ModelRetry(error)
        for resource in items:
            if not isinstance(resource, dict):
                raise ModelRetry(error)
            kind = resource.get("kind")
            metadata = resource.get("metadata")
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if (
                not isinstance(kind, str)
                or not kind.strip()
                or not isinstance(name, str)
                or not name.strip()
            ):
                raise ModelRetry(error)
            target = {"kind": kind, "name": name}
            if "namespace" in metadata:
                namespace = metadata["namespace"]
                if not isinstance(namespace, str) or not namespace.strip():
                    raise ModelRetry(error)
                target["namespace"] = namespace
            resources.append(target)
    if not resources:
        raise ModelRetry(error)
    return resources


@hooks.on.tool_execute(tools=sorted(MUTATING_TOOLS))
async def guardrail(ctx, *, call, tool_def, args, handler):
    """변경 요청을 승인 전 검토하고, 승인 후 한 번 실행해 결과를 기록한다."""
    tool_name = call.tool_name
    if tool_name not in MUTATING_TOOLS:
        raise ToolFailed(f"unregistered mutation tool: {tool_name}")
    if tool_name not in RISK_STICKERS:
        raise ToolFailed(f"missing RISK_STICKER for mutation tool: {tool_name}")
    if not call.tool_call_id:
        raise ToolFailed(f"missing tool_call_id for mutation tool: {tool_name}")

    intent = args["intent"]
    if not intent.strip():
        raise ModelRetry("intent must not be blank")
    if not args["expected_effects"]:
        raise ModelRetry("expected_effects must not be empty")
    if not args["side_effects"]:
        raise ModelRetry("side_effects must not be empty")

    canonical = canonicalize_mutation_args(
        tool_name,
        args,
        ctx.deps.namespace,
    )
    normalized_args = canonical.normalized
    plan_args = canonical.plan

    command = assemble(tool_name, normalized_args)
    target: PlanTarget = {
        "context": ctx.deps.context,
        **{
            key: normalized_args[key]
            for key in ("namespace", "kind", "name")
            if normalized_args.get(key) is not None
        },
    }
    if tool_name == "apply_manifest":
        target["resources"] = _manifest_resources(normalized_args["manifest_yaml"])
    risk = RISK_STICKERS[tool_name].name.lower()

    if ctx.tool_call_approved:
        # 여기서 server 에 못 닿으면(ServerError 503) 그대로 올라간다 — kubectl 전이라 아무것도 안 돌았고,
        # 재개 쪽이 RESUME_RETRYABLE 로 받아 같은 결정으로 다시 시도하게 한다.
        try:
            plan = await ActionPlan.find_by_call_id(call.tool_call_id, run_id=ctx.deps.run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise ToolFailed(str(exc)) from None
        # 재시도 분기 (DURO-66): 같은 승인으로 run 이 다시 왔을 때 kubectl 이 돌았는지는
        # 이 프로세스가 아니라 Plan 이 안다. 실행 기록이 있으면 그걸 돌려주고, 승인 기록만 있고
        # 실행 기록이 없으면(기록 도중 죽음·기록 실패) 실행 여부를 모르므로 다시 돌리지 않는다.
        if plan.execution_result is not None:
            return _recorded_result(plan)
        if plan.decision is not None:
            # 승인은 남았는데 결과가 없다 = kubectl 이 돌았는지 모른다. 다시 돌리지 않고 UNKNOWN 으로 남긴다 (DURO-83)
            try:
                await plan.mark_unknown()
            except Exception:
                logger.exception("UNKNOWN 표시 실패: plan_id=%s", plan.id)
            raise ToolFailed(
                "실행 여부를 확인할 수 없다 — 승인은 기록됐지만 실행 결과가 없다. "
                f"클러스터 상태를 직접 확인하라 (plan_id={plan.id})"
            )
        try:
            await plan.approve_for_execution(
                tool=tool_name,
                args=plan_args,
                command=command,
                risk=risk,
                target=target,
                user_id=ctx.deps.user_id,
            )
            await plan.mark_executing()
        except ValueError as exc:
            raise ToolFailed(str(exc)) from None
        try:
            result = await handler(args)
        except Exception as exc:
            try:
                await plan.record_execution(
                    success=False,
                    stdout="",
                    stderr=str(exc),
                    exit_code=None,
                )
            except Exception:
                logger.exception("failed to record execution error: plan_id=%s", plan.id)
            raise
        try:
            await plan.record_execution(
                success=result.success,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            )
        except Exception:
            logger.exception(
                "failed to record execution result: plan_id=%s",
                plan.id,
            )
            stderr = result.stderr.rstrip("\n")
            if stderr:
                stderr += "\n\n"
            stderr += (
                "[guardrail] 실행 결과를 Action Plan에 기록하지 못했습니다.\n"
                "동일 요청의 재실행은 차단되었습니다."
            )
            return result.model_copy(update={"stderr": stderr})
        return result

    # 승인 뒤(위 분기)에는 다시 보지 않는다 — 카드에 실린 경고를 보고 승인한 것이다.
    # 승인 순간 다시 판단해 길이 바뀌면 STALE 로 돌리는 것은 PR 길(DURO-134)과 함께 한다 (설계 1절)
    managed = await _argocd_ownership(ctx, target)
    if managed is not None and tool_name in _PR_ALTERNATIVE and getattr(ctx.deps, "cluster_id", None) is not None:
        # 길을 코드가 강제한다 (DURO-135): PR 길 리소스에 경고 없이 kubectl 을 부르면 이유를 붙여 한 번 돌려보낸다.
        # 옛 답변("kubectl 로 바꿀게요")에 끌려간 호출이 여기서 걸린다. 같은 호출을 다시 하면 경고 카드와 함께 간다(경고 후 허용)
        pr = await _pr_path(ctx, tool_name, target, normalized_args)
        if pr is not None:
            managed = {**managed, "route": {"path": "pr", "repository": pr.repository, "reason": pr.reason}}
            # 이 툴의 재시도를 이미 썼으면 돌려보내지 않는다 — 한도(1)를 넘으면 run 이 실패한다. 카드의 route 칸이 대신 알린다
            if not _already_warned(ctx, tool_name, args) and getattr(ctx, "retry", 0) < 1:
                raise ModelRetry(
                    f"{PR_PATH_RETRY} 이유: {pr.reason} 레플리카·이미지 태그·resources·환경변수 변경이면 "
                    "propose_git_change 로 다시 하라. 그 밖의 변경이거나 사용자가 kubectl 로 바로 바꾸길 원하면"
                    "(장애 대응 등) 같은 호출을 한 번 더 하라 — ArgoCD 경고 카드와 함께 승인을 받는다."
                )
    plan = await ActionPlan.create_draft(
        run_id=ctx.deps.run_id,
        call_id=call.tool_call_id,
        tool=tool_name,
        args=plan_args,
        command=command,
        risk=risk,
        skill=ctx.deps.skill.name,
        target=target,
        intent=intent,
        expected_effects=args["expected_effects"],
        side_effects=args["side_effects"],
        argocd=managed,
    )

    stdin = (
        normalized_args.get("manifest_yaml")
        if tool_name == "apply_manifest"
        else None
    )
    # 스레드로 넘긴다 — 여기는 async 훅이라 그냥 부르면 dry-run(최대 30초) 동안 이벤트 루프가 멈춰
    # 다른 채팅방의 run 까지 같이 선다. 툴 본체의 run_kubectl 은 pydantic-ai 가 이미 스레드로 돌린다.
    rehearsal = await to_thread.run_sync(
        partial(
            run_kubectl,
            command,
            context=ctx.deps.context,
            dry_run=True,
            stdin=stdin,
            kubeconfig=ctx.deps.kubeconfig,
        ),
        limiter=thread_limiter("dry_run", DRY_RUN_WORKERS),
    )

    if rehearsal.success:
        dry_run_status = "succeeded"
    elif _dry_run_unsupported(rehearsal.stderr):
        dry_run_status = "unsupported"
    else:
        dry_run_status = "failed"

    # 떨어진 예행은 기록하는 자리에서 FAILED + DRY_RUN_FAILED 로 닫힌다 (server 계획은 server 가 닫는다)
    await plan.record_dry_run(
        dry_run_status,
        rehearsal.stdout,
        rehearsal.stderr,
    )
    if dry_run_status == "failed":
        raise ToolFailed(f"dry-run failed: {rehearsal.stderr.strip()}")

    guidance = "guidance unavailable"
    if dry_run_status == "succeeded":
        try:
            # 승인 화면과 **같은** 마스킹을 거친 매니페스트를 함께 준다 (#50). Plan 에는 본문 대신
            # 해시만 남으므로, 안 주면 모델이 "본문이 제시되지 않았다" 고 답해 카드가 자기 모순이 된다.
            guidance = await generate_decision_guidance(plan, _guidance_manifest(stdin))
        except Exception:
            logger.exception("decision guidance unavailable: plan_id=%s", plan.id)

    await plan.offer(guidance)         # DRAFT → WAITING_APPROVAL: 이제 카드가 사용자에게 나간다
    raise ApprovalRequired({"plan_id": plan.id})
