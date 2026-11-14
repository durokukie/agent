"""ArgoCD 프로젝트의 리소스 종류 허락 — DURO-254.

규칙(argocd.kind_permitted)은 ArgoCD IsGroupKindPermitted 와 같다. 픽스처는 kukie-infra argocd/platform 의 AppProject
`kukie-apps` 모양이다 — clusterResourceWhitelist: [] 에 namespace 범위 7종만 허락한다.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kukie_agent import argocd
from kukie_agent.manifests import (
    AppSource, Delete, MoveNamespace, NewResource, Ready, Refused, Replicas, Target, plan_change, plan_new_resource,
)
from kukie_agent.manifests.project import kind_denied

FIXTURES = Path(__file__).parents[1] / "fixtures"
IN_CLUSTER = "https://kubernetes.default.svc"
KUKIE_APPS = {
    "clusterResourceWhitelist": [],
    "namespaceResourceWhitelist": [
        {"group": "apps", "kind": "Deployment"}, {"group": "apps", "kind": "StatefulSet"},
        {"group": "", "kind": "Service"}, {"group": "", "kind": "ConfigMap"},
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute"},
        {"group": "external-secrets.io", "kind": "ExternalSecret"},
        {"group": "external-secrets.io", "kind": "SecretStore"},
    ],
}
PLAIN = AppSource(source_type="Directory", path="apps/plain", project="kukie-apps", destination_server=IN_CLUSTER,
                  destination_namespace="shop", auto_sync=True, prune=True, project_kinds=KUKIE_APPS)
WEB = Target(api_version="apps/v1", kind="Deployment", name="web", namespace="shop")
NEW = Target(api_version="", kind="CronJob", name="backup", namespace="shop")
CRONJOB = """\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: backup
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: backup
              image: busybox:1.36
