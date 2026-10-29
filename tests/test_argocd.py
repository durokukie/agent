"""ArgoCD 관리 리소스 판별 — kukie_agent/argocd.py (DURO-129 가드레일 경고, DURO-140 앱 목록 API).

판별은 순수 함수와 가짜 kubectl(argocd.run_kubectl)로 본다. 다른 테스트는 conftest 의 _no_argocd_by_default 로
"ArgoCD 없는 클러스터" 에서 돈다 — 여기서만 ArgoCD 가 있는 클러스터를 바꿔 끼운다.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ApprovalRequired
from pydantic_ai.messages import ToolCallPart

from helpers import USER
from kukie_agent import argocd, server
from kukie_agent.guardrail import hook
from kukie_agent.guardrail.action_plan import ActionPlan
from kukie_agent.guardrail.approval import build_approval_request
from kukie_agent.kubectl import KubectlResult
from kukie_agent.tools import read as read_tools

SCALE_ARGS = {
    "kind": "deployment", "name": "guestbook-ui", "replicas": 3, "namespace": "shop",
    "intent": "레플리카를 늘린다.", "expected_effects": ["3개가 된다."], "side_effects": ["자원을 더 쓴다."],
}
FORBIDDEN = ('Error from server (Forbidden): applications.argoproj.io is forbidden: User "kukie" cannot list '
             'resource "applications" in API group "argoproj.io" at the cluster scope')
ABSENT = 'error: the server doesn\'t have a resource type "applications"'


def _deployment(*, annotation: str | None = None, label: str | None = None, name: str = "guestbook-ui") -> dict:
    meta: dict = {"name": name, "namespace": "shop"}
    if annotation is not None:
        meta["annotations"] = {argocd.TRACKING_ANNOTATION: annotation}
    if label is not None:
        meta["labels"] = {argocd.INSTANCE_LABEL: label}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta}


def _application(name: str = "guestbook", *, namespace: str = "argocd", automated: dict | None = None,
                 resources: list[dict] | None = None, source: dict | None = None, status: dict | None = None) -> dict:
    spec: dict = {
        "project": "default",
        "source": source or {"repoURL": "https://github.com/acme/manifests", "path": "guestbook",
                             "targetRevision": "main"},
        "destination": {"server": "https://kubernetes.default.svc", "namespace": "shop"},
    }
    if automated is not None:
        spec["syncPolicy"] = {"automated": automated}
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
        "status": status if status is not None else {
            "sourceType": "Directory",
            "sync": {"status": "Synced"}, "health": {"status": "Healthy"},
            "resources": resources if resources is not None else [
                {"group": "apps", "kind": "Deployment", "namespace": "shop", "name": "guestbook-ui"}],
        },
    }


def _ok(body) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout=json.dumps(body), stderr="", success=True, exit_code=0)


def _fail(stderr: str) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout="", stderr=stderr, success=False, exit_code=1)


TRACKED = "guestbook:apps/Deployment:shop/guestbook-ui"


@pytest.fixture
def cluster(monkeypatch):
    """ArgoCD 판별의 가짜 kubectl. resource 는 대상 리소스 답, apps 는 Application 목록 답. 부른 args 를 calls 에 남긴다."""
    state = SimpleNamespace(resource=_ok(_deployment(annotation=TRACKED)),
                            apps=_ok({"items": [_application(automated={"selfHeal": True, "prune": True})]}),
                            calls=[])

    def run(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        state.calls.append(args)
        return state.apps if args == argocd.LIST_COMMAND else state.resource

    monkeypatch.setattr(argocd, "run_kubectl", run)
    return state


def _inspect():
    return argocd.inspect([{"kind": "deployment", "name": "guestbook-ui", "namespace": "shop"}], context="eks-shop")


# ── 추적 표시 ──────────────────────────────────────────────

def test_어노테이션_추적_표시를_먼저_본다():
    resource = _deployment(annotation=TRACKED, label="helm-release")
    assert argocd.tracking_of(resource) == argocd.Tracking("guestbook", None, "annotation")


def test_라벨_추적_표시():
    assert argocd.tracking_of(_deployment(label="guestbook")) == argocd.Tracking("guestbook", None, "label")


def test_앱_이름이_namespace_밑줄_이름이면_그_namespace_의_Application이다():
    tracking = argocd.tracking_of(_deployment(annotation="team-a_guestbook:apps/Deployment:shop/guestbook-ui"))
    assert tracking == argocd.Tracking("guestbook", "team-a", "annotation")


def test_다른_리소스를_가리키는_tracking_id는_보지_않는다():
    """다른 리소스를 복사해 만들면 어노테이션도 따라온다 — ArgoCD 도 제 것으로 안 본다."""
    assert argocd.tracking_of(_deployment(annotation="guestbook:apps/Deployment:shop/other")) is None
    assert argocd.tracking_of(_deployment(annotation="guestbook:/Service:shop/guestbook-ui")) is None
    # 같은 이름으로 다른 namespace 에 복사한 것 (agent#123 리뷰)
    assert argocd.tracking_of(_deployment(annotation="guestbook:apps/Deployment:prod/guestbook-ui")) is None


def test_추적_표시가_없으면_None():
    assert argocd.tracking_of(_deployment()) is None
    assert argocd.tracking_of({"kind": "Deployment"}) is None


# ── Application 한 줄 (앱 목록 API) ─────────────────────────

def test_Application_한_줄은_앱_계약_모양이다():
    row = argocd.application_row(_application(automated={"selfHeal": True}))
    assert row == {
        "name": "guestbook", "namespace": "argocd", "project": "default",
        "repo_url": "https://github.com/acme/manifests", "path": "guestbook", "chart": None, "target_revision": "main",
        "source_type": "Directory", "sync_status": "Synced", "health_status": "Healthy",
        "auto_sync": True, "self_heal": True, "destination_namespace": "shop",
    }


def test_Helm_차트_저장소와_여러_소스와_아직_안_맞춘_앱():
    helm = _application(source={"repoURL": "https://charts.example.com", "chart": "redis", "targetRevision": "18.1.0"})
    assert (argocd.application_row(helm)["chart"], argocd.application_row(helm)["path"]) == ("redis", None)

    multi = _application()
    multi["spec"].pop("source")
    multi["spec"]["sources"] = [{"repoURL": "https://github.com/acme/a", "path": "kustomize/base"},
                                {"repoURL": "https://github.com/acme/b", "ref": "values"}]
    assert argocd.application_row(multi)["repo_url"] == "https://github.com/acme/a"

    fresh = argocd.application_row(_application(status={}))
    assert (fresh["source_type"], fresh["sync_status"], fresh["health_status"]) == (None, "Unknown", "Unknown")
    assert (fresh["auto_sync"], fresh["self_heal"]) == (False, False)


def test_automated_enabled_false는_자동_sync가_꺼진_것이다():
    assert argocd.sync_policy(_application(automated={"enabled": False, "selfHeal": True})) == (False, False, False)
    assert argocd.sync_policy(_application(automated={})) == (True, False, False)


@pytest.mark.parametrize(("stderr", "kind"), [
    (ABSENT, "absent"),
    ('error: resource mapping not found for name: "x": no matches for kind "Application" in version "argoproj.io/v1alpha1"',
     "absent"),
    (FORBIDDEN, "forbidden"),
    ("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout", "error"),
])
def test_kubectl_실패_가르기(stderr, kind):
    assert argocd.failure_kind(stderr) == kind


# ── 판별 (inspect) ────────────────────────────────────────

def test_selfHeal_켜짐(cluster):
    found = _inspect()
    assert found == {
        "summary": "ArgoCD 관리 리소스 직접 변경",
        "level": "self_heal",
        "warning": "몇 분 안에 ArgoCD가 되돌려요. 유지하려면 sync를 멈추거나 PR을 같이 올리세요.",
        "applications": [{
            "name": "guestbook", "namespace": "argocd", "repo_url": "https://github.com/acme/manifests",
            "path": "guestbook", "chart": None, "target_revision": "main", "source_type": "Directory",
            "sync_status": "Synced", "auto_sync": True, "self_heal": True, "prune": True,
        }],
        "resources": [{"kind": "deployment", "name": "guestbook-ui", "namespace": "shop", "application": "guestbook"}],
    }
    assert cluster.calls == [["get", "deployment", "guestbook-ui", "-o", "json", "-n", "shop"], argocd.LIST_COMMAND]


def test_자동_sync만(cluster):
    cluster.apps = _ok({"items": [_application(automated={"prune": False})]})
    found = _inspect()
    assert (found["level"], found["warning"]) == ("auto_sync", "다음 git 변경이 반영될 때 덮어써져요.")


def test_수동_sync(cluster):
    cluster.apps = _ok({"items": [_application()]})
    assert _inspect()["level"] == "manual"


def test_관리_아님_추적_표시가_없으면_Application을_읽지_않는다(cluster):
    cluster.resource = _ok(_deployment())
    assert _inspect() is None
    assert argocd.LIST_COMMAND not in cluster.calls


def test_Helm_instance_라벨일_뿐이면_관리_아님(cluster):
    """라벨은 Helm 도 붙인다 — Application 의 status.resources 에 있어야 관리로 본다."""
    cluster.resource = _ok(_deployment(label="guestbook"))
    cluster.apps = _ok({"items": [_application(automated={"selfHeal": True}, resources=[])]})
    assert _inspect() is None
    cluster.apps = _ok({"items": []})
    assert _inspect() is None
    cluster.apps = _ok({"items": [_application(automated={"selfHeal": True})]})
    assert _inspect()["level"] == "self_heal"


def test_권한_없음은_조용히_건너뛴다(cluster):
    cluster.apps = _fail(FORBIDDEN)
    assert _inspect() is None


def test_ArgoCD_없음_라벨이면_관리_아님_어노테이션이면_정책을_모른다고_경고(cluster):
    cluster.apps = _fail(ABSENT)
    cluster.resource = _ok(_deployment(label="guestbook"))
    assert _inspect() is None
    # 어노테이션은 ArgoCD 만 붙인다 — 허브형 ArgoCD(다른 클러스터의 Application)일 수 있다
    cluster.resource = _ok(_deployment(annotation=TRACKED))
    found = _inspect()
    assert (found["level"], found["applications"]) == ("unknown", [])
    assert found["resources"][0]["application"] == "guestbook"


def test_리소스가_없거나_못_읽으면_관리_아님(cluster):
    cluster.resource = _fail('Error from server (NotFound): deployments.apps "guestbook-ui" not found')
    assert _inspect() is None
    cluster.resource = KubectlResult(command="kubectl get", stdout="not json", stderr="", success=True)
    assert _inspect() is None


def test_리소스가_여럿이면_가장_센_경고(cluster, monkeypatch):
    manual = _application("api", resources=[{"group": "apps", "kind": "Deployment", "namespace": "shop", "name": "api"}])
    healing = _application(automated={"selfHeal": True})
    cluster.apps = _ok({"items": [manual, healing]})
    resources = {"api": _ok(_deployment(name="api", annotation="api:apps/Deployment:shop/api")),
                 "guestbook-ui": _ok(_deployment(annotation=TRACKED))}

    def run(args, **kwargs):
        cluster.calls.append(args)
        return cluster.apps if args == argocd.LIST_COMMAND else resources[args[2]]

    monkeypatch.setattr(argocd, "run_kubectl", run)
    found = argocd.inspect([{"kind": "Deployment", "name": "api", "namespace": "shop"},
                            {"kind": "Deployment", "name": "guestbook-ui", "namespace": "shop"}], context="eks-shop")
    assert found["level"] == "self_heal"
    assert [app["name"] for app in found["applications"]] == ["api", "guestbook"]
    assert cluster.calls.count(argocd.LIST_COMMAND) == 1                  # Application 목록은 한 번만


def test_한_번의_제한도_남은_시간으로_줄인다(cluster, monkeypatch):
    """끝 무렵에 시작한 조회가 전체 시간을 넘기지 않게 (agent#123 리뷰)."""
    timeouts = []

    def run(args, *, timeout, **kwargs):
        timeouts.append(timeout)
        return cluster.apps if args == argocd.LIST_COMMAND else cluster.resource

    monkeypatch.setattr(argocd, "run_kubectl", run)
    ticks = iter([0, 0, 8])            # 시작, 리소스 조회(5초 그대로), Application 목록(남은 2초)
    argocd.inspect([{"kind": "deployment", "name": "guestbook-ui", "namespace": "shop"}],
                   context="eks-shop", clock=lambda: next(ticks))
    assert timeouts == [argocd.LOOKUP_TIMEOUT, argocd.LOOKUP_BUDGET - 8]


def test_시간을_다_쓰면_더_묻지_않는다(cluster):
    ticks = iter([0, 0, 100])          # 시작, 리소스 조회, Application 목록 직전
    found = argocd.inspect([{"kind": "deployment", "name": "guestbook-ui", "namespace": "shop"}],
                           context="eks-shop", clock=lambda: next(ticks))
    assert argocd.LIST_COMMAND not in cluster.calls
    assert found["level"] == "unknown"                                    # 어노테이션은 봤다 — 정책만 모른다


# ── 가드레일 (DURO-129) ────────────────────────────────────

def _ctx(run_id=None, approved=False):
    return SimpleNamespace(
        deps=SimpleNamespace(context="eks-shop", namespace="shop", skill=SimpleNamespace(name="Shared"),
                             run_id=run_id, user_id="u-1", kubeconfig=None),
        tool_call_approved=approved,
    )


@pytest.fixture
def dry_run_ok(monkeypatch, plan_dir):
    monkeypatch.setattr(hook, "run_kubectl", lambda *a, **k: KubectlResult(
        command="kubectl dry-run", stdout="ok\n", stderr="", success=True, exit_code=0))
    monkeypatch.setattr(hook, "generate_decision_guidance", AsyncMock(return_value="롤백 기준을 확인한다."))


async def _card(run_id=None):
    call = ToolCallPart(tool_name="scale_resource", args=SCALE_ARGS, tool_call_id="call-argo")
    with pytest.raises(ApprovalRequired) as raised:
        await hook.guardrail(_ctx(run_id), call=call, tool_def=None, args=SCALE_ARGS, handler=AsyncMock())
    return await build_approval_request(call, raised.value.metadata, default_namespace="shop", run_id=run_id)


@pytest.mark.asyncio
async def test_ArgoCD_관리_리소스면_승인_카드와_Plan에_경고를_싣고_실행은_허용한다(cluster, dry_run_ok):
    card = await _card()
    assert card.argocd["level"] == "self_heal"
    assert card.model_dump()["argocd"]["warning"].startswith("몇 분 안에 ArgoCD가 되돌려요")
    plan = await ActionPlan.find_by_call_id("call-argo")
    assert plan.argocd["summary"] == "ArgoCD 관리 리소스 직접 변경"
    assert "ArgoCD 관리 리소스 직접 변경" in plan.render_markdown()           # 판단 가이드도 본다

    handler = AsyncMock(return_value=KubectlResult(command="kubectl scale", stdout="scaled\n", stderr="",
                                                   success=True, exit_code=0))
    calls_before = len(cluster.calls)
    result = await hook.guardrail(_ctx(approved=True), call=ToolCallPart(tool_name="scale_resource", args=SCALE_ARGS,
                                                                         tool_call_id="call-argo"),
                                  tool_def=None, args=SCALE_ARGS, handler=handler)
    assert result.stdout == "scaled\n"                                    # 경고 후 허용
    assert len(cluster.calls) == calls_before                             # 승인 뒤에는 다시 판별하지 않는다


@pytest.mark.asyncio
async def test_server_계획의_payload에_argocd_칸이_실린다(cluster, dry_run_ok, fake_server):
    room = fake_server.add_conversation(shared=True)
    run_id = fake_server.put_run(room, request_id="r-1")["id"]
    card = await _card(run_id)
    assert fake_server.plan_by_call("call-argo")["payload"]["argocd"]["level"] == "self_heal"
    assert card.argocd["applications"][0]["repo_url"] == "https://github.com/acme/manifests"


@pytest.mark.asyncio
async def test_ArgoCD_없는_클러스터는_지금과_같다(dry_run_ok, fake_server):
    """conftest 의 기본값 — 판별 kubectl 이 실패한다."""
    room = fake_server.add_conversation(shared=True)
    run_id = fake_server.put_run(room, request_id="r-1")["id"]
    card = await _card(run_id)
    assert "argocd" not in fake_server.plan_by_call("call-argo")["payload"]
    assert "argocd" not in card.model_dump()


@pytest.mark.asyncio
async def test_판별이_터져도_승인_카드는_뜬다(dry_run_ok, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(argocd, "inspect", boom)
    card = await _card()
    assert card.argocd is None


# ── 읽기 툴 ───────────────────────────────────────────────

def _read_ctx():
    return SimpleNamespace(deps=SimpleNamespace(context="eks-shop", namespace="shop", kubeconfig=None))


def test_describe_결과에_관리하는_ArgoCD_앱을_같이_적는다(cluster, monkeypatch):
    monkeypatch.setattr(read_tools, "run_kubectl", lambda *a, **k: KubectlResult(
        command="kubectl describe", stdout="Name: guestbook-ui\n", stderr="", success=True, exit_code=0))
    result = read_tools.describe_resource(_read_ctx(), "deployment", "guestbook-ui")
    assert result.stdout == "Name: guestbook-ui\n"                        # kubectl 출력은 그대로
    assert result.note.startswith("ArgoCD 앱 guestbook (소스 https://github.com/acme/manifests guestbook @ main, "
                                  "self-heal 켜짐) 가 관리하는 리소스다")
    assert "몇 분 안에 ArgoCD가 되돌려요" in result.note


def test_describe_관리_아니면_note_칸이_없다(monkeypatch):
    monkeypatch.setattr(read_tools, "run_kubectl", lambda *a, **k: KubectlResult(
        command="kubectl describe", stdout="Name: x\n", stderr="", success=True, exit_code=0))
    result = read_tools.describe_resource(_read_ctx(), "deployment", "x")
    assert result.note is None and "note" not in result.model_dump()


# ── 앱 목록 API (DURO-140) ─────────────────────────────────

@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)


def _api(monkeypatch, answer: KubectlResult) -> list:
    ran: list = []

    def run(args, **kwargs):
        ran.append(args)
        return answer

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", run)
    return ran


def test_앱_목록은_namespace_이름순이고_계약_모양이다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(owner="u-1")
    ran = _api(monkeypatch, _ok({"items": [_application("web", automated={}), _application("api"),
                                            _application("cache", namespace="team-a")]}))
    r = client.get(f"/clusters/{cluster}/argocd/applications", headers=USER)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["at"].endswith("Z")
    assert [(a["namespace"], a["name"]) for a in body["applications"]] == [
        ("argocd", "api"), ("argocd", "web"), ("team-a", "cache")]
    assert body["applications"][1]["auto_sync"] is True
    assert set(body["applications"][0]) == {
        "name", "namespace", "project", "repo_url", "path", "chart", "target_revision", "source_type",
        "sync_status", "health_status", "auto_sync", "self_heal", "destination_namespace"}
    assert ran == [argocd.LIST_COMMAND]


@pytest.mark.parametrize(("stderr", "status"), [(ABSENT, "absent"), (FORBIDDEN, "forbidden")])
def test_ArgoCD_없음과_권한_없음은_200으로_가른다(client, fake_server, monkeypatch, stderr, status):
    cluster = fake_server.add_cluster(owner="u-1")
    _api(monkeypatch, _fail(stderr))
    r = client.get(f"/clusters/{cluster}/argocd/applications", headers=USER)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["applications"]) == (status, [])


@pytest.mark.parametrize(("answer", "code"), [
    (_fail("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"), "CLUSTER_UNREACHABLE"),
    (_fail("error: You must be logged in to the server (Unauthorized)"), "CLUSTER_AUTH_EXPIRED"),
    (KubectlResult(command="kubectl get", stdout="not json", stderr="", success=True), "CLUSTER_BAD_RESPONSE"),
])
def test_그_밖의_실패는_개요와_같이_502(client, fake_server, monkeypatch, answer, code):
    cluster = fake_server.add_cluster(owner="u-1")
    _api(monkeypatch, answer)
    r = client.get(f"/clusters/{cluster}/argocd/applications", headers=USER)
    assert r.status_code == 502 and r.json()["detail"]["code"] == code


def test_볼_수_없는_클러스터는_404이고_kubectl은_돌지_않는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(owner="u-2")
    ran = _api(monkeypatch, _ok({"items": []}))
    r = client.get(f"/clusters/{cluster}/argocd/applications", headers=USER)
    assert r.status_code == 404 and ran == []
