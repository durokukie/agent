import logging
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


def _ctx(*, namespace="study", approved=False):
    return SimpleNamespace(
        deps=SimpleNamespace(
            context="kind-dev",
            namespace=namespace,
            skill=SimpleNamespace(name="실습"),
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


@pytest.fixture
def successful_dry_run(monkeypatch):
    calls = []

    def fake_run(args, *, context, dry_run=False, stdin=None, timeout=30):
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
    assert metadata["target"] == {
        "context": "kind-dev",
        "namespace": "study",
        "resources": [{"kind": "Pod", "name": "nginx"}],
    }
    assert metadata["command"] == ["apply", "-f", "-", "-n", "study"]
    assert successful_dry_run[0]["args"] == ["apply", "-f", "-", "-n", "study"]
    assert successful_dry_run[0]["stdin"] == args["manifest_yaml"]


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
    assert metadata["status"] == "draft"
    assert metadata["approval"] is None
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
    assert metadata["status"] == "draft"
    assert metadata["approval"] is None
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
    assert metadata["status"] == "failed"
    assert metadata["dry_run_result"]["status"] == "failed"
    assert metadata["dry_run_result"]["stdout"] == ""
    assert metadata["dry_run_result"]["stderr"] == "deployment nginx not found\n"
    assert metadata["approval"] is None
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
    assert metadata["status"] == "failed"
    assert metadata["dry_run_result"]["status"] == "failed"
    assert metadata["dry_run_result"]["stdout"] == ""
    assert expected_stderr in metadata["dry_run_result"]["stderr"]
    assert metadata["approval"] is None
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
    assert metadata["status"] == "draft"
    assert metadata["dry_run_result"]["status"] == "unsupported"
    assert metadata["dry_run_result"]["stderr"] == stderr
    assert metadata["decision_guidance"] == "guidance unavailable"
    assert metadata["approval"] is None
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
    assert metadata["status"] == "failed"
    assert metadata["dry_run_result"]["status"] == "failed"