"""
CONFIGMAP = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: backup\ndata:\n  a: b\n"
NAMESPACE = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: backup\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURES / "manifests", copy)
    return copy


def ready(result: Ready | Refused) -> Ready:
    assert isinstance(result, Ready), result
    return result


def refused(result: Ready | Refused) -> Refused:
    assert isinstance(result, Refused), result
    assert result.code == "project_denied"
    return result


def permitted(rules, group, kind, *, namespaced=True, name=""):
    return argocd.kind_permitted(rules, group=group, kind=kind, namespaced=namespaced, name=name)


# ── 규칙 ────────────────────────────────────────────────────


def test_kukie_apps_모양이면_허락한_namespace_종류만_되고_클러스터_범위는_전부_안된다():
    assert permitted(KUKIE_APPS, "apps", "Deployment")
    assert not permitted(KUKIE_APPS, "batch", "CronJob")
    assert not permitted(KUKIE_APPS, "", "Deployment")                 # group 도 맞아야 한다
    assert not permitted(KUKIE_APPS, "", "Namespace", namespaced=False)
    assert not permitted(KUKIE_APPS, "rbac.authorization.k8s.io", "ClusterRole", namespaced=False)


def test_whitelist가_없으면_namespace_범위는_전부_허락_클러스터_범위는_전부_거부():
    assert permitted({}, "batch", "CronJob")
    assert not permitted({}, "", "Namespace", namespaced=False)
    # 빈 목록은 없는 것과 다르다 — namespace 범위도 전부 거부다
    assert not permitted({"namespaceResourceWhitelist": []}, "batch", "CronJob")


def test_와일드카드는_group과_kind_둘_다_glob이다():
    rules = {"clusterResourceWhitelist": [{"group": "*", "kind": "*"}],
             "namespaceResourceWhitelist": [{"group": "apps", "kind": "*"}, {"group": "*", "kind": "Cron*"}]}
    assert permitted(rules, "", "Namespace", namespaced=False)
    assert permitted(rules, "apps", "DaemonSet") and permitted(rules, "batch", "CronJob")
    assert not permitted(rules, "batch", "Job")


def test_blacklist가_whitelist보다_우선한다():
    rules = {"clusterResourceWhitelist": [{"group": "*", "kind": "*"}],
             "clusterResourceBlacklist": [{"group": "", "kind": "Namespace"}],
             "namespaceResourceBlacklist": [{"group": "", "kind": "ResourceQuota"}, {"group": "*", "kind": "Lim*"}]}
    assert not permitted(rules, "", "Namespace", namespaced=False)
    assert permitted(rules, "rbac.authorization.k8s.io", "ClusterRole", namespaced=False)
    assert not permitted(rules, "", "ResourceQuota") and not permitted(rules, "", "LimitRange")
    assert permitted(rules, "apps", "Deployment")


def test_클러스터_범위_항목의_name이_있으면_이름도_맞춰_본다():
    rules = {"clusterResourceWhitelist": [{"group": "", "kind": "Namespace", "name": "team-*"}]}
    assert permitted(rules, "", "Namespace", namespaced=False, name="team-a")
    assert not permitted(rules, "", "Namespace", namespaced=False, name="kube-system")


def test_범위를_모르는_커스텀_리소스는_두_범위_모두에서_막혀야_막는다():
    only_cluster = {"clusterResourceWhitelist": [{"group": "kyverno.io", "kind": "*"}],
                    "namespaceResourceWhitelist": [{"group": "apps", "kind": "Deployment"}]}
    assert not kind_denied(only_cluster, api_version="kyverno.io/v1", kind="ClusterPolicy", namespace="", name="p")
    assert kind_denied(KUKIE_APPS, api_version="kyverno.io/v1", kind="ClusterPolicy", namespace="", name="p")
    # namespace 가 적혀 있으면 namespace 범위로 본다
    assert kind_denied(only_cluster, api_version="kyverno.io/v1", kind="Policy", namespace="shop", name="p")


def test_허락_종류_문구():
    assert argocd.allowed_kinds(KUKIE_APPS, namespaced=False) == "없음"
    assert argocd.allowed_kinds(KUKIE_APPS, namespaced=True).startswith("apps/Deployment, apps/StatefulSet, Service")
    assert argocd.allowed_kinds({"namespaceResourceBlacklist": [{"group": "", "kind": "ResourceQuota"}]},
                                namespaced=True) == "전부(제외: ResourceQuota)"


# ── 새 리소스 ───────────────────────────────────────────────


def plan_new(repo: Path, manifest: str, *, app: AppSource = PLAIN, target: Target = NEW) -> Ready | Refused:
    return plan_new_resource(repo, app, target, NewResource(application="shop-web", manifest_yaml=manifest))


def test_새_CronJob은_PR을_열지_않고_프로젝트와_허락_종류를_알린다(repo):
    result = refused(plan_new(repo, CRONJOB))
    assert result.retry
    assert "프로젝트 'kukie-apps' 가 CronJob 를 허락하지 않아 sync 가 거부돼요" in result.message
    assert "CronJob backup" in result.message and "apps/Deployment" in result.message
    assert "클러스터 범위 없음" in result.message


def test_새_Namespace는_clusterResourceWhitelist가_비어_있어_막는다(repo):
    result = refused(plan_new(repo, NAMESPACE, target=NEW.model_copy(update={"kind": "Namespace", "namespace": None})))
    assert "Namespace 를 허락하지 않아" in result.message


def test_허락된_종류는_PR로_가고_여럿_중_하나만_막혀도_막는다(repo):
    assert ready(plan_new(repo, CONFIGMAP, target=NEW.model_copy(update={"kind": "ConfigMap"}))).new_resources
    result = refused(plan_new(repo, f"{CONFIGMAP}---\n{CRONJOB}", target=NEW.model_copy(update={"kind": "ConfigMap"})))
    assert "CronJob backup" in result.message and "ConfigMap backup" not in result.message


def test_프로젝트를_못_읽었으면_막지_않는다(repo):
    app = PLAIN.model_copy(update={"project_kinds": None})
    ready(plan_new(repo, CRONJOB, app=app))
    result = ready(plan_new(repo, NAMESPACE, app=app, target=NEW.model_copy(update={"kind": "Namespace"})))
    assert "cluster_scoped" in [risk.code for risk in result.risk_warnings]


# ── 고치기 · 옮기기 · 지우기 ─────────────────────────────────


def test_막힌_종류를_고치는_PR은_열지_않는다(repo):
    app = PLAIN.model_copy(update={"project_kinds": {"namespaceResourceWhitelist": [{"group": "", "kind": "Service"}]}})
    result = refused(plan_change(repo, app, WEB, Replicas(count=3)))
    assert not result.retry and "Deployment 를 허락하지 않아" in result.message
    ready(plan_change(repo, PLAIN, WEB, Replicas(count=3)))                        # kukie-apps 는 Deployment 를 허락한다


def test_막힌_종류는_옮겨도_다시_거부돼_막는다(repo):
    app = PLAIN.model_copy(update={"project_kinds": {"namespaceResourceBlacklist": [{"group": "apps", "kind": "*"}]}})
    result = refused(plan_change(repo, app, WEB, MoveNamespace(to="other")))
    assert "Deployment 를 허락하지 않아" in result.message and "Deployment other/web" in result.message


def test_막힌_종류를_지우는_PR은_열되_클러스터엔_남는다고_알린다(repo):
    app = PLAIN.model_copy(update={"project_kinds": {"namespaceResourceWhitelist": [{"group": "", "kind": "Service"}]}})
    result = ready(plan_change(repo, app, WEB, Delete()))
    codes = [risk.code for risk in result.risk_warnings]
    assert codes[0] == "project_kind_denied" and "prune_on" not in codes
    assert "클러스터엔 남아요" in result.risk_warnings[0].text and "kubectl" in result.risk_warnings[0].text
    # git 에만 있는(sync 가 거부된) 것을 지우는 건 바로 그 거부를 푸는 변경이다
    missing = ready(plan_change(repo, app, WEB.model_copy(update={"in_cluster": False}), Delete()))
    assert [risk.code for risk in missing.risk_warnings][0] == "git_only"
    # 허락된 종류면 지금처럼 prune 경고
    assert ready(plan_change(repo, PLAIN, WEB, Delete())).risk_warnings[0].code == "prune_on"
