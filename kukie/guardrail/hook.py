"""가드레일 훅 — 변경 툴 호출이 트리거인 승인·기록 파이프라인.

팀 결정 (가드레일 v2):
- 위험도 판별 시스템 없음. 등급은 RISK_STICKERS(함수에 미리 붙인 스티커) 조회 한 줄.
- LLM이 호출하는 Action Plan 툴(조회/평가)은 MVP 제외 —
  Plan은 이 훅이 내부적으로 생성·기록한다.
- intent/expected_effects/side_effects는 변경 툴의 필수 인자 (스키마가 강제) —
  승인 화면과 Plan 본문에 실린다.

트리거: 변경 툴 4종의 호출 (LLM이 쓰려는 시도 자체).
LLM은 발동 여부에 관여할 수 없다. wrap 훅 하나가 전 단계를 감싼다.
"""
from __future__ import annotations

import logging
import shlex

import yaml
from pydantic_ai import ApprovalRequired, ModelRetry, ToolFailed
from pydantic_ai.capabilities.hooks import Hooks

from kukie.guardrail.action_plan import ActionPlan, PlanTarget
from kukie.guardrail.decision_guidance import generate_decision_guidance
from kukie.guardrail.mutation_request import canonicalize_mutation_args
from kukie.kubectl import KubectlResult, assemble, run_kubectl
from kukie.tools.mutate import MUTATING_TOOLS, RISK_STICKERS

logger = logging.getLogger(__name__)
hooks = Hooks()


def _recorded_result(plan: ActionPlan) -> KubectlResult:
    """이미 실행된 Plan 의 저장 결과를 툴 반환값 모양으로 되돌린다 (DURO-66 재시도).

    승인 재개(run)가 kubectl 실행 뒤에 실패하면 서버는 같은 승인으로 run 을 다시 부른다.
    그때 kubectl 을 또 돌리면 안 되므로 Plan 파일에 기록된 결과를 그대로 돌려준다 —
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
        try:
            plan = ActionPlan.find_by_call_id(call.tool_call_id, run_id=ctx.deps.run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise ToolFailed(str(exc)) from None
        # 재시도 분기 (DURO-66): 같은 승인으로 run 이 다시 왔을 때 kubectl 이 돌았는지는
        # 서버가 아니라 Plan 파일이 안다. 실행 기록이 있으면 그걸 돌려주고, 승인 기록만 있고
        # 실행 기록이 없으면(기록 도중 죽음·기록 실패) 실행 여부를 모르므로 다시 돌리지 않는다.
        if plan.execution_result is not None:
            return _recorded_result(plan)
        if plan.decision is not None:
            # 승인은 남았는데 결과가 없다 = kubectl 이 돌았는지 모른다. 다시 돌리지 않고 UNKNOWN 으로 남긴다 (DURO-83)
            try:
                plan.mark_unknown()
            except Exception:
                logger.exception("UNKNOWN 표시 실패: plan_id=%s", plan.id)
            raise ToolFailed(
                "실행 여부를 확인할 수 없다 — 승인은 기록됐지만 실행 결과가 없다. "
                f"클러스터 상태를 직접 확인하라 (plan_id={plan.id})"
            )
        try:
            plan.approve_for_execution(
                tool=tool_name,
                args=plan_args,
                command=command,
                risk=risk,
                target=target,
                user_id=ctx.deps.user_id,
            )
            plan.mark_executing()
        except ValueError as exc:
            raise ToolFailed(str(exc)) from None
        try:
            result = await handler(args)
        except Exception as exc:
            try:
                plan.record_execution(
                    success=False,
                    stdout="",
                    stderr=str(exc),
                    exit_code=None,
                )
            except Exception:
                logger.exception("failed to record execution error: plan_id=%s", plan.id)
            raise
        try:
            plan.record_execution(
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

    plan = ActionPlan.create_draft(
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
    )

    stdin = (
        normalized_args.get("manifest_yaml")
        if tool_name == "apply_manifest"
        else None
    )
    rehearsal = run_kubectl(
        command,
        context=ctx.deps.context,
        dry_run=True,
        stdin=stdin,
        kubeconfig=ctx.deps.kubeconfig,
    )

    if rehearsal.success:
        dry_run_status = "succeeded"
    elif _dry_run_unsupported(rehearsal.stderr):
        dry_run_status = "unsupported"
    else:
        dry_run_status = "failed"

    plan.record_dry_run(
        dry_run_status,
        rehearsal.stdout,
        rehearsal.stderr,
    )
    if dry_run_status == "failed":
        plan.mark_failed("DRY_RUN_FAILED")
        raise ToolFailed(f"dry-run failed: {rehearsal.stderr.strip()}")

    guidance = "guidance unavailable"
    if dry_run_status == "succeeded":
        try:
            guidance = await generate_decision_guidance(plan)
        except Exception:
            logger.exception("decision guidance unavailable: plan_id=%s", plan.id)

    plan.record_decision_guidance(guidance)
    plan.offer_for_approval()          # DRAFT → WAITING_APPROVAL: 이제 카드가 사용자에게 나간다
    raise ApprovalRequired({"plan_id": plan.id})
