"""새 리소스 더하기 + 렌더 비교 — DURO-154.

픽스처 레포(tests/fixtures/manifests — durokukie/kukie-gitops-sandbox 의 apps/plain · apps/kustomize/overlays/prod 모양)에
새 Deployment + Service 를 더해 PR diff 와 렌더 비교를 보고, 막음·경고 경우를 하나씩 본다.
Kustomize 는 진짜 `kustomize build` 를 돌린다 — 없으면 건너뛰지만 CI(CI=true)에서는 건너뛰지 않고 실패한다.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from kukie_agent.manifests import (
    AppSource, NewResource, Ready, Refused, Target, plan_new_resource,
)
from kukie_agent.manifests.compare import index
from kukie_agent.manifests.new_resource import _compare
from kukie_agent.manifests.render import kustomize_bin

FIXTURE = Path(__file__).parent / "fixtures" / "manifests"
PLAIN = AppSource(source_type="Directory", path="apps/plain")
RECURSE = AppSource(source_type="Directory", path="apps/plain", directory_recurse=True)
PROD = AppSource(source_type="Kustomize", path="apps/kustomize/overlays/prod")
BASE = AppSource(source_type="Kustomize", path="apps/kustomize/base")
CADDY = Target(api_version="", kind="Deployment", name="caddy", namespace="shop")

DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: caddy
spec:
  replicas: 1
  selector:
    matchLabels: {app: caddy}
  template:
    metadata:
      labels: {app: caddy}
    spec:
      containers:
        - name: caddy
          image: caddy:2.8.4
"""
SERVICE = """\
apiVersion: v1
kind: Service
metadata:
  name: caddy
spec:
  selector: {app: caddy}
  ports:
    - port: 80
"""
BOTH = f"{DEPLOYMENT}---\n{SERVICE}"

needs_kustomize = pytest.mark.skipif(
    shutil.which(kustomize_bin()) is None and not os.environ.get("CI"),
    reason="kustomize 가 없다 (CI 에서는 깔고 돈다)")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURE, copy)
    return copy


def plan(repo: Path, manifest: str = BOTH, *, app: AppSource = PLAIN, target: Target = CADDY,
         file_name: str | None = None) -> Ready | Refused:
    return plan_new_resource(repo, app, target, NewResource(application="shop-web", manifest_yaml=manifest,
                                                            file_name=file_name))


def ready(result: Ready | Refused) -> Ready:
    assert isinstance(result, Ready), result
    return result


def refused(result: Ready | Refused, code: str) -> Refused:
    assert isinstance(result, Refused), result
    assert result.code == code, result
    return result


