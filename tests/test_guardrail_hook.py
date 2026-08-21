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
async def test_위험도는_RISK_STICKERS의_고정값만_사용한다(
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
async def test_보호_namespace도_CAUTION_위험도를_유지한다(
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
async def test_apply_manifest는_세션_namespace를_Plan에_사용한다(
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
async def test_dry_run_성공은_실행_없이_Plan에_기록한다(
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
async def test_Plan은_server_dry_run_전에_저장된다(monkeypatch, tmp_path):
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
async def test_dry_run_실패는_Plan을_failed로_남기고_중단한다(monkeypatch, tmp_path):
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
async def test_dry_run_미지원은_다음_승인을_위해_draft를_유지한다(
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
