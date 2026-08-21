from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.messages import ToolCallPart

from kukie.guardrail import action_plan, hook
from kukie.kubectl import KubectlResult


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
async def test_invalid_explanation_fields_are_rejected_before_plan(
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
async def test_unregistered_mutation_is_rejected_before_plan(monkeypatch, tmp_path):
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
async def test_missing_risk_sticker_is_rejected_before_plan(monkeypatch, tmp_path):
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
async def test_missing_call_id_is_rejected_before_plan(monkeypatch, tmp_path):
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
async def test_hook_uses_only_fixed_risk_stickers(
    monkeypatch, tmp_path, successful_dry_run, tool_name, args, expected_risk
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    with pytest.raises(NotImplementedError, match="#25"):
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
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_protected_namespace_keeps_fixed_caution_risk(
    monkeypatch, tmp_path, successful_dry_run
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    args = {**BASE_ARGS, "namespace": "kube-system"}

    with pytest.raises(NotImplementedError, match="#25"):
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
async def test_apply_manifest_uses_session_namespace_in_plan(
    monkeypatch, tmp_path, successful_dry_run
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    args = {
        "manifest_yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx\n",
        "namespace": None,
        "intent": "Pod를 적용한다.",
        "expected_effects": ["Pod가 생성된다."],
        "side_effects": ["노드 자원을 사용한다."],
    }

    with pytest.raises(NotImplementedError, match="#25"):
        await hook.guardrail(
            _ctx(namespace="study"),
            call=_call("apply_manifest"),
            tool_def=None,
            args=args,
            handler=AsyncMock(),
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["target"] == {"context": "kind-dev", "namespace": "study"}
    assert metadata["command"] == ["apply", "-f", "-", "-n", "study"]
    assert successful_dry_run[0]["args"] == ["apply", "-f", "-", "-n", "study"]
    assert successful_dry_run[0]["stdin"] == args["manifest_yaml"]


@pytest.mark.asyncio
async def test_successful_dry_run_is_recorded_without_execution(
    monkeypatch, tmp_path, successful_dry_run
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    handler = AsyncMock()

    with pytest.raises(NotImplementedError, match="#25"):
        await hook.guardrail(
            _ctx(), call=_call(), tool_def=None, args=BASE_ARGS, handler=handler
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "draft"
    assert metadata["approval"] is None
    assert metadata["dry_run_result"]["status"] == "succeeded"
    assert metadata["dry_run_result"]["stdout"] == "ok\n"
    assert metadata["dry_run_result"]["stderr"] == ""
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
async def test_plan_is_saved_before_server_dry_run(monkeypatch, tmp_path):
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

    with pytest.raises(NotImplementedError, match="#25"):
        await hook.guardrail(
            _ctx(),
            call=_call(),
            tool_def=None,
            args=BASE_ARGS,
            handler=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_failed_dry_run_marks_plan_failed_and_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
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
    "stderr",
    [
        "Error from server: admission webhook does not support dry run\n",
        "error: unknown flag: --dry-run\n",
    ],
)
async def test_unsupported_dry_run_remains_draft_for_issue_25(
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

    with pytest.raises(NotImplementedError, match="#25"):
        await hook.guardrail(
            _ctx(), call=_call(), tool_def=None, args=BASE_ARGS, handler=handler
        )

    metadata = _read_plan(next(tmp_path.glob("*.md")))
    assert metadata["status"] == "draft"
    assert metadata["dry_run_result"]["status"] == "unsupported"
    assert metadata["dry_run_result"]["stderr"] == stderr
    assert metadata["approval"] is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_dry_run_error_is_not_treated_as_unsupported(
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