def added(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def removed(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]


def pod(*, image: str = "caddy:2.8.4", container: str = "", spec: str = "") -> str:
    """컨테이너·파드 칸을 하나 더한 Deployment. 들여쓰기는 컨테이너 칸 10칸, 파드 칸 6칸이다."""
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: caddy
spec:
  selector:
    matchLabels: {{app: caddy}}
  template:
    metadata:
      labels: {{app: caddy}}
    spec:
      containers:
        - name: caddy
          image: {image}
{container}{spec}"""


# ── plain (Directory) ────────────────────────────────────


def test_plain_새_Deployment와_Service를_새_파일_하나로_더한다(repo):
    result = ready(plan(repo))

    assert list(result.files) == ["apps/plain/caddy.yaml"]
    assert result.files["apps/plain/caddy.yaml"] == BOTH
    assert result.file_diff.startswith("--- /dev/null\n+++ b/apps/plain/caddy.yaml\n")
    assert removed(result.file_diff) == [] and "kind: Service" in added(result.file_diff)
    # 렌더 비교: 클러스터에 새로 생길 것 두 개만 — 기존 web·worker 는 나오지 않는다
    assert "/dev/null" in result.render_diff and "kind: Deployment" in added(result.render_diff)
    assert "Deployment caddy (머지 후)" in result.render_diff and "Service caddy (머지 후)" in result.render_diff
    assert "web" not in result.render_diff
    assert result.new_resources == [
        {"api_version": "apps/v1", "kind": "Deployment", "name": "caddy", "namespace": "shop", "file": "apps/plain/caddy.yaml"},
        {"api_version": "v1", "kind": "Service", "name": "caddy", "namespace": "shop", "file": "apps/plain/caddy.yaml"},
    ]
    assert result.risk_warnings == [] and result.warnings == []
    assert (repo / "apps/plain/caddy.yaml").exists() is False            # 레포 폴더는 읽기만 한다


def test_file_name_을_주면_그_이름으로_더한다(repo):
    assert list(ready(plan(repo, file_name="edge.yml")).files) == ["apps/plain/edge.yml"]


def test_recurse_앱은_하위_폴더에_더할_수_있다(repo):
    assert list(ready(plan(repo, app=RECURSE, file_name="edge/caddy.yaml")).files) == ["apps/plain/edge/caddy.yaml"]


def test_recurse_가_꺼진_앱은_하위_폴더_파일을_못_읽는다고_알려준다(repo):
    result = refused(plan(repo, file_name="edge/caddy.yaml"), "invalid_manifest")
    assert "recurse" in result.message and result.retry


# ── Kustomize ────────────────────────────────────────────


@needs_kustomize
def test_kustomize_그_폴더에_새_파일과_resources_한_줄을_더하고_base는_그대로다(repo):
    target = CADDY.model_copy(update={"namespace": "shop-prod"})
    result = ready(plan(repo, app=PROD, target=target))

    assert sorted(result.files) == ["apps/kustomize/overlays/prod/caddy.yaml", "apps/kustomize/overlays/prod/kustomization.yaml"]
    assert not any("base" in path for path in result.files)
    assert removed(result.file_diff) == []
    assert "  - caddy.yaml" in added(result.file_diff)
    assert result.files["apps/kustomize/overlays/prod/kustomization.yaml"].startswith("# Application 이 여기")   # 주석 유지
    # overlay 의 namespace 가 렌더 결과에 들어간다 — 경고도 그 값으로 본다
    assert {r["namespace"] for r in result.new_resources} == {"shop-prod"}
    assert "Deployment shop-prod/caddy (머지 후)" in result.render_diff
    assert result.risk_warnings == []


@needs_kustomize
def test_kustomize_namePrefix_가_붙어도_요청한_리소스로_본다(repo):
    file = repo / "apps/kustomize/overlays/prod/kustomization.yaml"
    file.write_text(file.read_text(encoding="utf-8") + "namePrefix: prod-\n", encoding="utf-8")
    result = ready(plan(repo, app=PROD))

    assert {r["name"] for r in result.new_resources} == {"prod-caddy"}


@needs_kustomize
def test_kustomize_namePrefix_와_이름이_겹치는_리소스를_같이_더해도_막지_않는다(repo):
    file = repo / "apps/kustomize/overlays/prod/kustomization.yaml"
    file.write_text(file.read_text(encoding="utf-8") + "namePrefix: x-\n", encoding="utf-8")
    worker = pod().replace("name: caddy\nspec", "name: caddy-worker\nspec")
    result = ready(plan(repo, f"{pod()}---\n{worker}", app=PROD))

    assert {r["name"] for r in result.new_resources} == {"x-caddy", "x-caddy-worker"}


@needs_kustomize
def test_kustomize_images_가_태그를_정하면_렌더_결과로_보고_경고하지_않는다(repo):
    manifest = pod(image="public.ecr.aws/docker/library/nginx")          # overlay 의 images 가 태그를 붙인다
    result = ready(plan(repo, manifest, app=PROD, target=CADDY.model_copy(update={"namespace": "shop-prod"})))

    assert result.risk_warnings == []


@needs_kustomize
def test_다른_환경이_base로_쓰는_폴더면_막고_overlay_앱을_고르게_한다(repo):
    result = refused(plan(repo, app=BASE), "render_mismatch")
    assert "다른 환경도 같이 바뀌어요" in result.message and "overlay" in result.message


# ── 막음 ─────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["../other.yaml", "../../etc/x.yaml", "/etc/x.yaml", "edge/../../x.yaml", "..\\x.yaml"])
def test_앱_폴더_밖에는_쓰지_않는다(repo, name):
    result = refused(plan(repo, app=RECURSE, file_name=name), "outside_app")
    assert "폴더" in result.message and result.retry


def test_앱_밖을_가리키는_링크_폴더로도_나가지_않는다(repo):
    (repo / "apps/plain/out").symlink_to(repo / "apps/kustomize")
    refused(plan(repo, app=RECURSE, file_name="out/x.yaml"), "outside_app")


def test_기존_파일은_덮어쓰지_않고_다른_이름을_권한다(repo):
    result = refused(plan(repo, file_name="service.yaml"), "overwrite")
    assert "다른 파일 이름" in result.message


def test_이름을_안_줘도_같은_이름_파일이_있으면_덮어쓰지_않는다(repo):
    (repo / "apps/plain/caddy.yaml").write_text("# 사람이 만든 파일\n", encoding="utf-8")
    refused(plan(repo), "overwrite")


def test_끊어진_링크_자리에도_쓰지_않는다(repo):
    (repo / "apps/plain/caddy.yaml").symlink_to(repo / "nowhere.yaml")
    refused(plan(repo), "overwrite")


def test_이미_앱에_있는_리소스는_새로_만들지_않고_고치는_길을_알려준다(repo):
    manifest = pod().replace("name: caddy\nspec", "name: web\nspec")
    result = refused(plan(repo, manifest, target=CADDY.model_copy(update={"name": "web"})), "resource_exists")
    assert "replicas" in result.message and "GitHub" in result.message


@pytest.mark.parametrize("content", ["stringData: {password: hunter2}", "data: {password: aHVudGVyMg==}"])
def test_값이_든_Secret은_막고_ExternalSecret을_권한다(repo, content):
    manifest = f"apiVersion: v1\nkind: Secret\nmetadata: {{name: caddy}}\n{content}\n"
    result = refused(plan(repo, manifest, target=CADDY.model_copy(update={"kind": "Secret"})), "secret")
    assert "ExternalSecret" in result.message and result.retry
    assert "hunter2" not in result.message and "aHVudGVyMg" not in result.message


def test_문서_여럿_중_하나라도_값이_든_Secret이면_막는다(repo):
    manifest = f"{DEPLOYMENT}---\napiVersion: v1\nkind: Secret\nmetadata: {{name: db}}\nstringData: {{a: b}}\n"
    refused(plan(repo, manifest), "secret")


def test_값이_없는_Secret과_ExternalSecret은_막지_않는다(repo):
    manifest = ("apiVersion: v1\nkind: Secret\nmetadata:\n  name: caddy\n  annotations: "
                "{kubernetes.io/service-account.name: caddy}\ntype: kubernetes.io/service-account-token\n"
                "---\napiVersion: external-secrets.io/v1beta1\nkind: ExternalSecret\nmetadata: {name: db}\n"
                "spec: {secretStoreRef: {name: vault, kind: SecretStore}}\n")
    ready(plan(repo, manifest, target=CADDY.model_copy(update={"kind": "Secret"})))


@pytest.mark.parametrize("api_version, kind", [
    ("kustomize.config.k8s.io/v1beta1", "Kustomization"),
    ("kustomize.config.k8s.io/v1alpha1", "Component"),
    ("kustomize.toolkit.fluxcd.io/v1", "Kustomization"),
])
def test_Kustomization_종류는_더하지_않는다(repo, api_version, kind):
    manifest = f"apiVersion: {api_version}\nkind: {kind}\nmetadata: {{name: caddy}}\nresources: [https://evil.example/x.yaml]\n"
    result = refused(plan(repo, manifest, target=CADDY.model_copy(update={"kind": kind})), "kustomize_config")
    assert "YAML 을 직접" in result.message


@pytest.mark.parametrize("name", ["kustomization.yaml", "kustomization.yml", "Chart.yaml"])
def test_ArgoCD가_앱_형식을_다르게_읽는_파일_이름은_쓰지_않는다(repo, name):
    refused(plan(repo, file_name=name), "kustomize_config")


def test_레포의_kustomization이_원격을_끌어오면_렌더하지_않고_막는다(repo):
    file = repo / "apps/kustomize/overlays/prod/kustomization.yaml"
    file.write_text(file.read_text(encoding="utf-8").replace(
        "  - ../../base\n", "  - ../../base\n  - https://github.com/acme/remote//base?ref=main\n"), encoding="utf-8")
    result = refused(plan(repo, app=PROD), "render_failed")
    assert "레포 밖 주소" in result.message and "먼저 고치거나" in result.message


def test_지금_레포가_렌더되지_않으면_막는다(repo):
    (repo / "apps/plain/broken.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    result = refused(plan(repo), "render_failed")
    assert "지금 레포를 렌더링하지 못했어요" in result.message


def test_더한_뒤_렌더가_안_되면_막는다(repo):
    result = refused(plan(repo, f"{DEPLOYMENT}---\n{DEPLOYMENT}"), "render_failed")
    assert "새 파일을 더하면 렌더링이 안 돼요" in result.message and "두 번" in result.message


@needs_kustomize
def test_kustomize_base에서_온_리소스도_이미_있는_리소스로_본다(repo):
    manifest = pod().replace("name: caddy\nspec", "name: api\nspec")
    refused(plan(repo, manifest, app=PROD, target=CADDY.model_copy(update={"name": "api"})), "resource_exists")


@needs_kustomize
def test_kustomize_격리_렌더가_이_컴퓨터_안_주소를_막으면_다음_방법과_함께_알려준다(repo):
    """격리 렌더(agent#128)는 레포의 어느 파일이든 localhost 주소가 있으면 돌리지 않는다 — 새 파일도 같다."""
    manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: caddy}\ndata: {upstream: 'http://localhost:8080'}\n"
    result = refused(plan(repo, manifest, app=PROD, target=CADDY.model_copy(update={"kind": "ConfigMap"})), "render_failed")
    assert "이 컴퓨터 안 주소" in result.message and "GitHub" in result.message


def _objects(*docs: str) -> dict:
    return index([doc for text in docs for doc in yaml.safe_load_all(text)])


WEB = "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web}\nspec: {replicas: 1}\n"


def test_렌더_비교에서_기존_리소스가_바뀌면_막는다():
    before = _objects(WEB)
    after = _objects(WEB.replace("replicas: 1", "replicas: 2"), DEPLOYMENT)
    result = refused(_compare(list(yaml.safe_load_all(DEPLOYMENT)), before, after), "render_mismatch")
    assert "기존 리소스가 바뀌어요: Deployment web" in result.message


def test_렌더_비교에서_기존_리소스가_사라져도_막는다():
    result = refused(_compare(list(yaml.safe_load_all(DEPLOYMENT)), _objects(WEB), _objects(DEPLOYMENT)), "render_mismatch")
    assert "기존 리소스가 바뀌어요" in result.message


def test_렌더_비교에서_요청하지_않은_리소스가_생기면_막는다():
    result = refused(_compare(list(yaml.safe_load_all(DEPLOYMENT)), {}, _objects(DEPLOYMENT, SERVICE)), "render_mismatch")
    assert "요청하지 않은 리소스도 생겨요: Service caddy" in result.message


@pytest.mark.parametrize("names", [("api", "api-worker"), ("api-worker", "api")])
@pytest.mark.parametrize("rendered", [("x-api", "x-api-worker"), ("x-api-worker", "x-api")])
def test_렌더_이름이_서로를_품어도_짝을_맞게_찾는다(names, rendered):
    def doc(name: str) -> str:
        return f"apiVersion: apps/v1\nkind: Deployment\nmetadata: {{name: {name}}}\n"

    created = _compare([yaml.safe_load(doc(name)) for name in names], {}, _objects(*map(doc, rendered)))
    assert [key[3] for key, _ in created] == [f"x-{name}" for name in names]


def test_렌더_결과에_요청한_리소스가_없으면_막는다():
    result = refused(_compare(list(yaml.safe_load_all(DEPLOYMENT)), {}, {}), "render_mismatch")
    assert "머지해도 클러스터에 안 생겨요" in result.message


@pytest.mark.parametrize("manifest, why", [
    ("a: [unclosed\n", "YAML"),
    ("# 주석만\n", "리소스가 없어요"),
    ("- just\n- a list\n", "metadata.name"),
    ("apiVersion: v1\nkind: ConfigMap\ndata: {}\n", "metadata.name"),
    ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: --kubeconfig=/x}\n", "metadata.name"),
    ("apiVersion: v1\nkind: --flag\nmetadata: {name: caddy}\n", "metadata.name"),
    ("apiVersion: v1\nkind: List\nmetadata: {name: caddy}\nitems: []\n", "문서(---)"),
    ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: other}\n", "manifest_yaml 에 없어요"),
])
def test_리소스로_못_읽는_입력은_고쳐_달라고_한다(repo, manifest, why):
    result = refused(plan(repo, manifest), "invalid_manifest")
    assert why in result.message and result.retry


