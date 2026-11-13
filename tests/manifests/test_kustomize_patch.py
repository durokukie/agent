"""Kustomize 앱의 경로 지정 변경(what=patch) — DURO-251.

픽스처 레포의 apps/kustomize/overlays/prod(kukie-gitops-sandbox 의 overlays/prod 와 같은 모양: replicas · images transformer,
기존 patch env.yaml)에 overlay patch 파일을 걸어 본다. 측정(DURO-250)에서 나온 조건 — 접두사 전 이름으로 target,
replicas · images 가 정하는 칸은 그 항목으로, overlay 전체에 붙는 칸·transformers 가 덮는 칸은 원인을 말하며 거절 — 을 확인한다.
진짜 `kustomize build` 로 렌더한다(없으면 건너뛰고 CI 에서는 실패한다).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from kukie_agent.manifests import AppSource, Patch, PatchOperation, Ready, Refused, Target, plan_change
from kukie_agent.manifests.kustomize import merge_patch, original_name
from kukie_agent.manifests.render import kustomize_bin

FIXTURE = Path(__file__).parents[1] / "fixtures" / "manifests"
OVERLAY = "apps/kustomize/overlays/prod"
PROD = AppSource(source_type="Kustomize", path=OVERLAY)
API = Target(api_version="apps/v1", kind="Deployment", name="api", namespace="shop-prod")
SVC = Target(api_version="v1", kind="Service", name="api", namespace="shop-prod")
KUSTOMIZATION = f"{OVERLAY}/kustomization.yaml"
PATCH = f"{OVERLAY}/kukie-deployment-api-patch.yaml"
CONTAINER = "spec.template.spec.containers[api]"

needs_kustomize = pytest.mark.skipif(
    shutil.which(kustomize_bin()) is None and not os.environ.get("CI"),
    reason="kustomize 가 없다 (CI 에서는 깔고 돈다)")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURE, copy)
    return copy


def ops(*items: tuple) -> Patch:
    return Patch(operations=[PatchOperation(op=item[0], path=item[1], value=item[2] if len(item) > 2 else None)
                             for item in items])


def ready(result: Ready | Refused) -> Ready:
    assert isinstance(result, Ready), result
    return result


def refused(result: Ready | Refused, code: str) -> Refused:
    assert isinstance(result, Refused), result
    assert result.code == code, result
    return result


def write(repo: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content, encoding="utf-8")


def overlay(repo: Path, name: str, kustomization: str, **files: str) -> AppSource:
    write(repo, {f"apps/kustomize/overlays/{name}/kustomization.yaml": kustomization,
                 **{f"apps/kustomize/overlays/{name}/{file}": text for file, text in files.items()}})
    return AppSource(source_type="Kustomize", path=f"apps/kustomize/overlays/{name}")


# ── patch 파일과 kustomization ───────────────────────────


@needs_kustomize
def test_overlay_에_patch_파일을_두고_patches_맨_뒤에_건다(repo):
    result = ready(plan_change(repo, PROD, API, ops(
        ("set", f"{CONTAINER}.readinessProbe", {"httpGet": {"path": "/", "port": 80}}))))
    assert sorted(result.files) == sorted([KUSTOMIZATION, PATCH])
    assert not any(path.startswith("apps/kustomize/base") for path in result.files)
    patches = yaml.safe_load(result.files[KUSTOMIZATION])["patches"]
    assert patches[0] == {"path": "env.yaml"}
    # target 은 접두사 전 이름, namespace 는 넣지 않는다 (DURO-250 측정 조건)
    assert patches[-1] == {"path": "kukie-deployment-api-patch.yaml",
                           "target": {"group": "apps", "version": "v1", "kind": "Deployment", "name": "api"}}
    patch = yaml.safe_load(result.files[PATCH])
    assert patch["metadata"] == {"name": "api"}
    assert patch["spec"]["template"]["spec"]["containers"] == [
        {"name": "api", "readinessProbe": {"httpGet": {"path": "/", "port": 80}}}]
    assert any(line.lstrip("+").strip() == "readinessProbe:" for line in result.render_diff.splitlines() if line.startswith("+"))


@needs_kustomize
def test_앞의_patch_가_넣은_환경변수도_지우고_이름_없는_병합_목록은_통째로_바꾼다(repo):
    result = ready(plan_change(repo, PROD, API, ops(
        ("remove", f"{CONTAINER}.env[STAGE]"),
        ("set", f"{CONTAINER}.ports", [{"containerPort": 8080, "protocol": "TCP"}]))))
    container = yaml.safe_load(result.files[PATCH])["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [{"name": "STAGE", "$patch": "delete"}]
    # ports 는 containerPort+protocol 로 병합된다 — 키를 맞추지 않고 통째로 바꾼다 (측정 podinfo R08)
    assert container["ports"] == [{"containerPort": 8080, "protocol": "TCP"}, {"$patch": "replace"}]


@needs_kustomize
def test_칸을_지우면_null_로_적는다(repo):
    result = ready(plan_change(repo, PROD, API, ops(("remove", f"{CONTAINER}.resources.limits"))))
    assert "limits: null" in result.files[PATCH]


@needs_kustomize
def test_같은_리소스를_다시_바꾸면_그_patch_파일을_다시_쓴다(repo):
    first = ready(plan_change(repo, PROD, API, ops(("set", f"{CONTAINER}.readinessProbe", {"tcpSocket": {"port": 80}}))))
    write(repo, first.files)
    second = ready(plan_change(repo, PROD, API, ops(("set", f"{CONTAINER}.readinessProbe.periodSeconds", 5),
                                                    ("set", "spec.minReadySeconds", 3))))
    assert list(second.files) == [PATCH]                     # kustomization 은 이미 걸려 있다
    patch = yaml.safe_load(second.files[PATCH])
    assert patch["spec"]["minReadySeconds"] == 3
    assert patch["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] == {
        "tcpSocket": {"port": 80}, "periodSeconds": 5}       # 앞 요청도 남는다
    assert yaml.safe_load((repo / KUSTOMIZATION).read_text())["patches"].count(
        yaml.safe_load(first.files[KUSTOMIZATION])["patches"][-1]) == 1


@needs_kustomize
def test_뒤에_다른_patch_가_걸렸으면_다시_쓸_때_맨_뒤로_옮긴다(repo):
    from kukie_agent.manifests import Env
    first = ready(plan_change(repo, PROD, API, ops(("set", f"{CONTAINER}.env[STAGE].value", "a"))))
    write(repo, first.files)
    second = ready(plan_change(repo, PROD, API, Env(container="api", values={"STAGE": "b"})))
    write(repo, second.files)
    third = ready(plan_change(repo, PROD, API, ops(("set", f"{CONTAINER}.env[STAGE].value", "c"))))
    paths = [p["path"] for p in yaml.safe_load(third.files[KUSTOMIZATION])["patches"]]
    assert paths == ["env.yaml", "kukie-deployment-api-env.yaml", "kukie-deployment-api-patch.yaml"]


@needs_kustomize
def test_Service_같은_다른_종류도_된다(repo):
    result = ready(plan_change(repo, PROD, SVC, ops(("set", "spec.type", "NodePort"))))
    assert f"{OVERLAY}/kukie-service-api-patch.yaml" in result.files
    target = yaml.safe_load(result.files[KUSTOMIZATION])["patches"][-1]["target"]
    assert target == {"version": "v1", "kind": "Service", "name": "api"}


# ── 같은 overlay 의 transformer 가 정하는 칸 ──────────────


@needs_kustomize
def test_replicas_가_정하는_레플리카는_replicas_항목을_고친다(repo):
    result = ready(plan_change(repo, PROD, API, ops(("set", "spec.replicas", 4))))
    assert list(result.files) == [KUSTOMIZATION]             # patch 파일은 필요 없다
    assert {"name": "api", "count": 4} in yaml.safe_load(result.files[KUSTOMIZATION])["replicas"]


@needs_kustomize
def test_images_가_정하는_이미지는_images_항목을_고치고_나머지는_patch_로(repo):
    result = ready(plan_change(repo, PROD, API, ops(
        ("set", f"{CONTAINER}.image", "public.ecr.aws/docker/library/nginx:1.30-alpine"),
        ("set", f"{CONTAINER}.imagePullPolicy", "Always"))))
    kustomization = yaml.safe_load(result.files[KUSTOMIZATION])
    assert {"name": "public.ecr.aws/docker/library/nginx", "newTag": "1.30-alpine"} in kustomization["images"]
    container = yaml.safe_load(result.files[PATCH])["spec"]["template"]["spec"]["containers"][0]
    assert container == {"name": "api", "imagePullPolicy": "Always"}   # 이미지는 patch 에 안 쓴다


@needs_kustomize
def test_컨테이너를_통째로_바꿔도_이미지는_images_항목으로(repo):
    import copy as copying

    from kukie_agent.manifests import compare
    from kukie_agent.manifests.render import render
    before = compare.index(render("Kustomize", repo / OVERLAY, root=repo))
    api = next(v for k, v in before.items() if k[1] == "Deployment" and k[3] == "api")
    new = copying.deepcopy(api["spec"]["template"]["spec"]["containers"][0])
    new["image"] = "public.ecr.aws/docker/library/nginx:1.31-alpine"
    new["args"] = ["-g", "daemon off;"]
    result = ready(plan_change(repo, PROD, API, ops(("set", CONTAINER, new))))
    assert {"name": "public.ecr.aws/docker/library/nginx", "newTag": "1.31-alpine"} in \
        yaml.safe_load(result.files[KUSTOMIZATION])["images"]
    assert "image" not in yaml.safe_load(result.files[PATCH])["spec"]["template"]["spec"]["containers"][0]


@needs_kustomize
def test_replicas_가_정하는_레플리카를_지우면_그_항목을_고치라고_한다(repo):
    result = refused(plan_change(repo, PROD, API, ops(("remove", "spec.replicas"))), "patch_mismatch")
    assert "replicas:" in result.message and "거기를 고쳐야 해요" in result.message


@needs_kustomize
def test_commonLabels_가_정하는_레이블은_그_항목이_정한다고_거절한다(repo):
    app = overlay(repo, "labeled", "resources: [../../base]\ncommonLabels: {team: shop}\n")
    target = Target(api_version="apps/v1", kind="Deployment", name="api")
    result = refused(plan_change(repo, app, target, ops(("set", 'metadata.labels["team"]', "pay"))), "patch_mismatch")
    assert "commonLabels:" in result.message and "거기를 고쳐야 해요" in result.message
    # 그 항목이 정하지 않는 레이블은 된다
    ready(plan_change(repo, app, target, ops(("set", 'metadata.labels["tier"]', "web"))))


@needs_kustomize
def test_transformers_가_patch_뒤에_덮으면_원인을_말하며_거절한다(repo):
    transformer = ("apiVersion: builtin\nkind: LabelTransformer\nmetadata: {name: tier}\nlabels: {tier: backend}\n"
                   "fieldSpecs: [{path: metadata/labels, create: true}]\n")
    app = overlay(repo, "transformed", "resources: [../../base]\ntransformers: [tier.yaml]\n", **{"tier.yaml": transformer})
    target = Target(api_version="apps/v1", kind="Deployment", name="api")
    result = refused(plan_change(repo, app, target, ops(("set", 'metadata.labels["tier"]', "web"))), "patch_mismatch")
    assert "transformers:(tier.yaml)" in result.message and "거기를 고쳐야 해요" in result.message


# ── 접두사 · 다른 overlay ─────────────────────────────────


@needs_kustomize
def test_namePrefix_앱은_접두사_전_이름으로_target_을_건다(repo):
    app = overlay(repo, "prefixed", "namePrefix: kustomize-\nresources: [../../base]\n")
    target = Target(api_version="apps/v1", kind="Deployment", name="kustomize-api")
    result = ready(plan_change(repo, app, target, ops(("set", "spec.minReadySeconds", 7))))
    path = "apps/kustomize/overlays/prefixed/kukie-deployment-kustomize-api-patch.yaml"
    assert yaml.safe_load(result.files[path])["metadata"]["name"] == "api"
    patches = yaml.safe_load(result.files["apps/kustomize/overlays/prefixed/kustomization.yaml"])["patches"]
    assert patches[-1]["target"]["name"] == "api"


@needs_kustomize
def test_다른_overlay_는_그대로다(repo):
    """prod 만 고쳤다 — base 를 같이 쓰는 dev 는 렌더 비교(other_environments)에서 그대로여야 PR 이 된다."""
    ready(plan_change(repo, PROD, API, ops(("set", "spec.minReadySeconds", 3))))
    # prod 를 base 로 쓰는 overlay 가 있으면 그 환경도 같이 바뀐다 — PR 을 열지 않는다
    overlay(repo, "prod-eu", "resources: [../prod]\nnameSuffix: -eu\n")
    result = refused(plan_change(repo, PROD, API, ops(("set", "spec.minReadySeconds", 3))), "render_mismatch")
    assert "apps/kustomize/overlays/prod-eu" in result.message


# ── 막는 것 · 경고는 plain 과 같다 ─────────────────────────


@needs_kustomize
def test_위험해_보이는_값은_막지_않고_경고로_올린다(repo):
    result = ready(plan_change(repo, PROD, API, ops(("set", "spec.template.spec.hostNetwork", True))))
    assert [risk.code for risk in result.risk_warnings] == ["host_network"]


@needs_kustomize
def test_셀렉터와_어긋나는_레이블_tracking_id_평문_비밀은_막는다(repo):
    refused(plan_change(repo, PROD, API, ops(("set", 'spec.template.metadata.labels["app"]', "other"))),
            "guarded_field")
    refused(plan_change(repo, PROD, API, ops(
        ("set", "metadata.annotations", {"argocd.argoproj.io/tracking-id": "x"}))), "guarded_field")
    refused(plan_change(repo, PROD, API, ops(
        ("set", f"{CONTAINER}.env", [{"name": "DB_PASSWORD", "value": "hunter2"}]))), "secret")


# ── 렌더 없이 — 패치 만들기 규칙 ───────────────────────────


def test_merge_patch_는_바뀐_칸만_담고_지운_칸은_null():
    assert merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"a": 1, "b": {"c": 5}}, builtin=True) == {
        "b": {"c": 5, "d": None}}


def test_merge_patch_이름으로_병합하는_목록은_항목만():
    current = {"env": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]}
    want = {"env": [{"name": "A", "value": "9"}, {"name": "C", "value": "3"}]}
    assert merge_patch(current, want, builtin=True) == {"env": [
        {"name": "A", "value": "9"}, {"name": "C", "value": "3"}, {"name": "B", "$patch": "delete"}]}


def test_merge_patch_커스텀_리소스와_원자_목록은_통째로():
    current, want = {"ports": [{"port": 1}]}, {"ports": [{"port": 2}]}
    assert merge_patch(current, want, builtin=False) == {"ports": [{"port": 2}]}
    assert merge_patch({"args": ["a"]}, {"args": ["b"]}, builtin=True) == {"args": ["b"]}
    assert merge_patch(current, want, builtin=True) == {"ports": [{"port": 2}, {"$patch": "replace"}]}


def test_original_name_은_이_overlay_의_접두사_접미사만_뗀다():
    assert original_name({"namePrefix": "kustomize-"}, "kustomize-api") == "api"
    assert original_name({"nameSuffix": "-eu"}, "api-eu") == "api"
    assert original_name({"namePrefix": "x-"}, "api") == "api"
    assert original_name({}, "api") == "api"
