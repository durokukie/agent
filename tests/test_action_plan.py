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
        call_id="call-123",
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


def test_초안은_모든_필드를_frontmatter에만_저장한다(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    metadata, body = _read_plan(plan.path)

    assert plan.id.startswith("ap-")
    assert plan.path == tmp_path / f"{plan.id}.md"
    assert metadata == {
        "id": plan.id,
        "created_at": metadata["created_at"],
        "call_id": "call-123",
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
        "intent": "nginx 실습 환경의 레플리카를 늘린다.",
        "expected_effects": ["nginx Deployment의 레플리카가 3개로 변경된다."],
        "side_effects": ["추가 Pod가 노드 자원을 사용한다."],
        "dry_run_result": None,
        "decision_guidance": None,
        "approval": None,
        "execution_result": None,
    }
    assert datetime.fromisoformat(metadata["created_at"]).tzinfo is not None
    assert body == ""


def test_create_draft_keeps_structured_fields_in_memory(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    assert plan.tool == "scale_resource"
    assert plan.call_id == "call-123"
    assert plan.skill == "실습"
    assert plan.target["name"] == "nginx"
    assert plan.command[-1] == "study"
    assert plan.risk_level == "caution"
    assert plan.status == "draft"
    assert plan.intent == "nginx 실습 환경의 레플리카를 늘린다."
    assert plan.expected_effects == ["nginx Deployment의 레플리카가 3개로 변경된다."]
    assert plan.side_effects == ["추가 Pod가 노드 자원을 사용한다."]
    assert plan.dry_run_result is None
    assert plan.decision_guidance is None
    assert plan.approval is None
    assert plan.execution_result is None


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
            call_id=f"call-{label}",
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


def test_record_메서드는_frontmatter를_갱신하고_본문을_비워둔다(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)

    plan.record_dry_run("succeeded", "dry-run ok", "")
    plan.record_approval("single")
    plan.record_result("scaled", True)

    metadata, body = _read_plan(plan.path)
    assert metadata["dry_run_result"]["status"] == "succeeded"
    assert metadata["dry_run_result"]["stdout"] == "dry-run ok"
    assert metadata["dry_run_result"]["stderr"] == ""
    assert datetime.fromisoformat(metadata["dry_run_result"]["at"]).tzinfo is not None
    assert metadata["approval"]["mode"] == "single"
    assert datetime.fromisoformat(metadata["approval"]["at"]).tzinfo is not None
    assert metadata["execution_result"]["success"] is True
    assert metadata["execution_result"]["output"] == "scaled"
    assert datetime.fromisoformat(metadata["execution_result"]["at"]).tzinfo is not None
    assert body == ""
    assert plan.dry_run_result == metadata["dry_run_result"]
    assert plan.approval == metadata["approval"]
    assert plan.execution_result == metadata["execution_result"]


@pytest.mark.parametrize(
    ("status", "stdout", "stderr"),
    [
        ("succeeded", "deployment.apps/nginx configured\n", ""),
        ("failed", "", "deployment nginx not found\n"),
        ("unsupported", "", "server does not support dry run\n"),
    ],
)
def test_dry_run_상태와_출력_stream을_분리해_저장한다(
    monkeypatch, tmp_path, status, stdout, stderr
):
    plan = _create_plan(monkeypatch, tmp_path)

    plan.record_dry_run(status, stdout, stderr)

    metadata, _ = _read_plan(plan.path)
    assert metadata["dry_run_result"]["status"] == status
    assert metadata["dry_run_result"]["stdout"] == stdout
    assert metadata["dry_run_result"]["stderr"] == stderr
    assert datetime.fromisoformat(metadata["dry_run_result"]["at"]).tzinfo is not None


def test_정의되지_않은_dry_run_상태를_거부한다(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="invalid dry-run status"):
        plan.record_dry_run("skipped", "", "")


def test_dry_run_failure_is_recorded_and_plan_is_kept(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    plan.record_dry_run("failed", "", "deployment nginx not found")
    plan.mark("failed")

    metadata, _ = _read_plan(plan.path)
    assert metadata["status"] == "failed"
    assert metadata["dry_run_result"]["status"] == "failed"
    assert metadata["dry_run_result"]["stdout"] == ""
    assert metadata["dry_run_result"]["stderr"] == "deployment nginx not found"
    assert datetime.fromisoformat(metadata["dry_run_result"]["at"]).tzinfo is not None
    assert metadata["execution_result"] is None
    assert plan.path.exists()
    assert plan.status == "failed"
    assert plan.dry_run_result == metadata["dry_run_result"]


@pytest.mark.parametrize("status", ["executed", "failed", "rejected"])
def test_mark_accepts_final_statuses(monkeypatch, tmp_path, status):
    plan = _create_plan(monkeypatch, tmp_path)

    plan.mark(status)

    metadata, _ = _read_plan(plan.path)
    assert metadata["status"] == status
    assert plan.status == status


def test_mark_rejects_unknown_status(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="invalid Action Plan status"):
        plan.mark("approved")


def test_validate_for_decision_guidance_uses_in_memory_fields(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.record_dry_run("succeeded", "dry-run ok", "")
    plan.path.write_text("broken", encoding="utf-8")

    plan.validate_for_decision_guidance()


def test_write_cleans_up_temp_file_when_write_fails(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.path.unlink()
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

    with pytest.raises(OSError, match="simulated write failure"):
        plan._write()

    assert list(tmp_path.iterdir()) == []


def test_update_rolls_back_object_when_write_fails(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    def fail_write():
        raise OSError("simulated write failure")

    monkeypatch.setattr(plan, "_write", fail_write)

    with pytest.raises(OSError, match="simulated write failure"):
        plan.mark("failed")

    assert plan.status == "draft"


def test_validate_for_decision_guidance_requires_successful_dry_run(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="dry_run_result.status must be succeeded"):
        plan.validate_for_decision_guidance()

    plan.record_dry_run("unsupported", "", "server does not support dry run")

    with pytest.raises(ValueError, match="dry_run_result.status must be succeeded"):
        plan.validate_for_decision_guidance()

    plan.record_dry_run("failed", "", "dry-run rejected")

    with pytest.raises(ValueError, match="dry_run_result.status must be succeeded"):
        plan.validate_for_decision_guidance()


def test_Markdown_표시는_각_설명_필드를_한_번만_조합한다(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.record_dry_run("succeeded", "dry-run ok", "")

    markdown = plan.render_markdown(include_decision_guidance=False)

    assert "decision_guidance" not in markdown
    assert "scale_resource" in markdown
    assert "--replicas=3" in markdown
    assert "dry-run ok" in markdown
    assert markdown.count(plan.intent) == 1
    assert markdown.count(plan.expected_effects[0]) == 1
    assert markdown.count(plan.side_effects[0]) == 1


def test_validate_for_decision_guidance_names_empty_object_field(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.record_dry_run("succeeded", "dry-run ok", "")
    plan.intent = ""

    with pytest.raises(ValueError, match="missing=intent"):
        plan.validate_for_decision_guidance()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tool", ""),
        ("skill", ""),
        ("target", {}),
        ("command", []),
        ("risk_level", ""),
    ],
)
def test_validate_for_decision_guidance_names_missing_required_field(
    monkeypatch, tmp_path, key, value
):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.record_dry_run("succeeded", "dry-run ok", "")
    setattr(plan, key, value)

    with pytest.raises(ValueError, match=f"missing={key}"):
        plan.validate_for_decision_guidance()


def test_validate_for_decision_guidance_fails_on_first_missing_field(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.record_dry_run("succeeded", "dry-run ok", "")
    plan.tool = ""
    plan.intent = ""

    with pytest.raises(ValueError, match=r"missing=tool$"):
        plan.validate_for_decision_guidance()


def test_validate_for_decision_guidance_rejects_wrong_lifecycle_state(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.record_dry_run("succeeded", "dry-run ok", "")
    plan.record_approval("single")

    with pytest.raises(ValueError, match="approval must be empty"):
        plan.validate_for_decision_guidance()


def test_record_decision_guidance_persists_text(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    plan.record_decision_guidance("  배포 시간과 롤백 기준을 확인한다.  ")

    metadata, _ = _read_plan(plan.path)
    assert metadata["decision_guidance"] == "배포 시간과 롤백 기준을 확인한다."
    assert plan.decision_guidance == "배포 시간과 롤백 기준을 확인한다."


def test_record_decision_guidance_rejects_empty_or_overwrite(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="decision guidance must not be empty"):
        plan.record_decision_guidance("   ")

    plan.record_decision_guidance("첫 판단")

    with pytest.raises(ValueError, match="decision guidance already exists"):
        plan.record_decision_guidance("두 번째 판단")


def test_Plan_파일에서_구조화된_객체를_복원한다(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)
    plan.record_dry_run("succeeded", "dry-run ok", "")
    plan.record_decision_guidance("배포 시간과 롤백 기준을 확인한다.")

    loaded = ActionPlan.load(plan.path)

    assert loaded.id == plan.id
    assert loaded.path == plan.path
    assert loaded.created_at == plan.created_at
    assert loaded.call_id == plan.call_id
    assert loaded.tool == plan.tool
    assert loaded.skill == plan.skill
    assert loaded.target == plan.target
    assert loaded.command == plan.command
    assert loaded.risk_level == plan.risk_level
    assert loaded.status == "draft"
    assert loaded.intent == plan.intent
    assert loaded.expected_effects == plan.expected_effects
    assert loaded.side_effects == plan.side_effects
    assert loaded.dry_run_result == plan.dry_run_result
    assert loaded.decision_guidance == plan.decision_guidance


def test_예약_제목과_여러_줄_effect도_손실_없이_복원한다(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    intent = "변경 결과\n\n## Expected Effects\n\n이 제목도 의도의 일부다."
    expected_effects = ["상태를 확인한다.\n- Ready Pod는 3개여야 한다."]
    side_effects = ["롤링 업데이트가 진행된다.\n- Pod가 교체된다."]
    plan = ActionPlan.create_draft(
        call_id="call-special",
        tool="scale_resource",
        command=["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        risk="caution",
        skill="실습",
        target={"context": "minikube", "namespace": "study"},
        intent=intent,
        expected_effects=expected_effects,
        side_effects=side_effects,
    )

    loaded = ActionPlan.load(plan.path)

    assert loaded.intent == intent
    assert loaded.expected_effects == expected_effects
    assert loaded.side_effects == side_effects


def test_새_Plan은_Markdown_본문을_복원에_사용하지_않는다(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)
    metadata, _ = _read_plan(plan.path)
    plan.path.write_text(
        "---\n"
        + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
        + "---\n임의로 추가된 본문\n",
        encoding="utf-8",
    )

    loaded = ActionPlan.load(plan.path)

    assert loaded.intent == plan.intent
    assert loaded.expected_effects == plan.expected_effects
    assert loaded.side_effects == plan.side_effects


def test_설명_frontmatter_필드가_누락되면_거부한다(monkeypatch, tmp_path):
    plan = _create_plan(monkeypatch, tmp_path)
    metadata, body = _read_plan(plan.path)
    metadata.update(
        intent=plan.intent,
        expected_effects=plan.expected_effects,
        side_effects=plan.side_effects,
    )
    metadata.pop("intent")
    plan.path.write_text(
        "---\n"
        + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
        + "---\n"
        + body,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid Action Plan frontmatter"):
        ActionPlan.load(plan.path)


def test_설명_frontmatter_필드의_타입이_잘못되면_거부한다(
    monkeypatch, tmp_path
):
    plan = _create_plan(monkeypatch, tmp_path)
    metadata, body = _read_plan(plan.path)
    metadata.update(
        intent=plan.intent,
        expected_effects="목록이 아닌 문자열",
        side_effects=plan.side_effects,
    )
    plan.path.write_text(
        "---\n"
        + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
        + "---\n"
        + body,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid Action Plan frontmatter"):
        ActionPlan.load(plan.path)


def test_call_id로_일치하는_Plan을_복원한다(monkeypatch, tmp_path):
    first = _create_plan(monkeypatch, tmp_path)
    second = ActionPlan.create_draft(
        call_id="call-456",
        tool="delete_resource",
        command=["delete", "pod", "old", "-n", "study"],
        risk="destructive",
        skill="실습",
        target={
            "context": "minikube",
            "namespace": "study",
            "kind": "pod",
            "name": "old",
        },
        intent="오래된 실습 Pod를 지운다.",
        expected_effects=["Pod가 삭제된다."],
        side_effects=["Pod의 임시 데이터가 사라진다."],
    )

    found = ActionPlan.find_by_call_id("call-456")

    assert found.id == second.id
    assert found.call_id == "call-456"
    assert found.path == second.path
    assert found.intent == "오래된 실습 Pod를 지운다."
    assert first.id != found.id


def test_call_id에_일치하는_Plan이_없으면_실패한다(monkeypatch, tmp_path):
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="missing-call"):
        ActionPlan.find_by_call_id("missing-call")
