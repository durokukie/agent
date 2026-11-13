"""키가 바뀌는 변경 — 리소스 삭제 · 네임스페이스 변경 (DURO-252).

plain 은 픽스처 레포(tests/fixtures/manifests 의 apps/plain)와 kukie-gitops-sandbox 의 plain 앱을 그대로 옮긴
tests/fixtures/sandbox-nginx 로, Kustomize 는 테스트마다 작은 base + overlay 를 만들어 본다.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kukie_agent.manifests import (
    AppSource, Delete, MoveNamespace, Ready, Refused, Target, plan_change, verify_change,
)
from kukie_agent.manifests.render import kustomize_bin

FIXTURES = Path(__file__).parents[1] / "fixtures"
#: 픽스처 plain 앱 — 앱 기본 네임스페이스 shop, 자동 sync + prune
PLAIN = AppSource(source_type="Directory", path="apps/plain", destination_namespace="shop", auto_sync=True, prune=True)
SVC = Target(api_version="v1", kind="Service", name="web", namespace="shop")
WEB = Target(api_version="apps/v1", kind="Deployment", name="web", namespace="shop")
HPA = Target(api_version="autoscaling/v2", kind="HorizontalPodAutoscaler", name="worker", namespace="shop")
#: kukie-gitops-sandbox 의 sandbox-plain 앱 — 프로젝트 sandbox 는 sandbox-* 만 허락한다
SANDBOX_PROJECT = [{"server": "https://kubernetes.default.svc", "namespace": "sandbox-*"}]
SANDBOX = AppSource(source_type="Directory", path="apps/plain", project="sandbox",
                    destination_server="https://kubernetes.default.svc", destination_namespace="sandbox-plain",
                    project_destinations=SANDBOX_PROJECT, auto_sync=True, prune=True)
NGINX = Target(api_version="v1", kind="Pod", name="nginx", namespace="default", in_cluster=False)

needs_kustomize = pytest.mark.skipif(
    shutil.which(kustomize_bin()) is None and not os.environ.get("CI"),
    reason="kustomize 가 없다 (CI 에는 있다)",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURES / "manifests", copy)
    return copy


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    copy = tmp_path / "sandbox"
    shutil.copytree(FIXTURES / "sandbox-nginx", copy)
    return copy


def ready(result: Ready | Refused) -> Ready:
    assert isinstance(result, Ready), result
    return result


def refused(result: Ready | Refused, code: str) -> Refused:
    assert isinstance(result, Refused), result
    assert result.code == code, result
    return result


def codes(result: Ready) -> list[str]:
    return [risk.code for risk in result.risk_warnings]


def write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, encoding="utf-8")
    return root


def test_요청_모양():
    assert MoveNamespace(to="sandbox-plain").to == "sandbox-plain"
    for bad in ("Sandbox", "-a", "a_b", "a" * 64, ""):
        with pytest.raises(ValidationError):
            MoveNamespace(to=bad)


# ── 삭제 · plain ─────────────────────────────────────────


def test_문서가_하나뿐인_파일은_파일째_지운다(repo):
    result = ready(plan_change(repo, PLAIN, SVC, Delete()))
    assert result.files == {"apps/plain/service.yaml": None}
    assert result.file_diff.startswith("--- a/apps/plain/service.yaml\n+++ /dev/null\n")
    assert "-kind: Service" in result.file_diff
    assert result.render_diff.startswith("--- Service shop/web (지금)\n+++ Service shop/web (머지 후 — 없어짐)")
    assert codes(result) == ["prune_on", "delete_address"]


def test_문서가_여럿인_파일은_그_문서만_빼고_나머지와_주석은_그대로다(repo):
    result = ready(plan_change(repo, PLAIN, HPA, Delete()))
    text = result.files["apps/plain/worker.yaml"]
    assert text is not None and text.startswith("# 일감 처리기. 레플리카는 아래 HPA 가 정한다\n")
    docs = list(yaml.safe_load_all(text))
    assert [d["kind"] for d in docs] == ["Deployment"]
    assert "HorizontalPodAutoscaler" not in text and "---" not in text


def test_가운데_문서를_빼도_다른_문서의_주석은_남는다(repo):
    write(repo, {"apps/plain/config.yaml": (
        "# a 설정\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n---\n"
        "# b 설정 — 지울 것\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n---\n"
        "# c 설정\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n")})
    target = Target(api_version="v1", kind="ConfigMap", name="b", namespace="shop")
    text = ready(plan_change(repo, PLAIN, target, Delete())).files["apps/plain/config.yaml"]
    assert text == ("# a 설정\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n---\n"
                    "# c 설정\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n")


def test_대상_말고_다른_것도_사라지면_렌더_비교가_거절한다(repo):
    """AI 가 고른 파일(verify_change)이 다른 리소스까지 지우면 — 요청한 키 하나만 사라져야 한다."""
    result = verify_change(repo, PLAIN, HPA, Delete(), {"apps/plain/worker.yaml": None})
    assert "Deployment worker(사라짐)" in refused(result, "render_mismatch").message


def test_Secret_은_지우지_않는다(repo):
    write(repo, {"apps/plain/secret.yaml": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\n"})
    refused(plan_change(repo, PLAIN, Target(api_version="v1", kind="Secret", name="s"), Delete()), "secret")


# ── 삭제 · 실제로 지워지나 ───────────────────────────────


@pytest.mark.parametrize("app,annotation,code,phrase", [
    (dict(auto_sync=True, prune=True), None, "prune_on", "클러스터에서도 지워요"),
    (dict(auto_sync=True, prune=False), None, "prune_off", "Git 에서만 빠지고 클러스터엔 남아요"),
    (dict(auto_sync=False, prune=False), None, "prune_manual", "sync 할 때 Prune 을 켜야"),
    (dict(auto_sync=True, prune=True), "Prune=false", "prune_disabled", "Prune=false 가 있어"),
    (dict(auto_sync=True, prune=True), "ServerSideApply=true, Prune=confirm", "prune_confirm", "확인(Prune=confirm)"),
])
def test_prune_설정과_리소스_sync_옵션으로_지워지는지를_가른다(repo, app, annotation, code, phrase):
    if annotation:
        text = (repo / "apps/plain/service.yaml").read_text()
        (repo / "apps/plain/service.yaml").write_text(text.replace(
            "  name: web\n", f"  name: web\n  annotations:\n    argocd.argoproj.io/sync-options: {annotation}\n"))
    result = ready(plan_change(repo, PLAIN.model_copy(update=app), SVC, Delete()))
    first = result.risk_warnings[0]
    assert first.code == code and phrase in first.text and first.level == "normal"
    assert first.text in result.warnings


def test_앱의_마지막_리소스는_allowEmpty가_꺼져_있으면_자동으로_안_지워진다고_적는다(sandbox):
    """agent#160 리뷰: ArgoCD 는 자동 sync 가 앱을 비우는 prune 을 막는다(automated.allowEmpty)."""
    for name in ("deployment.yaml", "service.yaml"):
        (sandbox / "apps/plain" / name).unlink()
    first = ready(plan_change(sandbox, SANDBOX.model_copy(update={"project_destinations": None}),
                              NGINX.model_copy(update={"in_cluster": True}), Delete())).risk_warnings[0]
    assert first.code == "prune_empty" and "allowEmpty" in first.text
    allowed = SANDBOX.model_copy(update={"allow_empty": True})
    first = ready(plan_change(sandbox, allowed, NGINX.model_copy(update={"in_cluster": True}), Delete())).risk_warnings[0]
    assert first.code == "prune_on"


