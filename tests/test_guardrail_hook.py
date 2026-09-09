import logging
from datetime import datetime
from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic_ai import ApprovalRequired, ModelRetry, ToolFailed
from pydantic_ai.messages import ToolCallPart

from kukie.guardrail import action_plan, hook
from kukie.kubectl import KubectlResult
from kukie.kubectl import runner as kubectl_runner


BASE_ARGS = {
    "kind": "deployment",
    "name": "nginx",
    "replicas": 3,
    "namespace": "study",
    "intent": "nginx 레플리카를 늘린다.",
    "expected_effects": ["레플리카가 3개가 된다."],
    "side_effects": ["추가 노드 자원을 사용한다."],
}

DELETE_ARGS = {
    "kind": "deployment",
    "name": "nginx",
    "namespace": "study",
    "intent": "nginx deployment를 삭제한다.",
    "expected_effects": ["deployment가 삭제된다."],
    "side_effects": ["서비스가 중단될 수 있다."],
}


def _ctx(*, namespace="study", approved=False, run_id=None, user_id=None):
    return SimpleNamespace(
        deps=SimpleNamespace(
            context="kind-dev",
            namespace=namespace,
            skill=SimpleNamespace(name="실습"),
            run_id=run_id,      # None 이면 계획이 DB 표에 들어가지 않는다 (flat 경로와 같다, #58)
            user_id=user_id,
            kubeconfig=None,    # 서버 컴퓨터의 기본 kubeconfig (기획 04 §8 등록 경로가 아님)
        ),
        tool_call_approved=approved,
    )


def _call(tool_name="scale_resource", call_id="call-123"):
    return ToolCallPart(tool_name=tool_name, tool_call_id=call_id)


