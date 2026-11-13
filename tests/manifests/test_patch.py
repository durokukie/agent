"""경로 지정 변경(what=patch) — DURO-249.

픽스처 레포(tests/fixtures/manifests 의 apps/plain — kukie-gitops-sandbox 의 plain 앱과 같은 모양)에 넷 밖의 칸을 경로로 걸어
올바른 줄만 바뀌는지, 틀린 경로·바꾸면 안 되는 칸·요청 밖 변화는 거절되는지, 위험한 값은 경고로 올라오는지 본다.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kukie_agent.manifests import (
    AppSource, Patch, PatchOperation, Ready, Refused, Target, plan_change, verify_change,
)
from kukie_agent.manifests import compare
from kukie_agent.manifests import patch as patching

FIXTURE = Path(__file__).parents[1] / "fixtures" / "manifests"
PLAIN = AppSource(source_type="Directory", path="apps/plain")
PROD = AppSource(source_type="Kustomize", path="apps/kustomize/overlays/prod")
WEB = Target(api_version="apps/v1", kind="Deployment", name="web", namespace="shop")
SVC = Target(api_version="v1", kind="Service", name="web", namespace="shop")
API = Target(api_version="apps/v1", kind="Deployment", name="api", namespace="shop-prod")
FILE = "apps/plain/deployment.yaml"
CONTAINER = "spec.template.spec.containers[web]"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURE, copy)
    return copy


def ops(*items: tuple) -> Patch:
    """("set", 경로, 값) · ("remove", 경로) 목록 → Patch."""
    return Patch(operations=[PatchOperation(op=item[0], path=item[1], value=item[2] if len(item) > 2 else None)
                             for item in items])


def ready(result: Ready | Refused) -> Ready:
    assert isinstance(result, Ready), result
    return result


def refused(result: Ready | Refused, code: str) -> Refused:
    assert isinstance(result, Refused), result
    assert result.code == code, result
    return result


def doc_after(result: Ready, path: str = FILE) -> dict:
    return yaml.safe_load(result.files[path])


def changed_lines(diff: str) -> tuple[list[str], list[str]]:
    lines = [line.rstrip("\n") for line in diff.splitlines()]
    return ([line[1:] for line in lines if line.startswith("-") and not line.startswith("---")],
            [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")])


# ── 되는 것 ──────────────────────────────────────────────


def test_없던_칸을_더하면_그_컨테이너_아래에만_생기고_주석은_그대로다(repo):
    probe = {"httpGet": {"path": "/", "port": 80}, "periodSeconds": 10}
    result = ready(plan_change(repo, PLAIN, WEB, ops(("set", f"{CONTAINER}.readinessProbe", probe))))

    assert list(result.files) == [FILE]
    assert doc_after(result)["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] == probe
    removed, added = changed_lines(result.file_diff)
    assert removed == []
    assert added == ["          readinessProbe:", "            httpGet:", "              path: /", "              port: 80",
                     "            periodSeconds: 10"]
    text = result.files[FILE]
    assert text.startswith("# 웹 화면.") and "# 비밀번호는 Secret 에서 온다" in text and "labels: {app: web}" in text
    assert result.risk_warnings == []


def test_이름_없는_목록은_통째로_바꾼다(repo):
    ports = [{"containerPort": 80}, {"containerPort": 9090, "name": "metrics"}]
    result = ready(plan_change(repo, PLAIN, WEB, ops(("set", f"{CONTAINER}.ports", ports))))
    assert doc_after(result)["spec"]["template"]["spec"]["containers"][0]["ports"] == ports


def test_점_슬래시가_든_키는_따옴표로_적는다(repo):
    result = ready(plan_change(repo, PLAIN, WEB, ops(
        ("set", 'metadata.labels["app.kubernetes.io/part-of"]', "shop"))))
    assert doc_after(result)["metadata"]["labels"] == {"app": "web", "app.kubernetes.io/part-of": "shop"}


def test_지우기와_바꾸기를_한_번에(repo):
    result = ready(plan_change(repo, PLAIN, WEB, ops(
        ("remove", f"{CONTAINER}.resources.limits"),
        ("set", f"{CONTAINER}.args", ["-g", "daemon off;"]),
    )))
    container = doc_after(result)["spec"]["template"]["spec"]["containers"][0]
    assert "limits" not in container["resources"] and container["args"] == ["-g", "daemon off;"]


def test_이름_있는_항목을_통째로_바꿀_때는_이름이_그대로여야_한다(repo):
    refused(plan_change(repo, PLAIN, WEB, ops(("set", CONTAINER, {"name": "other", "image": "nginx:1"}))),
            "invalid_patch")


def test_Service_의_포트도_바꾼다(repo):
    ports = [{"port": 80, "targetPort": 8080}]
    result = ready(plan_change(repo, PLAIN, SVC, ops(("set", "spec.ports", ports))))
    assert list(result.files) == ["apps/plain/service.yaml"]
    assert doc_after(result, "apps/plain/service.yaml")["spec"]["ports"] == ports


# ── 경로가 틀림 → 다시 고르게 (retry) ──────────────────────


@pytest.mark.parametrize("path", [
    f"{CONTAINER}.ports[0]",                       # 번호로 가리킴
    "spec.template.spec.containers[nope].image",   # 없는 이름
    "spec.tempalte.spec.hostNetwork",              # 위 칸 오타
    f"{CONTAINER}.securityContext.runAsNonRoot",   # 위 칸이 아직 없음
    "spec..replicas",
    "[web].image",
])
def test_경로가_이_리소스에_맞지_않으면_다시_고르게_한다(repo, path):
    result = refused(plan_change(repo, PLAIN, WEB, ops(("set", path, True))), "invalid_patch")
    assert result.retry


def test_위_칸이_없으면_위_칸부터_통째로_set_하면_된다(repo):
    refused(plan_change(repo, PLAIN, WEB, ops(("set", f"{CONTAINER}.securityContext.runAsNonRoot", True))),
            "invalid_patch")
    result = ready(plan_change(repo, PLAIN, WEB, ops(
        ("set", f"{CONTAINER}.securityContext", {"runAsNonRoot": True}))))
    assert doc_after(result)["spec"]["template"]["spec"]["containers"][0]["securityContext"] == {"runAsNonRoot": True}


def test_없는_칸은_지울_수_없다(repo):
    refused(plan_change(repo, PLAIN, WEB, ops(("remove", f"{CONTAINER}.livenessProbe"))), "invalid_patch")


def test_겹치는_경로는_한_번만(repo):
    refused(plan_change(repo, PLAIN, WEB, ops(
        ("set", f"{CONTAINER}.resources", {}), ("set", f"{CONTAINER}.resources.limits", {"memory": "1Gi"}),
    )), "invalid_patch")


@pytest.mark.parametrize("path", ["kind", "apiVersion", "metadata.name", "metadata.namespace", "metadata",
                                  "status.replicas", "metadata.ownerReferences"])
def test_리소스를_가리키는_칸과_metadata_는_못_바꾼다(repo, path):
    refused(plan_change(repo, PLAIN, WEB, ops(("set", path, "x"))), "invalid_patch")


def test_set_에는_값이_remove_에는_값이_없어야_한다():
    with pytest.raises(ValidationError):
        PatchOperation(op="set", path="spec.replicas")
    with pytest.raises(ValidationError):
        PatchOperation(op="remove", path="spec.replicas", value=1)
    with pytest.raises(ValidationError):
        Patch(operations=[])


# ── 바꾸면 안 되는 칸 ─────────────────────────────────────


def test_만든_뒤_못_바꾸는_칸은_막는다(repo):
    result = refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec.selector.matchLabels", {"app": "web2"}))),
                     "guarded_field")
    # 경로를 바꿔 다시 해도 풀리지 않는다 — 다시 고르게 하지도, kubectl 길로 보내지도 않는다 (pull_requests._STOP)
    assert "selector" in result.message and not result.retry


def test_위_칸을_통째로_바꿔도_못_바꾸는_칸이_그대로면_된다(repo):
    """막는 기준은 경로가 아니라 결과다 — spec 을 통째로 set 해도 selector 값이 같으면 된다."""
    spec = yaml.safe_load((repo / FILE).read_text(encoding="utf-8"))["spec"]
    spec["replicas"] = 2
    result = ready(plan_change(repo, PLAIN, WEB, ops(("set", "spec", spec))))
    assert doc_after(result)["spec"]["replicas"] == 2


def test_셀렉터가_고르는_파드_레이블을_빼면_막는다(repo):
    refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec.template.metadata.labels", {"tier": "front"}))),
            "guarded_field")
    ready(plan_change(repo, PLAIN, WEB, ops(("set", "spec.template.metadata.labels", {"app": "web", "tier": "front"}))))


def test_이름_있는_항목을_지운다(repo):
    worker = Target(api_version="apps/v1", kind="Deployment", name="worker", namespace="shop")
    result = ready(plan_change(repo, PLAIN, worker, ops(("remove", "spec.template.spec.containers[metrics]"))))
    doc = next(d for d in yaml.safe_load_all(result.files["apps/plain/worker.yaml"]) if d["kind"] == "Deployment")
    assert [c["name"] for c in doc["spec"]["template"]["spec"]["containers"]] == ["worker"]
    assert result.files["apps/plain/worker.yaml"].startswith("# 일감 처리기.")


def test_ArgoCD_추적_표시는_못_바꾼다(repo):
    refused(plan_change(repo, PLAIN, WEB, ops(("set", 'metadata.labels["app.kubernetes.io/instance"]', "x"))),
            "guarded_field")
    # 어노테이션이 아직 없으면 위 칸(annotations)부터 통째로 set 한다 — 그래도 결과로 막는다
    refused(plan_change(repo, PLAIN, WEB, ops(("set", "metadata.annotations", {"argocd.argoproj.io/tracking-id": "x"}))),
            "guarded_field")
    # 다른 어노테이션은 된다
    ready(plan_change(repo, PLAIN, WEB, ops(("set", "metadata.annotations", {"team": "shop"}))))


def test_Service_의_clusterIP_는_못_바꾼다(repo):
    refused(plan_change(repo, PLAIN, SVC, ops(("set", "spec.clusterIP", "10.0.0.9"))), "guarded_field")


def test_비밀처럼_보이는_환경변수에_평문을_넣지_않는다(repo):
    env = [{"name": "GREETING", "value": "hello"}, {"name": "DB_PASSWORD", "value": "hunter2"}]
    refused(plan_change(repo, PLAIN, WEB, ops(("set", f"{CONTAINER}.env", env))), "secret")


def test_init_컨테이너의_평문_비밀도_막는다(repo):
    init = [{"name": "migrate", "image": "busybox:1.36", "env": [{"name": "DB_PASSWORD", "value": "hunter2"}]}]
    refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec.template.spec.initContainers", init))), "secret")


def test_Secret_에서_가져오던_환경변수를_평문으로_바꾸지_않는다(repo):
    """이름(DB_URL)이 비밀처럼 보이지 않아도 secretKeyRef 였던 항목이 바뀌거나 사라지면 막는다 — 넷의 env 요청과 같다."""
    refused(plan_change(repo, PLAIN, WEB, ops(("set", f"{CONTAINER}.env[DB_URL]",
                                              {"name": "DB_URL", "value": "postgres://u:p@db/shop"}))), "secret")
    refused(plan_change(repo, PLAIN, WEB, ops(("remove", f"{CONTAINER}.env[DB_URL]"))), "secret")
    refused(plan_change(repo, PLAIN, WEB, ops(("set", f"{CONTAINER}.env", [{"name": "GREETING", "value": "hi"}]))),
            "secret")


def test_HPA_가_맡은_레플리카는_경로_지정으로도_못_바꾼다(repo):
    result = refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec.replicas", 3)), hpa="web"), "hpa")
    assert "HPA(web)" in result.message
    spec = yaml.safe_load((repo / FILE).read_text(encoding="utf-8"))["spec"]
    refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec", {**spec, "replicas": 3})), hpa="web"), "hpa")
    # 레플리카를 건드리지 않으면 된다
    ready(plan_change(repo, PLAIN, WEB, ops(("set", "spec.minReadySeconds", 5)), hpa="web"))


def test_immutable_은_풀_수_없다(repo):
    (repo / "apps/plain/config.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web-config\nimmutable: true\ndata:\n  a: b\n",
        encoding="utf-8")
    config = Target(api_version="v1", kind="ConfigMap", name="web-config", namespace="shop")
    refused(plan_change(repo, PLAIN, config, ops(("remove", "immutable"))), "guarded_field")
    refused(plan_change(repo, PLAIN, config, ops(("set", "immutable", False))), "guarded_field")
    refused(plan_change(repo, PLAIN, config, ops(("set", "data.a", "c"))), "guarded_field")
    ready(plan_change(repo, PLAIN, config, ops(("set", "metadata.labels", {"team": "shop"}))))


def test_셀렉터의_matchExpressions_도_본다(repo):
    text = (repo / FILE).read_text(encoding="utf-8")
    (repo / FILE).write_text(text.replace(
        "  selector:\n    matchLabels: {app: web}\n",
        "  selector:\n    matchExpressions: [{key: app, operator: In, values: [web]}]\n"), encoding="utf-8")
    refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec.template.metadata.labels", {"app": "api"}))),
            "guarded_field")
    refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec.template.metadata.labels", {"tier": "front"}))),
            "guarded_field")
    ready(plan_change(repo, PLAIN, WEB, ops(("set", "spec.template.metadata.labels", {"app": "web", "tier": "front"}))))


def test_셀렉터_고르기():
    labels = {"app": "web", "tier": "front"}
    assert patching.selects({"matchLabels": {"app": "web"}}, labels)
    assert not patching.selects({"matchLabels": {"app": "api"}}, labels)
    assert patching.selects({"matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["back"]},
                                                  {"key": "app", "operator": "Exists"},
                                                  {"key": "gone", "operator": "DoesNotExist"}]}, labels)
    assert not patching.selects({"matchExpressions": [{"key": "app", "operator": "DoesNotExist"}]}, labels)
    assert not patching.selects({"matchExpressions": [{"key": "app", "operator": "Weird"}]}, labels)


def test_YAML_1_1_에서_참거짓으로_읽히는_문자열은_따옴표로_쓴다(repo):
    """ruamel(1.2)은 yes·on·off 를 그냥 쓰지만 렌더(PyYAML)·ArgoCD(go-yaml)는 1.1 이라 참거짓으로 읽는다."""
    result = ready(plan_change(repo, PLAIN, WEB, ops(
        ("set", "metadata.annotations", {"sidecar": "yes", "mode": "off", "plain": "hello"}))))
    assert doc_after(result)["metadata"]["annotations"] == {"sidecar": "yes", "mode": "off", "plain": "hello"}
    assert 'sidecar: "yes"' in result.files[FILE] and "plain: hello" in result.files[FILE]


def test_Secret_은_경로_지정으로도_못_고친다(repo):
    secret = Target(api_version="v1", kind="Secret", name="web-db", namespace="shop")
    refused(plan_change(repo, PLAIN, secret, ops(("set", "data.url", "eA=="))), "secret")


def test_이미_그_값이면_바꿀_게_없다(repo):
    refused(plan_change(repo, PLAIN, WEB, ops(("set", "spec.replicas", 1))), "no_change")


# ── 렌더 비교 ─────────────────────────────────────────────


def test_요청하지_않은_칸이_같이_바뀌면_렌더_비교가_거절한다(repo):
    """AI 나 다른 곳이 고친 파일을 넘겨도(verify_change) 기대값 = 지금 렌더 + 같은 패치와 칸 하나까지 대조한다."""
    change = ops(("set", f"{CONTAINER}.args", ["-g", "daemon off;"]))
    text = (repo / FILE).read_text(encoding="utf-8")
    edited = text.replace("  replicas: 1", "  replicas: 2").replace(
        "          image: public.ecr.aws/docker/library/nginx:1.28-alpine",
        "          image: public.ecr.aws/docker/library/nginx:1.28-alpine\n          args: [\"-g\", \"daemon off;\"]")
    result = refused(verify_change(repo, PLAIN, WEB, change, {FILE: edited}), "render_mismatch")
    assert "spec.replicas" in result.message


def test_Kustomize_앱은_아직_경로_지정_변경을_못_한다(repo):
    result = refused(plan_change(repo, PROD, API, ops(("set", "spec.template.spec.hostNetwork", True))),
                     "unsupported_change")
    assert not result.retry and "plain YAML" in result.message     # kubectl 길로 간다


# ── 위험 경고 — 막지 않고 카드에 ──────────────────────────


def test_위험해_보이는_값은_막지_않고_경고로_올린다(repo):
    result = ready(plan_change(repo, PLAIN, WEB, ops(
        ("set", f"{CONTAINER}.securityContext", {"privileged": True, "allowPrivilegeEscalation": True,
                                                 "capabilities": {"add": ["NET_ADMIN"]}, "runAsUser": 0}),
        ("set", "spec.template.spec.hostNetwork", True),
        ("set", "spec.template.spec.serviceAccountName", "deployer"),
        ("set", "spec.template.spec.automountServiceAccountToken", True),
    )))
    codes = sorted(risk.code for risk in result.risk_warnings)
    assert codes == sorted(["privileged", "privilege_escalation", "capabilities", "run_as_root", "host_network",
                            "service_account", "service_account_token"])
    assert all(risk.location.file == FILE for risk in result.risk_warnings)
    assert all(risk.text in result.warnings for risk in result.risk_warnings)   # 옛 앱도 경고를 본다


def test_원래_있던_위험은_다시_경고하지_않는다(repo):
    text = (repo / FILE).read_text(encoding="utf-8")
    (repo / FILE).write_text(text.replace("    spec:\n      containers:", "    spec:\n      hostNetwork: true\n"
                                                                            "      containers:"), encoding="utf-8")
    result = ready(plan_change(repo, PLAIN, WEB, ops(("set", 'metadata.labels["tier"]', "front"))))
    assert result.risk_warnings == []


# ── 경로 읽기 ─────────────────────────────────────────────


def test_경로_모양():
    assert patching.field_path(patching.parse('spec.template.spec.containers[api].env')) == (
        "spec", "template", "spec", "containers", "[api]", "env")
    assert patching.field_path(patching.parse('metadata.annotations["a.b/c"]')) == ("metadata", "annotations", "a.b/c")
    for bad in ("", ".spec", "spec.", "spec[0]", "spec[ 1 ]", "spec x", "spec.[a]b"):
        with pytest.raises(patching.PathError):
            patching.parse(bad)
    # 메시지에 보이는 경로(compare.show)는 그대로 다시 쓸 수 있다
    for path in ('metadata.annotations["a.b/c"]', "spec.template.spec.containers[api].env"):
        assert compare.show(patching.field_path(patching.parse(path))) == path


def test_패치가_건드리는_칸():
    change = ops(("set", "spec", {}), ("remove", "metadata.labels.tier"))
    assert patching.touches(change, ("spec", "replicas")) and patching.touches(change, ("metadata",))
    assert not patching.touches(ops(("set", "spec.minReadySeconds", 1)), ("spec", "replicas"))