def test_Delete_false는_git에서_빼는_것과_상관없다고_적는다(repo):
    text = (repo / "apps/plain/service.yaml").read_text()
    (repo / "apps/plain/service.yaml").write_text(text.replace(
        "  name: web\n", "  name: web\n  annotations:\n    argocd.argoproj.io/sync-options: Delete=false\n"))
    first = ready(plan_change(repo, PLAIN, SVC, Delete())).risk_warnings[0]
    assert first.code == "prune_on" and "Delete=false 는 앱을 지울 때만" in first.text


def test_클러스터에_없는_리소스는_git에서만_빠진다고_적는다(sandbox):
    first = ready(plan_change(sandbox, SANDBOX, NGINX, Delete())).risk_warnings[0]
    assert first.code == "git_only" and "클러스터에서 지울 것은 없어요" in first.text


# ── 삭제 · 위험도 ────────────────────────────────────────


@pytest.mark.parametrize("doc,level,phrase", [
    ("apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: x\nspec:\n  resources: {requests: {storage: 1Gi}}\n"
     "  accessModes: [ReadWriteOnce]\n", "high", "데이터는 돌아오지 않아요"),
    ("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: x\n", "high", "모두 지워져요"),
    ("apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: x\n", "high",
     "커스텀 리소스가 클러스터에서 모두"),
    ("apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: x\nspec:\n  volumeClaimTemplates:\n"
     "    - metadata: {name: data}\n", "high", "PVC 는 남지만"),
    ("apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: x\nspec:\n  persistentVolumeClaimRetentionPolicy:\n"
     "    whenDeleted: Delete\n  volumeClaimTemplates:\n    - metadata: {name: data}\n", "high", "데이터도 같이 지워져요"),
    ("apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: x\n", "normal", "다시 만들어져요"),
    ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", "normal", "다시 만들어져요"),
])
def test_되돌려도_데이터가_안_돌아오는_종류는_높게_적는다(repo, doc, level, phrase):
    write(repo, {"apps/plain/x.yaml": doc})
    parsed = yaml.safe_load(doc)
    namespace = None if parsed["kind"] in ("Namespace", "CustomResourceDefinition") else "shop"
    target = Target(api_version=parsed["apiVersion"], kind=parsed["kind"], name="x", namespace=namespace)
    kind_risk = ready(plan_change(repo, PLAIN, target, Delete())).risk_warnings[1]
    assert kind_risk.level == level and phrase in kind_risk.text


