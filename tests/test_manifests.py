"""매니페스트 레포 고칠 곳 찾기 + 렌더 비교 — DURO-133.

픽스처 레포(tests/fixtures/manifests)에 네 요청(레플리카 · 이미지 태그 · resources · 환경변수)을 걸어
올바른 파일의 올바른 줄만 바뀌는지, base 를 고치거나 다른 리소스를 바꾸는 시도가 렌더 비교에서 걸리는지 본다.
Kustomize 는 진짜 `kustomize build` 를 돌린다 — 없으면 건너뛰지만 CI(CI=true)에서는 건너뛰지 않고 실패한다.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from kukie_agent.manifests import (
    AppSource, Env, ImageTag, Other, Ready, Refused, Replicas, Resources, Target, plan_change, verify_change,
)
from kukie_agent.manifests.render import kustomize_bin
from kukie_agent.manifests.workload import split_image

FIXTURE = Path(__file__).parent / "fixtures" / "manifests"
PLAIN = AppSource(source_type="Directory", path="apps/plain")
PROD = AppSource(source_type="Kustomize", path="apps/kustomize/overlays/prod")
WEB = Target(api_version="apps/v1", kind="Deployment", name="web", namespace="shop")
API = Target(api_version="apps/v1", kind="Deployment", name="api", namespace="shop-prod")
AGENT = Target(api_version="apps/v1", kind="Deployment", name="agent", namespace="shop-prod")

needs_kustomize = pytest.mark.skipif(
    shutil.which(kustomize_bin()) is None and not os.environ.get("CI"),
    reason="kustomize 가 없다 (CI 에서는 깔고 돈다)")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """고친 결과를 다시 써 보는 테스트가 있어서 픽스처를 복사해 쓴다."""
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURE, copy)
    return copy


def ready(result: Ready | Refused) -> Ready:
    assert isinstance(result, Ready), result
    return result


def refused(result: Ready | Refused, code: str) -> Refused:
    assert isinstance(result, Refused), result
    assert result.code == code, result
    return result


def changed_lines(diff: str) -> tuple[list[str], list[str]]:
    """unified diff 에서 지운 줄과 더한 줄 (머리 줄은 뺀다)."""
    lines = [line.rstrip("\n") for line in diff.splitlines()]
    removed = [l[1:] for l in lines if l.startswith("-") and not l.startswith("---")]
    added = [l[1:] for l in lines if l.startswith("+") and not l.startswith("+++")]
    return removed, added


def write(repo: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        (repo / path).write_text(content, encoding="utf-8")


def rendered(objects_yaml: str) -> dict:
    return yaml.safe_load(objects_yaml)


# ── plain (Directory) ────────────────────────────────────


def test_plain_레플리카는_그_문서의_replicas_한_줄만_바꾼다(repo):
    result = ready(plan_change(repo, PLAIN, WEB, Replicas(count=3)))

    assert list(result.files) == ["apps/plain/deployment.yaml"]
    assert changed_lines(result.file_diff) == (["  replicas: 1"], ["  replicas: 3"])
    assert changed_lines(result.render_diff) == (["  replicas: 1"], ["  replicas: 3"])
    # 주석과 `{app: web}` 모양은 그대로다
    assert result.files["apps/plain/deployment.yaml"].startswith("# 웹 화면.")
    assert result.warnings == []


def test_plain_이미지_태그는_그_컨테이너의_image_만_바꾼다(repo):
    result = ready(plan_change(repo, PLAIN, WEB, ImageTag(tag="1.29-alpine")))

    assert changed_lines(result.file_diff) == (
        ["          image: public.ecr.aws/docker/library/nginx:1.28-alpine"],
        ["          image: public.ecr.aws/docker/library/nginx:1.29-alpine"])


def test_plain_resources_는_준_칸만_바꾸고_나머지는_둔다(repo):
    result = ready(plan_change(repo, PLAIN, WEB, Resources(requests={"cpu": "50m"}, limits={"cpu": "500m"})))

    assert changed_lines(result.file_diff) == (
        ["            requests: {cpu: 10m, memory: 16Mi}", "            limits: {memory: 64Mi}"],
        ["            requests: {cpu: 50m, memory: 16Mi}", "            limits: {memory: 64Mi, cpu: 500m}"])


def test_plain_환경변수는_있으면_바꾸고_없으면_더한다(repo):
    result = ready(plan_change(repo, PLAIN, WEB, Env(values={"GREETING": "hi", "PORT": "8080"})))

    removed, added = changed_lines(result.file_diff)
    assert removed == ["              value: hello"]
    # 숫자처럼 보이는 값은 따옴표로 — 쿠버네티스 env 값은 문자열이어야 한다
    assert added == ["              value: hi", "            - name: PORT", "              value: '8080'"]


def test_plain_환경변수를_지운다(repo):
    result = ready(plan_change(repo, PLAIN, WEB, Env(remove=["GREETING"])))

    removed, added = changed_lines(result.file_diff)
    assert removed == ["            - name: GREETING", "              value: hello"]
    assert added == []


def test_plain_컨테이너가_여럿이면_골라_달라고_한다(repo):
    worker = Target(api_version="apps/v1", kind="Deployment", name="worker")
    result = refused(plan_change(repo, PLAIN, worker, ImageTag(tag="1.38")), "ambiguous")
    assert result.retry and "worker, metrics" in result.message

    ready(plan_change(repo, PLAIN, worker, ImageTag(tag="1.38", container="metrics")))


# ── Kustomize: Application path(overlays/prod)만 고친다 ──


@needs_kustomize
def test_kustomize_레플리카는_overlay_의_replicas_칸만_바꾼다(repo):
    result = ready(plan_change(repo, PROD, API, Replicas(count=4)))

    assert list(result.files) == ["apps/kustomize/overlays/prod/kustomization.yaml"]
    assert changed_lines(result.file_diff) == (["    count: 2"], ["    count: 4"])
    assert changed_lines(result.render_diff) == (["  replicas: 2"], ["  replicas: 4"])


@needs_kustomize
def test_kustomize_레플리카_칸이_없는_리소스는_칸을_더한다(repo):
    result = ready(plan_change(repo, PROD, AGENT, Replicas(count=2)))

    kustomization = yaml.safe_load(result.files["apps/kustomize/overlays/prod/kustomization.yaml"])
    assert kustomization["replicas"] == [{"name": "api", "count": 2}, {"name": "agent", "count": 2}]
    assert changed_lines(result.file_diff)[0] == []


@needs_kustomize
def test_kustomize_이미지_태그는_images_칸의_newTag_를_바꾼다(repo):
    result = ready(plan_change(repo, PROD, API, ImageTag(tag="1.30-alpine")))

    assert list(result.files) == ["apps/kustomize/overlays/prod/kustomization.yaml"]
    assert changed_lines(result.file_diff) == (["    newTag: 1.29-alpine"], ["    newTag: 1.30-alpine"])


@needs_kustomize
def test_kustomize_digest_로_고정된_이미지는_digest_를_떼고_태그를_단다(repo):
    result = ready(plan_change(repo, PROD, AGENT, ImageTag(tag="prod-2026-10-30")))

    removed, added = changed_lines(result.file_diff)
    assert removed == ["    digest: sha256:d6c243eba5605e37d6e9d662f89c7058866db42b3b35989454842c3898ee689f"]
    assert added == ["    newTag: prod-2026-10-30"]
    _, image_after = changed_lines(result.render_diff)
    assert image_after == ["      - image: 320343233643.dkr.ecr.ap-northeast-2.amazonaws.com/kukie-agent:prod-2026-10-30"]


@needs_kustomize
def test_kustomize_resources_는_overlay_에_patch_파일을_더한다(repo):
    result = ready(plan_change(repo, PROD, API, Resources(limits={"memory": "128Mi"})))

    patch = "apps/kustomize/overlays/prod/kukie-deployment-api-resources.yaml"
    assert sorted(result.files) == [patch, "apps/kustomize/overlays/prod/kustomization.yaml"]
    assert yaml.safe_load(result.files[patch])["spec"]["template"]["spec"]["containers"] == [
        {"name": "api", "resources": {"limits": {"memory": "128Mi"}}}]
    assert f"--- /dev/null\n+++ b/{patch}" in result.file_diff
    assert changed_lines(result.render_diff) == (["            memory: 64Mi"], ["            memory: 128Mi"])
    # base 는 그대로다
    assert not any(path.startswith("apps/kustomize/base") for path in result.files)


@needs_kustomize
def test_kustomize_같은_요청이_다시_오면_Kukie_patch_파일을_고친다(repo):
    write(repo, ready(plan_change(repo, PROD, API, Resources(limits={"memory": "128Mi"}))).files)

    result = ready(plan_change(repo, PROD, API, Resources(requests={"cpu": "20m"})))

    assert list(result.files) == ["apps/kustomize/overlays/prod/kukie-deployment-api-resources.yaml"]
    assert changed_lines(result.file_diff) == ([], ["            requests:", "              cpu: 20m"])


@needs_kustomize
def test_kustomize_환경변수는_patch_로_바꾸고_앞의_patch_보다_나중에_걸린다(repo):
    result = ready(plan_change(repo, PROD, API, Env(values={"STAGE": "prod-blue", "LOG_LEVEL": "debug"})))

    patch = "apps/kustomize/overlays/prod/kukie-deployment-api-env.yaml"
    assert sorted(result.files) == ["apps/kustomize/overlays/prod/kukie-deployment-api-env.yaml",
                                    "apps/kustomize/overlays/prod/kustomization.yaml"]
    kustomization = yaml.safe_load(result.files["apps/kustomize/overlays/prod/kustomization.yaml"])
    assert kustomization["patches"] == [
        {"path": "env.yaml"},
        {"path": "kukie-deployment-api-env.yaml",
         "target": {"group": "apps", "version": "v1", "kind": "Deployment", "name": "api"}}]
    removed, added = changed_lines(result.render_diff)
    assert "          value: prod" in removed
    assert {"          value: prod-blue", "        - name: LOG_LEVEL"} <= set(added)
    assert result.files[patch].startswith("# Kukie 가 더한 patch")


@needs_kustomize
def test_kustomize_환경변수_지우기는_patch_delete_표시로(repo):
    result = ready(plan_change(repo, PROD, API, Env(remove=["STAGE"])))

    patch = yaml.safe_load(result.files["apps/kustomize/overlays/prod/kukie-deployment-api-env.yaml"])
    assert patch["spec"]["template"]["spec"]["containers"][0]["env"] == [{"name": "STAGE", "$patch": "delete"}]
    removed, _ = changed_lines(result.render_diff)
    assert "        - name: STAGE" in removed


# ── 렌더 비교에서 걸리는 시도 ─────────────────────────────


@needs_kustomize
def test_base_를_고치면_다른_환경이_바뀌어_렌더_비교에서_걸린다(repo):
    base = "apps/kustomize/base/deployment.yaml"
    edited = (repo / base).read_text().replace("limits: {memory: 64Mi}", "limits: {memory: 128Mi}")

    result = refused(verify_change(repo, PROD, API, Resources(limits={"memory": "128Mi"}), {base: edited}),
                     "render_mismatch")

    assert result.retry
    assert "apps/kustomize/overlays/dev" in result.message and "apps/kustomize/base" in result.message


@needs_kustomize
def test_다른_리소스가_바뀌면_렌더_비교에서_걸린다(repo):
    path = "apps/kustomize/overlays/prod/kustomization.yaml"
    # images 칸 이름을 바꾸면 api 뿐 아니라 agent 의 이미지도 바뀐다
    edited = (repo / path).read_text().replace("    newTag: 1.29-alpine\n", "    newTag: 1.30-alpine\n").replace(
        "    digest: sha256:d6c243eba5605e37d6e9d662f89c7058866db42b3b35989454842c3898ee689f\n", "    newTag: x\n")

    result = refused(verify_change(repo, PROD, API, ImageTag(tag="1.30-alpine"), {path: edited}), "render_mismatch")

    assert "Deployment shop-prod/agent" in result.message


@needs_kustomize
def test_요청한_리소스의_다른_칸이_바뀌면_렌더_비교에서_걸린다(repo):
    path = "apps/kustomize/overlays/prod/kustomization.yaml"
    edited = (repo / path).read_text().replace("    count: 2", "    count: 4").replace(
        "    newTag: 1.29-alpine", "    newTag: 1.30-alpine")

    result = refused(verify_change(repo, PROD, API, Replicas(count=4), {path: edited}), "render_mismatch")

    assert "spec.template.spec.containers[api].image" in result.message


@needs_kustomize
def test_고쳐도_렌더_결과가_그대로면_걸린다(repo):
    path = "apps/kustomize/overlays/prod/kustomization.yaml"
    # 주석만 고치면 파일은 바뀌어도 클러스터에 갈 것은 그대로다
    edited = (repo / path).read_text() + "# replicas 4 로 올림\n"

    result = refused(verify_change(repo, PROD, API, Replicas(count=4), {path: edited}), "render_mismatch")

    assert "그대로" in result.message


@needs_kustomize
def test_값이_요청과_다르면_걸린다(repo):
    path = "apps/kustomize/overlays/prod/kustomization.yaml"
    edited = (repo / path).read_text().replace("    count: 2", "    count: 5")

    result = refused(verify_change(repo, PROD, API, Replicas(count=4), {path: edited}), "render_mismatch")

    assert "spec.replicas (요청대로면 4, 고친 뒤 5)" in result.message


def test_요청하지_않은_resources_키가_바뀌면_걸린다(repo):
    path = "apps/plain/deployment.yaml"
    # cpu 를 올리면서 memory limit 을 지운다
    edited = (repo / path).read_text().replace("requests: {cpu: 10m, memory: 16Mi}", "requests: {cpu: 100m, memory: 16Mi}") \
        .replace("            limits: {memory: 64Mi}\n", "")

    result = refused(verify_change(repo, PLAIN, WEB, Resources(requests={"cpu": "100m"}), {path: edited}),
                     "render_mismatch")

    assert "resources.limits" in result.message


def test_요청하지_않은_환경변수가_바뀌면_걸린다(repo):
    path = "apps/plain/deployment.yaml"
    # PORT 를 더하면서 Secret 에서 오던 DB_URL 을 평문으로 바꾼다
    edited = (repo / path).read_text().replace(
        "              valueFrom:\n                secretKeyRef: {name: web-db, key: url}\n",
        "              value: postgres://x\n            - name: PORT\n              value: '8080'\n")

    result = refused(verify_change(repo, PLAIN, WEB, Env(values={"PORT": "8080"}), {path: edited}), "render_mismatch")

    assert "env[DB_URL]" in result.message


@needs_kustomize
def test_kustomize_patch_가_다른_환경변수를_지우면_걸린다(repo):
    patch = "apps/kustomize/overlays/prod/kukie-deployment-api-env.yaml"
    files = ready(plan_change(repo, PROD, API, Env(values={"LOG_LEVEL": "debug"}))).files
    files[patch] = files[patch].replace(
        "            - name: LOG_LEVEL\n", "            - name: STAGE\n              $patch: delete\n            - name: LOG_LEVEL\n")

    result = refused(verify_change(repo, PROD, API, Env(values={"LOG_LEVEL": "debug"}), files), "render_mismatch")

    assert "env[STAGE]" in result.message


def test_plain_Application_path_밖_파일을_고치면_걸린다(repo):
    (repo / "apps/other").mkdir()
    edits = {"apps/plain/deployment.yaml": (repo / "apps/plain/deployment.yaml").read_text().replace(
        "replicas: 1", "replicas: 3"), "apps/other/note.yaml": "hello: world\n"}

    result = refused(verify_change(repo, PLAIN, WEB, Replicas(count=3), edits), "outside_app")

    assert "apps/other/note.yaml" in result.message


def test_plain_새_문서를_더하면_다른_리소스가_생겨_걸린다(repo):
    path = "apps/plain/service.yaml"
    edits = {
        "apps/plain/deployment.yaml": (repo / "apps/plain/deployment.yaml").read_text().replace("replicas: 1", "replicas: 3"),
        path: (repo / path).read_text() + "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: extra}\n",
    }

    result = refused(verify_change(repo, PLAIN, WEB, Replicas(count=3), edits), "render_mismatch")

    assert "ConfigMap extra(새로 생김)" in result.message


def test_레포_밖_경로는_고칠_수_없다(repo):
    result = refused(verify_change(repo, PLAIN, WEB, Replicas(count=3), {"../escape.yaml": "x: 1\n"}), "invalid_path")
    assert result.retry


# ── 함정 ─────────────────────────────────────────────────


def test_HPA_가_있으면_레플리카_대신_HPA_를_바꾸라고_한다(repo):
    worker = Target(api_version="apps/v1", kind="Deployment", name="worker")
    result = refused(plan_change(repo, PLAIN, worker, Replicas(count=3)), "hpa")
    assert "HPA(worker)" in result.message and not result.retry


def test_클러스터에만_있는_HPA_도_레플리카를_막는다(repo):
    result = refused(plan_change(repo, PLAIN, WEB, Replicas(count=3), hpa="web-hpa"), "hpa")
    assert "HPA(web-hpa)" in result.message


@needs_kustomize
def test_git_에_원본이_없는_리소스는_원본_없음(repo):
    operator_made = Target(api_version="apps/v1", kind="Deployment", name="redis-operator-made", namespace="shop-prod")
    refused(plan_change(repo, PROD, operator_made, Replicas(count=2)), "no_source")
    refused(plan_change(repo, PLAIN, operator_made, Replicas(count=2)), "no_source")


def test_Secret_은_PR_로_바꾸지_않는다(repo):
    secret = Target(api_version="v1", kind="Secret", name="web-db")
    result = refused(plan_change(repo, PLAIN, secret, Env(values={"A": "b"})), "secret")
    assert "Sealed Secrets" in result.message


def test_Secret_에서_오는_환경변수나_비밀처럼_보이는_이름은_바꾸지_않는다(repo):
    refused(plan_change(repo, PLAIN, WEB, Env(values={"DB_URL": "postgres://x"})), "secret")
    refused(plan_change(repo, PLAIN, WEB, Env(values={"API_TOKEN": "abc"})), "secret")


def test_Secret_이_바뀌는_diff_는_만들지_않는다(repo):
    path = "apps/plain/secret.yaml"
    (repo / path).write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: web-db}\nstringData: {url: old}\n")
    edits = {
        "apps/plain/deployment.yaml": (repo / "apps/plain/deployment.yaml").read_text().replace("replicas: 1", "replicas: 3"),
        path: "apiVersion: v1\nkind: Secret\nmetadata: {name: web-db}\nstringData: {url: new-password}\n",
    }

    result = refused(verify_change(repo, PLAIN, WEB, Replicas(count=3), edits), "secret")

    assert "new-password" not in result.model_dump_json()


def test_RespectIgnoreDifferences_로_sync_때_빼는_칸은_고치지_않는다(repo):
    app = PLAIN.model_copy(update={"respect_ignore_differences": True, "ignore_differences": [
        {"group": "apps", "kind": "Deployment", "jsonPointers": ["/spec/replicas"]}]})
    refused(plan_change(repo, app, WEB, Replicas(count=3)), "ignored_field")


def test_ignoreDifferences_만_있으면_고치되_자동_sync_가_안_될_수_있다고_경고한다(repo):
    # ArgoCD 는 ignoreDifferences 를 sync 상태 계산에만 쓴다 — sync 하면 반영된다
    app = PLAIN.model_copy(update={"ignore_differences": [
        {"group": "apps", "kind": "Deployment", "jsonPointers": ["/spec/replicas"]}]})
    result = ready(plan_change(repo, app, WEB, Replicas(count=3)))
    assert any("자동 sync" in w for w in result.warnings)


def test_RespectIgnoreDifferences_가_칸의_일부만_빼면_경고를_단다(repo):
    app = PLAIN.model_copy(update={"respect_ignore_differences": True, "ignore_differences": [
        {"group": "apps", "kind": "Deployment", "name": "web",
         "jsonPointers": ["/spec/template/spec/containers/0/env/0/value"]}]})
    result = ready(plan_change(repo, app, WEB, Env(values={"PORT": "8080"})))
    assert any("/spec/template/spec/containers/0/env/0/value" in w for w in result.warnings)


def test_이미_그_값이면_바꿀_게_없다(repo):
    refused(plan_change(repo, PLAIN, WEB, Replicas(count=1)), "no_change")
    refused(plan_change(repo, PLAIN, WEB, ImageTag(tag="1.28-alpine")), "no_change")
    refused(plan_change(repo, PLAIN, WEB, Env(values={"GREETING": "hello"}, remove=["NOPE"])), "no_change")


def test_Helm_은_아직_지원하지_않는다(repo):
    helm = AppSource(source_type="Helm", path="apps/helm")
    result = refused(plan_change(repo, helm, WEB, Replicas(count=3)), "unsupported_source")
    assert not result.retry


def test_네_요청_밖은_아직_PR_로_못_한다(repo):
    result = refused(plan_change(repo, PLAIN, WEB, Other(description="노드 셀렉터 바꾸기")), "unsupported_change")
    assert "아직 PR 로 못 해요" in result.message


def test_Service_에는_레플리카가_없다(repo):
    service = Target(api_version="v1", kind="Service", name="web")
    refused(plan_change(repo, PLAIN, service, Replicas(count=2)), "unsupported_change")


def test_Application_path_가_레포_밖이면_거절한다(repo):
    refused(plan_change(repo, PLAIN.model_copy(update={"path": "../.."}), WEB, Replicas(count=2)), "invalid_path")


def test_요청_값은_모양을_검사한다():
    with pytest.raises(ValueError):
        Resources(requests={"cpu": "lots"})
    with pytest.raises(ValueError):
        ImageTag(tag="1.0; rm -rf /")
    with pytest.raises(ValueError):
        Env(values={"BAD NAME": "x"})
    with pytest.raises(ValueError):
        Replicas(count=-1)


@pytest.mark.parametrize(("image", "parts"), [
    ("nginx", ("nginx", None, None)),
    ("nginx:1.29", ("nginx", "1.29", None)),
    ("registry:5000/team/app", ("registry:5000/team/app", None, None)),
    ("registry:5000/team/app:v2@sha256:ab", ("registry:5000/team/app", "v2", "sha256:ab")),
])
def test_이미지_이름을_나눈다(image, parts):
    assert split_image(image) == parts


def test_레포_밖을_가리키는_링크는_읽지_않는다(repo, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: host-file}\n")
    (repo / "apps/plain/link.yaml").symlink_to(outside)

    result = refused(plan_change(repo, PLAIN, WEB, Replicas(count=3)), "render_failed")

    assert "레포 밖" in result.message


# ── 리뷰에서 나온 모양 (PR #124) ─────────────────────────


def test_이름_없는_컨테이너는_고르지_않는다(repo):
    (repo / "apps/plain/deployment.yaml").write_text((repo / "apps/plain/deployment.yaml").read_text().replace(
        "        - name: web\n          image:", "        - image:"))

    refused(plan_change(repo, PLAIN, WEB, ImageTag(tag="1.29-alpine")), "unsupported_change")


def test_값이_빈_resources_칸에도_자원을_넣는다(repo):
    path = repo / "apps/plain/deployment.yaml"
    path.write_text(path.read_text().replace(
        "          resources:\n            requests: {cpu: 10m, memory: 16Mi}\n            limits: {memory: 64Mi}\n",
        "          resources:\n"))

    result = ready(plan_change(repo, PLAIN, WEB, Resources(requests={"cpu": "50m"})))

    assert changed_lines(result.file_diff) == ([], ["            requests:", "              cpu: 50m"])


@needs_kustomize
@pytest.mark.parametrize("content", ["", "# 다 지웠다\n"])
def test_비었거나_주석만_남은_Kukie_patch_파일은_새로_쓴다(repo, content):
    patch = repo / "apps/kustomize/overlays/prod/kukie-deployment-api-env.yaml"
    write(repo, ready(plan_change(repo, PROD, API, Env(values={"LOG_LEVEL": "debug"}))).files)
    patch.write_text(content)

    result = ready(plan_change(repo, PROD, API, Env(values={"LOG_LEVEL": "info"})))

    rewritten = yaml.safe_load(result.files["apps/kustomize/overlays/prod/kukie-deployment-api-env.yaml"])
    assert rewritten["spec"]["template"]["spec"]["containers"] == [
        {"name": "api", "env": [{"name": "LOG_LEVEL", "value": "info"}]}]


def test_같은_리소스를_두_번_만드는_edits_는_걸린다(repo):
    fixed = (repo / "apps/plain/deployment.yaml").read_text().replace("replicas: 1", "replicas: 3")
    edits = {"apps/plain/deployment.yaml": fixed, "apps/plain/aaa-web.yaml": fixed.replace("replicas: 3", "replicas: 99")}

    result = refused(verify_change(repo, PLAIN, WEB, Replicas(count=3), edits), "render_failed")

    assert "두 번" in result.message


def test_없는_컨테이너_번호를_가리키는_ignoreDifferences_는_아무_칸도_덮지_않는다(repo):
    app = PLAIN.model_copy(update={"respect_ignore_differences": True, "ignore_differences": [
        {"group": "apps", "kind": "Deployment", "jsonPointers": ["/spec/template/spec/containers/1/resources"]}]})

    result = ready(plan_change(repo, app, WEB, Resources(requests={"cpu": "50m"})))

    assert result.warnings == []