@pytest.mark.parametrize("api_version, kind, name", [
    ("apps/v1", "Deployment", "CADDY"),
    ("apps/v1", "Deployment", "bad_name"),
    ("apps/v1", "Deployment", "bad:name"),
    ("apps/v1", "Deployment", "bad-"),
    ("apps/v1", "Deployment", "a" * 254),
    ("v1", "Service", "1caddy"),
    ("v1", "Service", "my.caddy"),
    ("v1", "Service", "a" * 64),
    ("v1", "Namespace", "my.caddy"),
    ("rbac.authorization.k8s.io/v1", "ClusterRole", "a" * 254),
])
def test_쿠버네티스가_받지_않는_이름은_PR을_만들지_않고_고칠_방법을_알려준다(repo, api_version, kind, name):
    manifest = f"apiVersion: {api_version}\nkind: {kind}\nmetadata: {{name: {name}}}\n"
    result = refused(plan(repo, manifest, target=CADDY.model_copy(update={"kind": kind, "name": name})), "invalid_manifest")
    assert result.retry and "고쳐 다시 제안해 주세요" in result.message and "자 안에서" in result.message
    assert len(result.message) < 200                                    # 긴 이름을 그대로 싣지 않는다


@pytest.mark.parametrize("api_version, kind, name", [
    ("apps/v1", "Deployment", "my.caddy-1"),
    ("apps/v1", "Deployment", "1caddy"),
    ("apps/v1", "Deployment", "a" * 253),
    ("v1", "Service", "a" * 63),
    ("rbac.authorization.k8s.io/v1", "ClusterRole", "system:Caddy_view"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding", "system:caddy"),
])
def test_쿠버네티스가_받는_이름은_막지_않는다(repo, api_version, kind, name):
    manifest = f"apiVersion: {api_version}\nkind: {kind}\nmetadata: {{name: '{name}'}}\n"
    result = ready(plan(repo, manifest, target=CADDY.model_copy(update={"kind": kind, "name": name})))
    assert result.new_resources[0]["name"] == name
    assert all(len(path.rsplit("/", 1)[-1]) <= 200 for path in result.files)   # 긴 이름도 파일 시스템 한도 안으로


@pytest.mark.parametrize("namespace", ["Shop", "my.shop", "a" * 64, "123"])
def test_쿠버네티스가_받지_않는_네임스페이스도_고칠_방법을_알려준다(repo, namespace):
    # 따옴표 없는 123 은 숫자로 읽힌다 — 글자가 아닌 네임스페이스도 거른다
    manifest = SERVICE.replace("name: caddy\n", f"name: caddy\n  namespace: {namespace}\n")
    result = refused(plan(repo, manifest, target=CADDY.model_copy(update={"kind": "Service"})), "invalid_manifest")
    assert "metadata.namespace" in result.message and "지워" in result.message


def test_YAML_오류_문구에_값을_싣지_않는다(repo):
    result = refused(plan(repo, 'password: "hunter2\n'), "invalid_manifest")
    assert "hunter2" not in result.message


@pytest.mark.parametrize("name", [".argocd-source.yaml", ".git/config.yaml", "caddy.txt", "caddy"])
def test_파일_이름_모양이_틀리면_고쳐_달라고_한다(repo, name):
    refused(plan(repo, app=RECURSE, file_name=name), "invalid_manifest")


def test_너무_긴_파일_이름은_고쳐_달라고_한다(repo):
    assert "너무 길어요" in refused(plan(repo, file_name="a" * 300 + ".yaml"), "invalid_manifest").message


def test_다른_파일_아래로는_못_쓴다(repo):
    refused(plan(repo, app=RECURSE, file_name="service.yaml/x.yaml"), "invalid_manifest")


def test_Helm_앱은_못_더한다(repo):
    result = refused(plan(repo, app=AppSource(source_type="Helm", path="apps/plain")), "unsupported_source")
    assert not result.retry


def test_앱_폴더가_없으면_못_더한다(repo):
    refused(plan(repo, app=AppSource(source_type="Directory", path="apps/nope")), "invalid_path")


# ── 경고 — 막지 않고 위치·위험·대안을 준다 ────────────────


def risk_of(result: Ready | Refused, code: str) -> dict:
    found = [risk for risk in ready(result).risk_warnings if risk.code == code]
    assert len(found) == 1, ready(result).risk_warnings
    return found[0].model_dump()


FIELD = "spec.template.spec.containers[caddy]"


@pytest.mark.parametrize("manifest, code, field, value", [
    (pod(image="caddy:latest"), "latest_image", f"{FIELD}.image", "caddy:latest"),
    (pod(image="caddy"), "untagged_image", f"{FIELD}.image", "caddy"),
    (pod(image="registry.local:5000/caddy"), "untagged_image", f"{FIELD}.image", "registry.local:5000/caddy"),
    (pod(container="          securityContext: {privileged: true}\n"), "privileged",
     f"{FIELD}.securityContext.privileged", "true"),
    (pod(spec="      hostNetwork: true\n"), "host_network", "spec.template.spec.hostNetwork", "true"),
    (pod(spec="      volumes:\n        - name: docker\n          hostPath: {path: /var/run/docker.sock}\n"), "host_path",
     "spec.template.spec.volumes[docker].hostPath", "/var/run/docker.sock"),
    (pod(container="          env:\n            - {name: DB_PASSWORD, value: hunter2}\n"), "plaintext_env",
     f"{FIELD}.env[DB_PASSWORD].value", None),
])
def test_위험한_설정은_위치_위험_대안과_함께_경고한다(repo, manifest, code, field, value):
    result = plan(repo, manifest)
    risk = risk_of(result, code)

    assert risk["location"] == {"file": "apps/plain/caddy.yaml", "resource": "Deployment shop/caddy", "field": field, "value": value}
    assert risk["risk"] and risk["suggestion"]
    assert risk["text"] in ready(result).warnings                       # 옛 앱도 같은 경고를 본다
    assert "hunter2" not in str(ready(result).risk_warnings)            # 비밀 값은 경고에도 안 싣는다


@pytest.mark.parametrize("manifest, kind", [
    ("apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata: {name: caddy}\nrules: []\n", "ClusterRole"),
    ("apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata: {name: caddy}\n", "ClusterRoleBinding"),
    ("apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata: {name: caddy}\n", "CustomResourceDefinition"),
    ("apiVersion: v1\nkind: Namespace\nmetadata: {name: caddy}\n", "Namespace"),
    ("apiVersion: cert-manager.io/v1\nkind: ClusterIssuer\nmetadata: {name: caddy}\n", "ClusterIssuer"),
])
def test_클러스터_범위_리소스는_경고한다(repo, manifest, kind):
    result = plan(repo, manifest, target=CADDY.model_copy(update={"kind": kind}))
    risk = risk_of(result, "cluster_scoped")
    assert risk["location"]["field"] == "kind" and risk["location"]["value"] == kind
    assert risk["location"]["resource"] == f"{kind} caddy"
    assert ready(result).new_resources[0]["namespace"] == ""            # destination 이 있어도 네임스페이스가 없다


def test_Cluster로_시작해도_네임스페이스가_있는_종류는_그_네임스페이스를_그대로_둔다(repo):
    manifest = ("apiVersion: operators.coreos.com/v1alpha1\nkind: ClusterServiceVersion\n"
                "metadata: {name: caddy, namespace: other}\n")
    result = plan(repo, manifest, target=CADDY.model_copy(update={"kind": "ClusterServiceVersion"}))

    assert ready(result).new_resources[0]["namespace"] == "other"
    assert [risk.code for risk in ready(result).risk_warnings] == ["other_namespace"]


def test_앱과_다른_네임스페이스는_경고한다(repo):
    manifest = SERVICE.replace("name: caddy\n", "name: caddy\n  namespace: other\n")
    risk = risk_of(plan(repo, manifest, target=CADDY.model_copy(update={"kind": "Service"})), "other_namespace")
    assert risk["location"]["value"] == "other" and "shop" in risk["risk"]


def test_앱과_같은_네임스페이스와_적지_않은_네임스페이스는_경고하지_않는다(repo):
    same = SERVICE.replace("name: caddy\n", "name: caddy\n  namespace: shop\n")
    target = CADDY.model_copy(update={"kind": "Service"})
    assert ready(plan(repo, same, target=target)).risk_warnings == []
    assert ready(plan(repo, SERVICE, target=target)).risk_warnings == []


def test_앱의_destination이_비어_있으면_네임스페이스를_비교하지_않는다(repo):
    manifest = SERVICE.replace("name: caddy\n", "name: caddy\n  namespace: other\n")
    target = Target(api_version="", kind="Service", name="caddy", namespace=None)
    assert ready(plan(repo, manifest, target=target)).risk_warnings == []


@needs_kustomize
def test_kustomize는_렌더된_네임스페이스로_앱과_비교한다(repo):
    manifest = SERVICE.replace("name: caddy\n", "name: caddy\n  namespace: other\n")
    target = CADDY.model_copy(update={"kind": "Service"})      # destination shop, overlay 가 shop-prod 로 바꾼다
    assert risk_of(plan(repo, manifest, app=PROD, target=target), "other_namespace")["location"]["value"] == "shop-prod"


@pytest.mark.parametrize("image", ["caddy:2.8.4", "caddy@sha256:" + "a" * 64, "caddy:latest@sha256:" + "a" * 64])
def test_버전을_고정한_이미지는_경고하지_않는다(repo, image):
    assert ready(plan(repo, pod(image=image))).risk_warnings == []


def test_initContainer와_CronJob과_커스텀_리소스의_파드_스펙도_본다(repo):
    init = pod(spec="      initContainers:\n        - {name: setup, image: 'busybox:latest'}\n")
    assert risk_of(plan(repo, init), "latest_image")["location"]["field"] == "spec.template.spec.initContainers[setup].image"

    cron = ("apiVersion: batch/v1\nkind: CronJob\nmetadata: {name: caddy}\nspec:\n  schedule: '* * * * *'\n  jobTemplate:\n"
            "    spec:\n      template:\n        spec:\n          containers:\n            - {name: a, image: 'b:latest'}\n")
    assert risk_of(plan(repo, cron, target=CADDY.model_copy(update={"kind": "CronJob"})), "latest_image")

    rollout = pod(spec="      hostNetwork: true\n").replace("apps/v1", "argoproj.io/v1alpha1").replace("kind: Deployment", "kind: Rollout")
    assert risk_of(plan(repo, rollout, target=CADDY.model_copy(update={"kind": "Rollout"})), "host_network")


def test_종류를_좁히지_않는다_모르는_커스텀_리소스도_일반_승인으로_간다(repo):
    manifest = "apiVersion: acme.example/v1\nkind: Widget\nmetadata: {name: caddy}\nspec: {size: 3}\n"
    result = ready(plan(repo, manifest, target=CADDY.model_copy(update={"kind": "Widget"})))
    assert result.risk_warnings == [] and result.new_resources[0]["kind"] == "Widget"


def test_글자나_스키마_속_같은_단어는_경고하지_않는다(repo):
    """CRD 스키마의 hostPath·ConfigMap 안의 글자 — 실제 설정이 아니다."""
    config = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: caddy}\n"
              "data: {note: 'hostNetwork: true, privileged: true, image: x:latest', TOKEN_TTL: '300'}\n")
    assert ready(plan(repo, config, target=CADDY.model_copy(update={"kind": "ConfigMap"}))).risk_warnings == []

    crd = ("apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata: {name: caddy}\nspec:\n  versions:\n"
           "    - name: v1\n      schema:\n        openAPIV3Schema:\n          properties:\n            volumes:\n"
           "              items:\n                properties:\n                  hostPath: {type: object}\n"
           "                  hostNetwork: {type: boolean}\n")
    risks = ready(plan(repo, crd, target=CADDY.model_copy(update={"kind": "CustomResourceDefinition"}))).risk_warnings
    assert [risk.code for risk in risks] == ["cluster_scoped"]


def test_include_exclude가_있는_앱은_새_파일이_안_읽힐_수_있다고_경고한다(repo):
    app = PLAIN.model_copy(update={"directory_filtered": True})
    risk = risk_of(plan(repo, app=app), "directory_filter")
    assert risk["location"]["field"] == "spec.source.directory"


def test_경고가_여럿이면_모두_싣고_막지_않는다(repo):
    manifest = pod(image="caddy:latest", container="          securityContext: {privileged: true}\n", spec="      hostNetwork: true\n")
    result = ready(plan(repo, manifest))
    assert {risk.code for risk in result.risk_warnings} == {"latest_image", "privileged", "host_network"}
    assert len(result.warnings) == 3
