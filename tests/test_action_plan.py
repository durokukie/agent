from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile as real_named_temporary_file
from threading import Barrier

import pytest
import yaml

from kukie.guardrail import action_plan
from kukie.guardrail.action_plan import ActionPlan


def _read_plan(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    frontmatter, separator, body = text.removeprefix("---\n").partition("\n---\n")
    assert separator
    return yaml.safe_load(frontmatter), body


def _create_plan(monkeypatch, tmp_path: Path, tool: str = "scale_resource") -> ActionPlan:
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    return ActionPlan.create_draft(
        tool=tool,
        command=["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        risk="caution",
        skill="실습",
        target={
            "context": "minikube",
            "namespace": "study",
            "kind": "deployment",
            "name": "nginx",
        },
        intent="nginx 실습 환경의 레플리카를 늘린다.",
        expected_effects=["nginx Deployment의 레플리카가 3개로 변경된다."],
        side_effects=["추가 Pod가 노드 자원을 사용한다."],
    )


def test_create_draft_writes_frontmatter_and_body(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    metadata, body = _read_plan(plan.path)

    assert plan.id.startswith("ap-")
    assert plan.path == tmp_path / f"{plan.id}.md"
    assert metadata == {
        "id": plan.id,
        "created_at": metadata["created_at"],
        "tool": "scale_resource",
        "skill": "실습",
        "target": {
            "context": "minikube",
            "namespace": "study",
            "kind": "deployment",
            "name": "nginx",
        },
        "command": ["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        "risk_level": "caution",
        "status": "draft",
        "dry_run_result": None,
        "approval": None,
        "execution_result": None,
    }
    assert datetime.fromisoformat(metadata["created_at"]).tzinfo is not None
    assert "# Intent\n\nnginx 실습 환경의 레플리카를 늘린다." in body
    assert "## Expected Effects\n\n- nginx Deployment의 레플리카가 3개로 변경된다." in body
    assert "## Side Effects\n\n- 추가 Pod가 노드 자원을 사용한다." in body


def test_create_draft_uses_minute_and_safe_tool_in_unique_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(
        action_plan,
        "_utc_now",
        lambda: "2026-08-17T15:30:45.123456+00:00",
    )

    first = _create_plan(monkeypatch, tmp_path, tool="scale resource/now")
    second = _create_plan(monkeypatch, tmp_path, tool="scale resource/now")
    metadata, _ = _read_plan(first.path)

    assert first.id == "ap-260817-1530-scale-resource-now"
    assert second.id == "ap-260817-1530-scale-resource-now-2"
    assert first.path.name == f"{first.id}.md"
    assert second.path.name == f"{second.id}.md"
    assert metadata["tool"] == "scale resource/now"
    assert metadata["created_at"] == "2026-08-17T15:30:45.123456+00:00"


def test_create_draft_reserves_unique_filename_atomically(monkeypatch, tmp_path):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(
        action_plan,
        "_utc_now",
        lambda: "2026-08-17T15:30:45.123456+00:00",
    )
    candidate = tmp_path / "ap-260817-1530-scale-resource.md"
    barrier = Barrier(2)
    original_exists = Path.exists

    def raced_exists(path):
        exists = original_exists(path)
        if path == candidate:
            barrier.wait(timeout=2)
        return exists

    def create_plan(label):
        return ActionPlan.create_draft(
            tool="scale resource",
            command=["scale", label],
            risk="caution",
            skill="실습",
            target={"name": label},
            intent=label,
            expected_effects=[],
            side_effects=[],
        )

    monkeypatch.setattr(Path, "exists", raced_exists)
    with ThreadPoolExecutor(max_workers=2) as executor:
        plans = list(executor.map(create_plan, ["first", "second"]))

    assert {plan.id for plan in plans} == {
        "ap-260817-1530-scale-resource",
        "ap-260817-1530-scale-resource-2",
    }
    assert {path.name for path in tmp_path.iterdir()} == {
        "ap-260817-1530-scale-resource.md",
        "ap-260817-1530-scale-resource-2.md",
    }
    assert {
        tuple(_read_plan(plan.path)[0]["command"])
        for plan in plans
    } == {("scale", "first"), ("scale", "second")}


def test_record_methods_update_frontmatter_and_preserve_body(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)
    _, original_body = _read_plan(plan.path)

    plan.record_dry_run("dry-run ok", True)
    plan.record_approval("single")
    plan.record_result("scaled", True)

    metadata, body = _read_plan(plan.path)
    assert metadata["dry_run_result"]["success"] is True
    assert metadata["dry_run_result"]["output"] == "dry-run ok"
    assert datetime.fromisoformat(metadata["dry_run_result"]["at"]).tzinfo is not None
    assert metadata["approval"]["mode"] == "single"
    assert datetime.fromisoformat(metadata["approval"]["at"]).tzinfo is not None
    assert metadata["execution_result"]["success"] is True
    assert metadata["execution_result"]["output"] == "scaled"
    assert datetime.fromisoformat(metadata["execution_result"]["at"]).tzinfo is not None
    assert body == original_body


def test_dry_run_failure_is_recorded_and_plan_is_kept(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    plan.record_dry_run("deployment nginx not found", False)
    plan.mark("failed")

    metadata, _ = _read_plan(plan.path)
    assert metadata["status"] == "failed"
    assert metadata["dry_run_result"]["success"] is False
    assert metadata["dry_run_result"]["output"] == "deployment nginx not found"
    assert datetime.fromisoformat(metadata["dry_run_result"]["at"]).tzinfo is not None
    assert metadata["execution_result"] is None
    assert plan.path.exists()


@pytest.mark.parametrize("status", ["executed", "failed", "rejected"])
def test_mark_accepts_final_statuses(monkeypatch, tmp_path, status):
    plan = _create_plan(monkeypatch, tmp_path)

    plan.mark(status)

    metadata, _ = _read_plan(plan.path)
    assert metadata["status"] == status


def test_mark_rejects_unknown_status(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="invalid Action Plan status"):
        plan.mark("approved")


def test_update_rejects_invalid_frontmatter(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.path.write_text("broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid Action Plan frontmatter"):
        plan.mark("failed")


def test_write_cleans_up_temp_file_when_write_fails(monkeypatch, tmp_path):
    real_temp = real_named_temporary_file("w", dir=tmp_path, delete=False)

    class FailingTemporaryFile:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            real_temp.close()

        @property
        def name(self):
            return real_temp.name

        def write(self, content):
            raise OSError("simulated write failure")

    monkeypatch.setattr(action_plan, "NamedTemporaryFile", lambda *args, **kwargs: FailingTemporaryFile())
    plan = ActionPlan("ap-test", tmp_path / "plan.md")

    with pytest.raises(OSError, match="simulated write failure"):
        plan._write({}, "body")

    assert list(tmp_path.iterdir()) == []