# ── 네임스페이스 변경 · plain ────────────────────────────


def test_sandbox_nginx를_앱_기본_네임스페이스로_옮기면_namespace_줄을_지운다(sandbox):
    """계기(2026-11-13, kukie-gitops-sandbox#10): 프로젝트가 막는 default 에 만든 Pod — 앱 기본 sandbox-plain 으로."""
    result = ready(plan_change(sandbox, SANDBOX, NGINX, MoveNamespace(to="sandbox-plain")))
    assert result.files == {"apps/plain/nginx-pod.yaml": (
        "apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx\nspec:\n  containers:\n    - name: nginx\n"
        "      image: nginx:1.27\n")}
    assert result.render_diff.startswith("--- Pod default/nginx (지금)\n+++ Pod sandbox-plain/nginx (머지 후 — 새로 생김)")
    assert "-  namespace: default" in result.render_diff
    assert codes(result) == ["git_only", "recreate", "move_refs"]          # 옛 것은 클러스터에 없다 · 앱 기본이라 ns 경고 없음
    assert "삭제 후 생성" in result.risk_warnings[1].text


def test_허락된_다른_네임스페이스로_옮기면_namespace_줄을_바꾸거나_더한다(sandbox):
    to = MoveNamespace(to="sandbox-two")
    text = ready(plan_change(sandbox, SANDBOX, NGINX, to)).files["apps/plain/nginx-pod.yaml"]
    assert text is not None and "  namespace: sandbox-two\n" in text and "default" not in text
    service = Target(api_version="v1", kind="Service", name="web", namespace="sandbox-plain")
    result = ready(plan_change(sandbox, SANDBOX, service, to))
    assert result.files["apps/plain/service.yaml"].startswith(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  namespace: sandbox-two\nspec:")
    assert codes(result) == ["prune_on", "recreate", "move_address", "move_namespace_exists"]
    assert "web.sandbox-plain.svc 에서 web.sandbox-two.svc" in result.risk_warnings[2].text


def test_프로젝트가_허락하지_않는_곳으로는_옮기지_않는다(sandbox):
    result = refused(plan_change(sandbox, SANDBOX, NGINX, MoveNamespace(to="kube-public")), "project_denied")
    assert result.retry and "sandbox-*" in result.message and "앱 기본은 sandbox-plain" in result.message


def test_프로젝트를_못_읽었으면_막지_않고_경고한다(sandbox):
    app = SANDBOX.model_copy(update={"project_destinations": None})
    result = ready(plan_change(sandbox, app, NGINX, MoveNamespace(to="kube-public")))
    assert "project_unknown" in codes(result)


def test_이미_그_네임스페이스이거나_옮길_수_없는_것은_거절한다(repo, sandbox):
    refused(plan_change(repo, PLAIN, SVC, MoveNamespace(to="shop")), "no_change")     # 문서에 없어도 앱 기본이 shop
    write(repo, {"apps/plain/ns.yaml": "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: extra\n"})
    namespace = Target(api_version="v1", kind="Namespace", name="extra")
    refused(plan_change(repo, PLAIN, namespace, MoveNamespace(to="other")), "key_change_blocked")
    write(sandbox, {"apps/plain/web-two.yaml": (
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  namespace: sandbox-two\nspec:\n  ports: [{port: 80}]\n")})
    service = Target(api_version="v1", kind="Service", name="web", namespace="sandbox-plain")
    result = refused(plan_change(sandbox, SANDBOX, service, MoveNamespace(to="sandbox-two")), "key_change_blocked")
    assert "이미 이 앱에 있어요" in result.message
    # agent#160 리뷰: 앱 기본으로 옮길 때 그곳을 문서에 적어 둔 같은 리소스도 같은 곳이다 (키는 "" 와 sandbox-plain)
    write(sandbox, {"apps/plain/nginx-two.yaml": (
        "apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx\n  namespace: sandbox-plain\nspec:\n"
        "  containers: [{name: nginx, image: nginx:1.27}]\n")})
    result = refused(plan_change(sandbox, SANDBOX, NGINX, MoveNamespace(to="sandbox-plain")), "key_change_blocked")
    assert "Pod sandbox-plain/nginx 가 이미 이 앱에 있어요" in result.message


def test_PVC를_옮기면_데이터가_안_옮겨진다고_높게_적는다(repo):
    write(repo, {"apps/plain/pvc.yaml": "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: data\n"})
    target = Target(api_version="v1", kind="PersistentVolumeClaim", name="data", namespace="shop")
    result = ready(plan_change(repo, PLAIN, target, MoveNamespace(to="other")))
    [data] = [risk for risk in result.risk_warnings if risk.code == "move_data"]
    assert data.level == "high" and "데이터는 옮겨지지 않아요" in data.text


def test_옮기면서_다른_칸까지_바뀌면_렌더_비교가_거절한다(sandbox):
    text = (sandbox / "apps/plain/nginx-pod.yaml").read_text()
    edited = text.replace("  namespace: default\n", "").replace("nginx:1.27", "nginx:1.28")
    result = verify_change(sandbox, SANDBOX, NGINX, MoveNamespace(to="sandbox-plain"),
                           {"apps/plain/nginx-pod.yaml": edited})
    assert "image" in refused(result, "render_mismatch").message


# ── Kustomize ────────────────────────────────────────────

BASE = {
    "apps/k/base/kustomization.yaml": "resources:\n  - deployment.yaml\n",
    "apps/k/base/deployment.yaml": (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  selector:\n    matchLabels: {app: api}\n"
        "  template:\n    metadata:\n      labels: {app: api}\n    spec:\n      containers:\n        - name: api\n"
        "          image: nginx:1.27\n"),
    "apps/k/overlays/dev/kustomization.yaml": "namespace: dev\nresources:\n  - ../../base\n",
    "apps/k/overlays/prod/kustomization.yaml": (
        "resources:\n  - ../../base\n  # 이 환경만 쓰는 설정\n  - settings.yaml\n  - extra.yaml\n"),
    "apps/k/overlays/prod/settings.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\ndata:\n  a: '1'\n",
    "apps/k/overlays/prod/extra.yaml": (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: extra-a\n---\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: extra-b\n"),
}
PROD = AppSource(source_type="Kustomize", path="apps/k/overlays/prod", destination_namespace="shop",
                 auto_sync=True, prune=True)
SETTINGS = Target(api_version="v1", kind="ConfigMap", name="settings", namespace="shop")


@pytest.fixture
def kustomize_repo(tmp_path: Path) -> Path:
    return write(tmp_path / "k", BASE)


@needs_kustomize
def test_overlay_파일에만_있으면_resources_줄과_파일을_지운다(kustomize_repo):
    result = ready(plan_change(kustomize_repo, PROD, SETTINGS, Delete()))
    assert result.files == {
        "apps/k/overlays/prod/settings.yaml": None,
        "apps/k/overlays/prod/kustomization.yaml": "resources:\n  - ../../base\n  # 이 환경만 쓰는 설정\n  - extra.yaml\n",
    }


@needs_kustomize
def test_overlay_파일에_문서가_여럿이면_그_문서만_뺀다(kustomize_repo):
    target = Target(api_version="v1", kind="ConfigMap", name="extra-b", namespace="shop")
    result = ready(plan_change(kustomize_repo, PROD, target, Delete()))
    assert result.files == {"apps/k/overlays/prod/extra.yaml": (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: extra-a\n")}


@needs_kustomize
def test_base에서_오는_리소스는_지우지도_옮기지도_않는다(kustomize_repo):
    api = Target(api_version="apps/v1", kind="Deployment", name="api", namespace="shop")
    for change in (Delete(), MoveNamespace(to="other")):
        result = refused(plan_change(kustomize_repo, PROD, api, change), "key_change_blocked")
        assert "base(다른 폴더)에서 와요" in result.message and "다른 환경" in result.message


@needs_kustomize
@pytest.mark.parametrize("patches,where", [
    ("patches:\n  - path: settings-patch.yaml\n", "settings-patch.yaml"),
    ("patches:\n  - target: {kind: ConfigMap, name: settings}\n    patch: |-\n      - op: add\n        path: /data/b\n"
     "        value: '2'\n", "patches 1번째"),
    ("patches:\n  - target: {kind: ConfigMap, name: 'set.*'}\n    path: json.yaml\n", "json.yaml"),
    ("patchesStrategicMerge:\n  - settings-patch.yaml\n", "settings-patch.yaml"),
])
def test_overlay_patch가_가리키는_리소스는_거절한다(kustomize_repo, patches, where):
    write(kustomize_repo, {
        "apps/k/overlays/prod/settings-patch.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n"
                                                    "data:\n  b: '2'\n",
        "apps/k/overlays/prod/json.yaml": "- op: add\n  path: /data/b\n  value: '2'\n",
    })
    file = kustomize_repo / "apps/k/overlays/prod/kustomization.yaml"
    file.write_text(file.read_text() + patches)
    for change in (Delete(), MoveNamespace(to="other")):
        result = refused(plan_change(kustomize_repo, PROD, SETTINGS, change), "key_change_blocked")
        assert f"patch({where})" in result.message and "렌더가 깨져요" in result.message


@needs_kustomize
def test_종류로만_고르는_patch는_남아도_되니_막지_않는다(kustomize_repo):
    file = kustomize_repo / "apps/k/overlays/prod/kustomization.yaml"
    file.write_text(file.read_text() + "patches:\n  - target: {kind: ConfigMap}\n    patch: |-\n"
                                       "      - op: add\n        path: /metadata/labels\n        value: {team: a}\n")
    ready(plan_change(kustomize_repo, PROD, SETTINGS, Delete()))


@needs_kustomize
def test_overlay_namespace가_정하면_옮기지_않는다(kustomize_repo):
    write(kustomize_repo, {"apps/k/overlays/dev/kustomization.yaml": "namespace: dev\nresources:\n  - ../../base\n"
                                                                      "  - local.yaml\n",
                           "apps/k/overlays/dev/local.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: local\n"})
    dev = PROD.model_copy(update={"path": "apps/k/overlays/dev", "destination_namespace": "dev"})
    target = Target(api_version="v1", kind="ConfigMap", name="local", namespace="dev")
    result = refused(plan_change(kustomize_repo, dev, target, MoveNamespace(to="other")), "key_change_blocked")
    assert "`namespace: dev`" in result.message and "앱 전체가 옮겨져요" in result.message
    ready(plan_change(kustomize_repo, dev, target, Delete()))                     # 지우는 것은 된다


@needs_kustomize
def test_overlay_파일의_리소스는_옮길_수_있다(kustomize_repo):
    result = ready(plan_change(kustomize_repo, PROD, SETTINGS, MoveNamespace(to="other")))
    assert result.files == {"apps/k/overlays/prod/settings.yaml": (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n  namespace: other\ndata:\n  a: '1'\n")}


@needs_kustomize
def test_이름_앞_붙이기가_있어도_overlay_파일의_문서를_찾는다(kustomize_repo):
    file = kustomize_repo / "apps/k/overlays/prod/kustomization.yaml"
    file.write_text("namePrefix: prod-\n" + file.read_text())
    target = Target(api_version="v1", kind="ConfigMap", name="prod-settings", namespace="shop")
    assert "apps/k/overlays/prod/settings.yaml" in ready(plan_change(kustomize_repo, PROD, target, Delete())).files
