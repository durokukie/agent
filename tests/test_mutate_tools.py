"""변경 툴 4종 본체 검증 (DURO-45) — 조립 결과와 실행 방식만 본다. 실제 kubectl은 안 부른다.

훅과의 계약:
- 본체는 자기 인자로 assemble()을 부른다. 훅도 같은 assemble()을 부르므로 결과가 동일해야 한다.
- 조립은 순수 함수 — 같은 입력이면 항상 같은 출력.
- apply의 매니페스트는 args가 아니라 stdin으로 간다 (임시파일 없음).
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kukie.kubectl import KubectlResult, assemble
from kukie.tools import mutate

INTENT = dict(intent="테스트 의도", expected_effects=["영향"], side_effects=["부작용"])


def _ctx(context="kind-dev", namespace="study"):
    return SimpleNamespace(deps=SimpleNamespace(context=context, namespace=namespace))


@pytest.fixture
def captured():
    """run_kubectl을 가로채 조립된 args와 stdin을 기록한다."""
    box = {}

    def fake_run(args, *, context, dry_run=False, stdin=None, timeout=30):
        box.update(args=args, context=context, dry_run=dry_run, stdin=stdin)
        return KubectlResult(command=" ".join(args), stdout="", stderr="", success=True)

    with patch.object(mutate, "run_kubectl", fake_run):
        yield box


def test_delete_resource_조립(captured):
    mutate.delete_resource(_ctx(), "deployment", "nginx", "study", **INTENT)
    assert captured["args"] == ["delete", "deployment", "nginx", "-n", "study"]
    assert captured["context"] == "kind-dev"
    assert captured["dry_run"] is False          # 본체는 진짜 실행. dry-run은 훅 몫


def test_scale_resource_조립(captured):
    mutate.scale_resource(_ctx(), "deployment", "nginx", 3, "study", **INTENT)
    assert captured["args"] == ["scale", "deployment", "nginx", "--replicas=3", "-n", "study"]


def test_rollout_restart_조립(captured):
    mutate.rollout_restart(_ctx(), "deployment", "nginx", "study", **INTENT)
    assert captured["args"] == ["rollout", "restart", "deployment/nginx", "-n", "study"]


def test_apply_manifest는_stdin으로_넘기고_args에_YAML을_넣지_않는다(captured):
    yaml = "apiVersion: v1\nkind: Pod\nmetadata:\n  name: x"
    mutate.apply_manifest(_ctx(), yaml, **INTENT)
    assert captured["args"] == ["apply", "-f", "-", "-n", "study"]   # 세션 기본 ns
    assert captured["stdin"] == yaml
    assert yaml not in " ".join(captured["args"])


def test_apply_manifest_namespace_명시(captured):
    mutate.apply_manifest(_ctx(), "kind: Pod", namespace="prod", **INTENT)
    assert captured["args"][-2:] == ["-n", "prod"]


def test_intent_3종은_명령에_들어가지_않는다(captured):
    """intent/expected_effects/side_effects는 승인 화면·Plan 전용 — kubectl 인자 오염 금지."""
    mutate.delete_resource(_ctx(), "deployment", "nginx", "study",
                           intent="비밀문장", expected_effects=["E"], side_effects=["S"])
    joined = " ".join(captured["args"])
    assert "비밀문장" not in joined and "E" not in captured["args"] and "S" not in captured["args"]


@pytest.mark.parametrize("tool,args", [
    ("delete_resource",  {"kind": "deployment", "name": "nginx", "namespace": "study"}),
    ("scale_resource",   {"kind": "deployment", "name": "nginx", "replicas": 3, "namespace": "study"}),
    ("rollout_restart",  {"kind": "deployment", "name": "nginx", "namespace": "study"}),
    ("apply_manifest",   {"namespace": "study"}),
])
def test_assemble은_결정론적이다(tool, args):
    """훅(승인 화면용)과 본체(실행용)가 각각 불러도 같은 결과 — '승인된 명령 = 실행된 명령'의 근거."""
    assert assemble(tool, args) == assemble(tool, args)


def test_모든_변경_툴에_위험도_스티커가_있다():
    for fn in mutate.MUTATE_TOOLS:
        assert fn.__name__ in mutate.RISK_STICKERS


# ── kubectl 옵션 파싱 방어 (CodeRabbit 지적) ─────────────────

@pytest.mark.parametrize("field,args", [
    ("name",      {"kind": "deployment", "name": "--all", "namespace": "study"}),
    ("name",      {"kind": "deployment", "name": "-n", "namespace": "study"}),
    ("kind",      {"kind": "--all", "name": "nginx", "namespace": "study"}),
    ("namespace", {"kind": "deployment", "name": "nginx", "namespace": "--all-namespaces"}),
])
def test_플래그_모양_값은_조립에서_거부된다(field, args):
    """name="--all"이 kubectl 옵션으로 해석돼 전체 삭제되는 것을 차단 (shell=False와 별개 층)."""
    with pytest.raises(ValueError, match=field):
        assemble("delete_resource", args)


def test_툴_본체를_통해서도_플래그_모양_값은_거부된다(captured):
    with pytest.raises(ValueError):
        mutate.delete_resource(_ctx(), "deployment", "--all", "study", **INTENT)
    assert "args" not in captured        # run_kubectl까지 도달하지 못함