def _read_plan(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    frontmatter, separator, _ = text.removeprefix("---\n").partition("\n---\n")
    assert separator
    return yaml.safe_load(frontmatter)


async def _create_pending_plan(*, ctx=None, call=None, args=None) -> None:
    with pytest.raises(ApprovalRequired):
        await hook.guardrail(
            ctx or _ctx(),
            call=call or _call(),
            tool_def=None,
            args=args if args is not None else BASE_ARGS,
            handler=AsyncMock(),
        )


@pytest.fixture
def successful_dry_run(monkeypatch):
    calls = []

    def fake_run(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        calls.append(
            {
                "args": args,
                "context": context,
                "dry_run": dry_run,
                "stdin": stdin,
            }
        )
        return KubectlResult(
            command="kubectl dry-run",
            stdout="ok\n",
            stderr="",
            success=True,
        )

    monkeypatch.setattr(hook, "run_kubectl", fake_run)
    return calls


@pytest.fixture
def successful_guidance(monkeypatch):
    generate = AsyncMock(return_value="배포 시간과 롤백 기준을 확인한다.")
    monkeypatch.setattr(
        hook,
        "generate_decision_guidance",
        generate,
        raising=False,
    )
    return generate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent", ""),
        ("intent", "   "),
        ("expected_effects", []),
        ("side_effects", []),
    ],
)
async def test_설명_필드가_유효하지_않으면_Plan_생성_전에_거부한다(
    monkeypatch, tmp_path, field, value
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    with pytest.raises(ModelRetry):
        await hook.guardrail(
            _ctx(),
            call=_call(),
            tool_def=None,
            args={**BASE_ARGS, field: value},
            handler=handler,
        )

    handler.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_미등록_mutation은_Plan_생성_전에_거부한다(monkeypatch, tmp_path):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="unregistered mutation tool"):
        await hook.guardrail(
            _ctx(),
            call=_call("raw_kubectl"),
            tool_def=None,
            args=BASE_ARGS,
            handler=handler,
        )

    handler.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_RISK_STICKER_누락은_Plan_생성_전에_거부한다(monkeypatch, tmp_path):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.delitem(hook.RISK_STICKERS, "scale_resource")
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="RISK_STICKER"):
        await hook.guardrail(
            _ctx(), call=_call(), tool_def=None, args=BASE_ARGS, handler=handler
        )

    handler.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_call_id_누락은_Plan_생성_전에_거부한다(monkeypatch, tmp_path):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="tool_call_id"):
        await hook.guardrail(
            _ctx(), call=_call(call_id=""), tool_def=None, args=BASE_ARGS, handler=handler
        )

    handler.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "args", "expected_risk"),
    [
        (
            "apply_manifest",
            {
                "manifest_yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx\n",
                "namespace": "study",
                "intent": "Pod를 적용한다.",
                "expected_effects": ["Pod가 생성된다."],
                "side_effects": ["노드 자원을 사용한다."],
            },
            "caution",
        ),
        ("scale_resource", BASE_ARGS, "caution"),
        (
            "rollout_restart",
            {key: value for key, value in BASE_ARGS.items() if key != "replicas"},
            "caution",
        ),
        (
            "delete_resource",
            {key: value for key, value in BASE_ARGS.items() if key != "replicas"},
            "destructive",
        ),
    ],
)
async def test_CAUTION과_DESTRUCTIVE는_고정_위험도로_ApprovalRequired를_한번_발생시킨다(
    monkeypatch,
    tmp_path,
    successful_dry_run,
    successful_guidance,
    tool_name,
    args,
    expected_risk,
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    with pytest.raises(ApprovalRequired) as approval_required:
        await hook.guardrail(
            _ctx(),
            call=_call(tool_name),
            tool_def=None,
            args=args,
            handler=handler,
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["risk_level"] == expected_risk
    assert metadata["call_id"] == "call-123"
    assert set(metadata["args"]).isdisjoint(
        {"intent", "expected_effects", "side_effects"}
    )
    assert approval_required.value.metadata == {"plan_id": metadata["id"]}
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_보호_namespace도_CAUTION_위험도를_유지한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    args = {**BASE_ARGS, "namespace": "kube-system"}

    with pytest.raises(ApprovalRequired):
        await hook.guardrail(
            _ctx(),
            call=_call(),
            tool_def=None,
            args=args,
            handler=AsyncMock(),
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["risk_level"] == "caution"


@pytest.mark.asyncio
async def test_apply_manifest는_리소스_식별정보를_Plan에_저장한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    args = {
        "manifest_yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx\n",
        "namespace": None,
        "intent": "Pod를 적용한다.",
        "expected_effects": ["Pod가 생성된다."],
        "side_effects": ["노드 자원을 사용한다."],
    }

    with pytest.raises(ApprovalRequired):
        await hook.guardrail(
            _ctx(namespace="study"),
            call=_call("apply_manifest"),
            tool_def=None,
            args=args,
            handler=AsyncMock(),
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["args"] == {
        "namespace": "study",
        "manifest_sha256": "7844a377da8301123db998e13ee9ff005ba1022db463e2316fb95b6f681c34fb",
    }
    assert metadata["target"] == {
        "context": "kind-dev",
        "namespace": "study",
        "resources": [{"kind": "Pod", "name": "nginx"}],
    }
    assert metadata["command"] == ["apply", "-f", "-", "-n", "study"]
    assert successful_dry_run[0]["args"] == ["apply", "-f", "-", "-n", "study"]
    assert successful_dry_run[0]["stdin"] == args["manifest_yaml"]
    assert args["manifest_yaml"] not in next(tmp_path.glob("*.md")).read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_apply_manifest는_여러_YAML문서의_대상을_순서대로_저장한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    args = {
        "manifest_yaml": (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: nginx\n"
            "---\n"
            "apiVersion: v1\n"
            "kind: Service\n"
            "metadata:\n"
            "  name: nginx\n"
            "  namespace: study\n"
        ),
        "namespace": "study",
        "intent": "nginx Deployment와 Service를 적용한다.",
        "expected_effects": ["두 리소스가 적용된다."],
        "side_effects": ["클러스터 구성이 변경된다."],
    }

    with pytest.raises(ApprovalRequired):
        await hook.guardrail(
            _ctx(),
            call=_call("apply_manifest"),
            tool_def=None,
            args=args,
            handler=AsyncMock(),
        )

    loaded = action_plan.ActionPlan.load(next(tmp_path.glob("*.md")))
    assert loaded.target["resources"] == [
        {"kind": "Deployment", "name": "nginx"},
        {"kind": "Service", "name": "nginx", "namespace": "study"},
    ]


@pytest.mark.asyncio
async def test_apply_manifest는_List의_items를_개별_대상으로_저장한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    args = {
        "manifest_yaml": (
            "apiVersion: v1\n"
            "kind: List\n"
            "items:\n"
            "  - apiVersion: v1\n"
            "    kind: ConfigMap\n"
            "    metadata:\n"
            "      name: settings\n"
            "  - apiVersion: v1\n"
            "    kind: Service\n"
            "    metadata:\n"
            "      name: nginx\n"
            "      namespace: study\n"
        ),
        "namespace": "study",
        "intent": "ConfigMap과 Service를 적용한다.",
        "expected_effects": ["두 리소스가 적용된다."],
        "side_effects": ["클러스터 구성이 변경된다."],
    }

    with pytest.raises(ApprovalRequired):
        await hook.guardrail(
            _ctx(),
            call=_call("apply_manifest"),
            tool_def=None,
            args=args,
            handler=AsyncMock(),
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["target"]["resources"] == [
        {"kind": "ConfigMap", "name": "settings"},
        {"kind": "Service", "name": "nginx", "namespace": "study"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_yaml",
    [
        "kind: [",
        "metadata:\n  name: nginx\n",
        "kind: Pod\nmetadata: {}\n",
        " \n---\n",
        "kind: List\nitems:\n  - invalid\n",
        "kind: '   '\nmetadata:\n  name: nginx\n",
        "kind: Pod\nmetadata:\n  name: '   '\n",
        "kind: Pod\nmetadata:\n  name: nginx\n  namespace: '   '\n",
        "kind: Pod\nmetadata:\n  name: nginx\n  namespace: 0\n",
        "kind: Pod\nmetadata:\n  name: nginx\n  namespace: false\n",
    ],
)
async def test_apply_manifest는_식별할_수_없는_리소스를_Plan_생성_전에_거부한다(
    monkeypatch, tmp_path, manifest_yaml
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    def unexpected_dry_run(*args, **kwargs):
        pytest.fail("식별할 수 없는 manifest는 dry-run하면 안 된다")

    monkeypatch.setattr(hook, "run_kubectl", unexpected_dry_run)
    args = {
        "manifest_yaml": manifest_yaml,
        "namespace": "study",
        "intent": "리소스를 적용한다.",
        "expected_effects": ["리소스가 적용된다."],
        "side_effects": ["클러스터 구성이 변경된다."],
    }

    with pytest.raises(ModelRetry, match="manifest"):
        await hook.guardrail(
            _ctx(),
            call=_call("apply_manifest"),
            tool_def=None,
            args=args,
            handler=handler,
        )

    assert list(tmp_path.iterdir()) == []
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_성공은_판단_가이드를_저장하고_Plan으로_승인을_연결한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    def unexpected_cli(*args, **kwargs):
        pytest.fail("CLI 입력이나 cli_approve를 호출하면 안 된다")

    monkeypatch.setattr("builtins.input", unexpected_cli)
    monkeypatch.setattr(hook, "cli_approve", unexpected_cli, raising=False)

    with pytest.raises(ApprovalRequired) as approval_required:
        await hook.guardrail(
            _ctx(), call=_call(), tool_def=None, args=BASE_ARGS, handler=handler
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "WAITING_APPROVAL"
    assert metadata["decision"] is None
    assert metadata["dry_run_result"]["status"] == "succeeded"
    assert metadata["dry_run_result"]["stdout"] == "ok\n"
    assert metadata["dry_run_result"]["stderr"] == ""
    assert metadata["decision_guidance"] == "배포 시간과 롤백 기준을 확인한다."
    assert approval_required.value.metadata == {"plan_id": metadata["id"]}
    assert successful_dry_run == [
        {
            "args": [
                "scale",
                "deployment",
                "nginx",
                "--replicas=3",
                "-n",
                "study",
            ],
            "context": "kind-dev",
            "dry_run": True,
            "stdin": None,
        }
    ]
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("guidance model unavailable"),
        ValueError("LLM returned empty decision guidance"),
    ],
)
async def test_판단_가이드_예외와_빈_출력은_대체문구를_저장하고_승인을_계속한다(
    monkeypatch, tmp_path, successful_dry_run, caplog, error
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)

    async def fail_guidance(plan):
        raise error

    monkeypatch.setattr(hook, "generate_decision_guidance", fail_guidance)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ApprovalRequired):
            await hook.guardrail(
                _ctx(),
                call=_call(),
                tool_def=None,
                args=BASE_ARGS,
                handler=AsyncMock(),
            )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["decision_guidance"] == "guidance unavailable"
    assert metadata["risk_level"] == "caution"
    assert metadata["status"] == "WAITING_APPROVAL"
    assert metadata["decision"] is None
    assert metadata["execution_result"] is None
    assert str(error) in caplog.text


@pytest.mark.asyncio
async def test_Plan은_server_dry_run_전에_저장된다(
    monkeypatch, tmp_path, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)

    def fake_run(*args, **kwargs):
        plans = list(tmp_path.glob("*.md"))
        assert len(plans) == 1
        assert _read_plan(plans[0])["dry_run_result"] is None
        return KubectlResult(
            command="kubectl dry-run",
            stdout="ok\n",
            stderr="",
            success=True,
        )

    monkeypatch.setattr(hook, "run_kubectl", fake_run)

    with pytest.raises(ApprovalRequired):
        await hook.guardrail(
            _ctx(),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_dry_run_실패는_Plan을_failed로_남기고_중단한다(monkeypatch, tmp_path):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)

    async def unexpected_guidance(plan):
        pytest.fail("dry-run 실패에서는 guidance를 생성하면 안 된다")

    monkeypatch.setattr(hook, "generate_decision_guidance", unexpected_guidance)
    monkeypatch.setattr(
        hook,
        "run_kubectl",
        lambda *args, **kwargs: KubectlResult(
            command="kubectl dry-run",
            stdout="",
            stderr="deployment nginx not found\n",
            success=False,
        ),
    )
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="dry-run failed"):
        await hook.guardrail(
            _ctx(), call=_call(), tool_def=None, args=BASE_ARGS, handler=handler
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "FAILED"
    assert metadata["failure_reason"] == "DRY_RUN_FAILED"
    assert metadata["dry_run_result"]["status"] == "failed"
    assert metadata["dry_run_result"]["stdout"] == ""
    assert metadata["dry_run_result"]["stderr"] == "deployment nginx not found\n"
    assert metadata["decision"] is None
    assert metadata["execution_result"] is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_stderr"),
    [
        (
            TimeoutExpired(cmd=["kubectl"], timeout=30),
            "timed out after 30 seconds",
        ),
        (FileNotFoundError("kubectl을 찾을 수 없음"), "kubectl을 찾을 수 없음"),
    ],
)
async def test_dry_run_실행_예외도_Plan을_failed로_남긴다(
    monkeypatch, tmp_path, error, expected_stderr
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)

    def raise_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(kubectl_runner.subprocess, "run", raise_error)
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="dry-run failed"):
        await hook.guardrail(
            _ctx(), call=_call(), tool_def=None, args=BASE_ARGS, handler=handler
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "FAILED"
    assert metadata["failure_reason"] == "DRY_RUN_FAILED"
    assert metadata["dry_run_result"]["status"] == "failed"
    assert metadata["dry_run_result"]["stdout"] == ""
    assert expected_stderr in metadata["dry_run_result"]["stderr"]
    assert metadata["decision"] is None
    assert metadata["execution_result"] is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stderr",
    [
        "Error from server: admission webhook does not support dry run\n",
        "error: unknown flag: --dry-run\n",
    ],
)
async def test_dry_run_미지원은_판단_가이드_없이_원인을_남기고_승인을_요청한다(
    monkeypatch, tmp_path, stderr
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(
        hook,
        "run_kubectl",
        lambda *args, **kwargs: KubectlResult(
            command="kubectl dry-run",
            stdout="",
            stderr=stderr,
            success=False,
        ),
    )
    handler = AsyncMock()

    async def unexpected_guidance(plan):
        pytest.fail("dry-run 미지원에서는 guidance를 생성하면 안 된다")

    monkeypatch.setattr(
        hook,
        "generate_decision_guidance",
        unexpected_guidance,
    )

    with pytest.raises(ApprovalRequired) as approval_required:
        await hook.guardrail(
            _ctx(), call=_call(), tool_def=None, args=BASE_ARGS, handler=handler
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "WAITING_APPROVAL"
    assert metadata["dry_run_result"]["status"] == "unsupported"
    assert metadata["dry_run_result"]["stderr"] == stderr
    assert metadata["decision_guidance"] == "guidance unavailable"
    assert metadata["decision"] is None
    assert approval_required.value.metadata == {"plan_id": metadata["id"]}
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_일반_dry_run_오류를_미지원으로_오인하지_않는다(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(
        hook,
        "run_kubectl",
        lambda *args, **kwargs: KubectlResult(
            command="kubectl dry-run",
            stdout="",
            stderr="dry-run request forbidden\n",
            success=False,
        ),
    )

    with pytest.raises(ToolFailed, match="dry-run failed"):
        await hook.guardrail(
            _ctx(),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=AsyncMock(),
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "FAILED"
    assert metadata["failure_reason"] == "DRY_RUN_FAILED"
    assert metadata["dry_run_result"]["status"] == "failed"


@pytest.mark.asyncio
async def test_ToolApproved_재개는_기존_Plan의_handler를_한번_실행한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    handler = AsyncMock(
        return_value=KubectlResult(
            command="kubectl scale deployment nginx --replicas=3 -n study",
            stdout="scaled\n",
            stderr="",
            success=True,
            exit_code=0,
        )
    )

    result = await hook.guardrail(
        _ctx(approved=True),
        call=_call(),
        tool_def=None,
        args=BASE_ARGS,
        handler=handler,
    )

    assert result.success is True
    handler.assert_awaited_once_with(BASE_ARGS)
    assert len(successful_dry_run) == 1
    successful_guidance.assert_awaited_once()
    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["decision"]["approved"] is True
    assert metadata["status"] == "APPLIED"
    assert metadata["args"] == {
        "kind": "deployment",
        "name": "nginx",
        "replicas": 3,
        "namespace": "study",
    }
    assert metadata["execution_result"]["success"] is True
    assert metadata["execution_result"]["stdout"] == "scaled\n"
    assert metadata["execution_result"]["stderr"] == ""
    assert metadata["execution_result"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_DESTRUCTIVE도_단일_승인_후_handler를_한번_실행한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    call = _call("delete_resource")
    await _create_pending_plan(call=call, args=DELETE_ARGS)
    handler = AsyncMock(
        return_value=KubectlResult(
            command="kubectl delete deployment nginx -n study",
            stdout="deployment.apps/nginx deleted\n",
            stderr="",
            success=True,
            exit_code=0,
        )
    )

    await hook.guardrail(
        _ctx(approved=True),
        call=call,
        tool_def=None,
        args=DELETE_ARGS,
        handler=handler,
    )

    handler.assert_awaited_once_with(DELETE_ARGS)
    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["risk_level"] == "destructive"
    assert metadata["decision"]["approved"] is True
    assert datetime.fromisoformat(metadata["decision"]["at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_ToolApproved에_해당하는_Plan이_없으면_handler를_실행하지_않는다(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="Action Plan not found"):
        await hook.guardrail(
            _ctx(approved=True),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=handler,
        )

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_예외는_failed로_기록하고_원래_예외를_유지한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    handler = AsyncMock(side_effect=RuntimeError("cluster disconnected"))

    with pytest.raises(RuntimeError, match="cluster disconnected"):
        await hook.guardrail(
            _ctx(approved=True),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=handler,
        )

    handler.assert_awaited_once_with(BASE_ARGS)
    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "FAILED"
    assert metadata["failure_reason"] == "EXECUTION_FAILED"
    assert metadata["execution_result"]["success"] is False
    assert metadata["execution_result"]["stdout"] == ""
    assert metadata["execution_result"]["stderr"] == "cluster disconnected"
    assert metadata["execution_result"]["exit_code"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tool", "args", "command", "risk", "target"])
async def test_승인_후_요청이_달라지면_handler를_실행하지_않는다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance, field
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    ctx = _ctx(approved=True)
    call = _call()
    args = BASE_ARGS

    if field == "tool":
        call = _call("rollout_restart")
    elif field == "args":
        args = {**BASE_ARGS, "replicas": 4}
    elif field == "command":
        original_assemble = hook.assemble
        monkeypatch.setattr(
            hook,
            "assemble",
            lambda tool_name, values: [*original_assemble(tool_name, values), "--changed"],
        )
    elif field == "risk":
        monkeypatch.setitem(
            hook.RISK_STICKERS,
            "scale_resource",
            hook.RISK_STICKERS["delete_resource"],
        )
    else:
        ctx.deps.context = "other-cluster"

    handler = AsyncMock()
    with pytest.raises(ToolFailed, match=f"approved request mismatch: {field}"):
        await hook.guardrail(
            ctx,
            call=call,
            tool_def=None,
            args=args,
            handler=handler,
        )

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_manifest_원문이_달라지면_해시_불일치로_실행하지_않는다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    original = {
        "manifest_yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx\n",
        "namespace": "study",
        "intent": "Pod를 적용한다.",
        "expected_effects": ["Pod가 생성된다."],
        "side_effects": ["노드 자원을 사용한다."],
    }
    await _create_pending_plan(call=_call("apply_manifest"), args=original)
    changed = {
        **original,
        "manifest_yaml": original["manifest_yaml"] + "  labels:\n    app: changed\n",
    }
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="approved request mismatch: args") as exc:
        await hook.guardrail(
            _ctx(approved=True),
            call=_call("apply_manifest"),
            tool_def=None,
            args=changed,
            handler=handler,
        )

    handler.assert_not_awaited()
    assert changed["manifest_yaml"] not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["APPLIED", "REJECTED", "STALE"])
async def test_terminal_Plan은_handler를_실행하지_않는다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance, status
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    plan = action_plan.ActionPlan.load(next(tmp_path.glob("*.md")))
    plan.mark(status)
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="not ready for execution"):
        await hook.guardrail(
            _ctx(approved=True),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=handler,
        )

    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("success", "stdout", "stderr", "exit_code"),
    [
        (True, "scaled\n", "", 0),
        (False, "", "deployment not found\n", 1),
    ],
)
async def test_같은_Deferred_결과를_재전달하면_handler_대신_저장된_결과를_돌려준다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance,
    success, stdout, stderr, exit_code,
):
    """재개 실패 뒤 /resume 이 같은 승인으로 다시 오는 경로 (DURO-66).
    kubectl 은 다시 돌지 않고, LLM 은 첫 실행과 같은 결과를 본다 — 성공이든 실패든."""
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    result = KubectlResult(
        command="kubectl --context kind-dev scale deployment nginx --replicas=3 -n study",
        stdout=stdout,
        stderr=stderr,
        success=success,
        exit_code=exit_code,
    )
    await hook.guardrail(
        _ctx(approved=True),
        call=_call(),
        tool_def=None,
        args=BASE_ARGS,
        handler=AsyncMock(return_value=result),
    )
    duplicate_handler = AsyncMock()

    replayed = await hook.guardrail(
        _ctx(approved=True),
        call=_call(),
        tool_def=None,
        args=BASE_ARGS,
        handler=duplicate_handler,
    )

    duplicate_handler.assert_not_awaited()
    assert isinstance(replayed, KubectlResult)          # collect_steps 의 isinstance 검사를 통과해야 함
    assert replayed == result                            # command 까지 run_kubectl 과 같은 모양으로 복원
    assert len(successful_dry_run) == 1                  # dry-run 도 다시 돌지 않음
    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == ("APPLIED" if success else "FAILED")


@pytest.mark.asyncio
async def test_승인_기록만_있고_실행_기록이_없으면_실행_여부_불명으로_실패한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    """승인 기록 뒤·실행 기록 전에 프로세스가 죽은 경우 — kubectl 이 돌았는지 모르므로
    다시 돌리지도, 성공했다고 하지도 않는다."""
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    plan = action_plan.ActionPlan.load(next(tmp_path.glob("*.md")))
    plan.record_decision(approved=True)
    handler = AsyncMock()

    with pytest.raises(ToolFailed, match="실행 여부를 확인할 수 없다") as exc:
        await hook.guardrail(
            _ctx(approved=True),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=handler,
        )

    handler.assert_not_awaited()
    assert plan.id in str(exc.value)


@pytest.mark.asyncio
async def test_handler_non_zero_결과는_failed로_기록한다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    result = KubectlResult(
        command="kubectl scale deployment nginx --replicas=3 -n study",
        stdout="",
        stderr="deployment not found\n",
        success=False,
        exit_code=1,
    )

    returned = await hook.guardrail(
        _ctx(approved=True),
        call=_call(),
        tool_def=None,
        args=BASE_ARGS,
        handler=AsyncMock(return_value=result),
    )

    assert returned is result
    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "FAILED"
    assert metadata["failure_reason"] == "EXECUTION_FAILED"
    assert metadata["execution_result"]["success"] is False
    assert metadata["execution_result"]["stderr"] == "deployment not found\n"
    assert metadata["execution_result"]["exit_code"] == 1


@pytest.mark.asyncio
async def test_실패_기록_오류가_handler의_원래_예외를_숨기지_않는다(
    monkeypatch, tmp_path, successful_dry_run, successful_guidance, caplog
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    monkeypatch.setattr(
        action_plan.ActionPlan,
        "record_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="cluster disconnected"):
            await hook.guardrail(
                _ctx(approved=True),
                call=_call(),
                tool_def=None,
                args=BASE_ARGS,
                handler=AsyncMock(side_effect=RuntimeError("cluster disconnected")),
            )

    assert "disk full" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("success", "stdout", "stderr", "exit_code", "expected_stderr"),
    [
        (
            True,
            "scaled\n",
            "",
            0,
            "[guardrail] 실행 결과를 Action Plan에 기록하지 못했습니다.\n"
            "동일 요청의 재실행은 차단되었습니다.",
        ),
        (
            False,
            "",
            "deployment not found\n",
            1,
            "deployment not found\n\n"
            "[guardrail] 실행 결과를 Action Plan에 기록하지 못했습니다.\n"
            "동일 요청의 재실행은 차단되었습니다.",
        ),
    ],
)
async def test_기록_실패는_원래_결과에_경고하고_재실행을_차단한다(
    monkeypatch,
    tmp_path,
    successful_dry_run,
    successful_guidance,
    caplog,
    success,
    stdout,
    stderr,
    exit_code,
    expected_stderr,
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    await _create_pending_plan()
    result = KubectlResult(
        command="kubectl scale deployment nginx --replicas=3 -n study",
        stdout=stdout,
        stderr=stderr,
        success=success,
        exit_code=exit_code,
    )
    monkeypatch.setattr(
        action_plan.ActionPlan,
        "record_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with caplog.at_level(logging.ERROR):
        returned = await hook.guardrail(
            _ctx(approved=True),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=AsyncMock(return_value=result),
        )

    assert returned.success is success
    assert returned.stdout == stdout
    assert returned.exit_code == exit_code
    assert returned.stderr == expected_stderr
    assert "disk full" not in returned.stderr
    assert "disk full" in caplog.text

    # 승인은 기록됐는데 실행 결과 기록이 실패한 상태 → 재전달 시 "실행 여부 불명" 으로 차단
    duplicate_handler = AsyncMock()
    with pytest.raises(ToolFailed, match="실행 여부를 확인할 수 없다"):
        await hook.guardrail(
            _ctx(approved=True),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=duplicate_handler,
        )

    duplicate_handler.assert_not_awaited()
