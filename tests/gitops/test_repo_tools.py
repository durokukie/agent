"""매니페스트 레포 읽기 툴 3종 — 채팅에서 연결된 레포의 폴더·파일·앱 렌더 결과를 본다 (DURO-151).

읽기는 레포 탭 API 와 같은 gitops/browse.py 를 탄다 — 가림·상한·렌더 안전장치 자체는 test_gitops_browse.py 가 본다.
여기서는 툴이 대상을 대화의 클러스터에 연결된 레포로 좁히는지, 결과를 모델에 넣을 만큼 자르는지, 실패를 까닭으로 돌려주는지 본다.
GitHub 는 tests/fake_github.py, 클러스터의 Application 목록은 tools/repo.run_kubectl 을 바꿔 끼운다.
"""
from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fake_github import REPO, FakeGitHub
from helpers import USER, chat_model, new_room, sync
from kukie_agent import server
from kukie_agent.agent import agent
from kukie_agent.deps import Deps
from kukie_agent.gitops import browse, github
from kukie_agent.kubectl import KubectlResult
from kukie_agent.manifests import render as renderer
from kukie_agent.manifests.render import kustomize_bin
from kukie_agent.skills import PRIVATE
from kukie_agent.tools import repo as repo_tools
from kukie_agent.tools.repo import NOT_CONNECTED, list_repo_dir, read_repo_file, render_app

needs_kustomize = pytest.mark.skipif(
    shutil.which(kustomize_bin()) is None and not os.environ.get("CI"),
    reason="kustomize 가 없다 (CI 에서는 깔고 돈다)")

SECRET = """apiVersion: v1
kind: Secret
metadata:
  name: web-db
  namespace: shop
type: Opaque
stringData:
  password: hunter2
"""


def _app(name: str, path: str | None, *, source_type: str = "Directory", revision: str = "HEAD",
         repo_url: str = "https://github.com/Acme/Manifests.git", namespace: str = "argocd", **source) -> dict:
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"project": "default", "destination": {"namespace": "shop"},
                 "source": {"repoURL": repo_url, "targetRevision": revision,
                            **({"path": path} if path is not None else {}), **source}},
        "status": {"sourceType": source_type, "sync": {"status": "Synced", "revision": "c0"}},
    }


APPS = [
    _app("shop-web", "apps/plain"),
    _app("shop-prod", "apps/kustomize/overlays/prod", source_type="Kustomize", revision="main"),
    _app("shop-dev", "apps/kustomize/overlays/dev", source_type="Kustomize", revision="dev"),
    _app("web-chart", "apps/helm", source_type="Helm"),
    _app("other-repo", "apps/plain", repo_url="https://github.com/acme/elsewhere"),
]


@pytest.fixture(autouse=True)
def _forget(monkeypatch):
    browse.forget()
    monkeypatch.setattr(renderer, "_loopback_scans", renderer.OrderedDict())
    yield
    browse.forget()


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    fake.commits["c0"].update({
        "apps/secret/db.yaml": SECRET,
        "apps/helm/values.yaml": "replicaCount: 2\nimage:\n  repository: nginx\n  tag: 1.29-alpine\n",
        "apps/helm/Chart.yaml": "apiVersion: v2\nname: web\nversion: 0.1.0\n",
    })
    fake.branches["dev"] = "c0"
    monkeypatch.setattr(github, "connect", fake.connect)
    return fake


@pytest.fixture
def cluster(fake_server):
    cluster_id = fake_server.add_cluster(owner="u-1")
    fake_server.link_repo(cluster_id, REPO)
    return cluster_id


def _ctx(cluster_id: str | None):
    return SimpleNamespace(deps=Deps(context="kind-dev", namespace="study", skill=PRIVATE, cluster_id=cluster_id))


def _cluster_apps(monkeypatch, items=APPS, *, fail: str | None = None) -> list:
    ran: list = []

    def run(args, **kwargs):
        ran.append(args)
        if fail is not None:
            return KubectlResult(command="kubectl get", stdout="", stderr=fail, success=False, exit_code=1)
        return KubectlResult(command="kubectl get", stdout=json.dumps({"items": items}), stderr="", success=True,
                             exit_code=0)

    monkeypatch.setattr(repo_tools, "run_kubectl", run)
    return ran


def _token_calls(fake_server) -> list:
    return [call for call in fake_server.calls if call[0] == "repo_token"]


# ── 폴더 ──────────────────────────────────────────────

def test_폴더는_항목과_그_경로를_쓰는_앱을_준다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    result = sync(list_repo_dir(_ctx(cluster), "apps"))
    assert result.success, result.stderr
    assert result.command == f"repo tree {REPO}:apps@main"
    out = result.stdout
    assert "[폴더] plain/  ← ArgoCD 앱 shop-web" in out and "[폴더] kustomize/  ← ArgoCD 앱" in out
    assert "other-repo" not in out                                # 다른 레포를 쓰는 앱은 붙이지 않는다
    assert f"https://github.com/{REPO}/tree/c0/apps" in out
    assert gh.tokens == ["ghs_fake-token"] and "ghs_fake-token" not in out


def test_앱_목록을_못_읽어도_폴더는_준다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, fail="Error from server (Forbidden): applications.argoproj.io is forbidden")
    result = sync(list_repo_dir(_ctx(cluster), ""))
    assert result.success and "[폴더] apps/" in result.stdout
    assert "ArgoCD 앱을 읽을 권한이 없어요" in result.stdout


def test_폴더가_아닌_경로와_레포_밖_경로는_까닭을_돌려준다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    assert "폴더가 아니에요" in sync(list_repo_dir(_ctx(cluster), "README.md")).stderr
    missing = sync(list_repo_dir(_ctx(cluster), "nope"))
    assert not missing.success and "'nope' 가 없어요" in missing.stderr
    outside = sync(list_repo_dir(_ctx(cluster), "../etc"))
    assert not outside.success and "레포 안 경로가 아니에요" in outside.stderr


# ── 파일 ──────────────────────────────────────────────

def test_파일은_실제_내용과_그_파일을_쓰는_앱을_준다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    result = sync(read_repo_file(_ctx(cluster), "apps/helm/values.yaml"))
    assert result.success, result.stderr
    out = result.stdout
    assert "--- 내용 ---\nreplicaCount: 2\nimage:" in out and "tag: 1.29-alpine" in out
    assert "argocd/web-chart — path apps/helm · Helm · 이 경로가 앱 path" in out


def test_ref_를_안_주면_그_경로를_쓰는_앱의_targetRevision_을_본다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    dev = sync(read_repo_file(_ctx(cluster), "apps/kustomize/overlays/dev/kustomization.yaml"))
    assert dev.success and dev.command.endswith("@dev")
    given = sync(read_repo_file(_ctx(cluster), "apps/kustomize/overlays/dev/kustomization.yaml", ref="main"))
    assert given.success and given.command.endswith("@main")
    # 앱이 쓰지 않는 경로는 기본 브랜치
    assert sync(read_repo_file(_ctx(cluster), "README.md")).command.endswith("@main")


def test_Secret_파일은_내용_대신_가린_까닭만_준다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    result = sync(read_repo_file(_ctx(cluster), "apps/secret/db.yaml"))
    assert result.success and "내용을 가렸어요" in result.stdout and "kind: Secret" in result.stdout
    assert "hunter2" not in result.stdout and "Secret shop/web-db" in result.stdout   # 이름은 비밀이 아니다


@pytest.mark.parametrize("path,text", [
    ("apps/plain/sops.yaml", "apiVersion: v1\nkind: ConfigMap\ndata:\n  password: ENC[AES256_GCM,data:abc]\n"),
    ("apps/plain/key.txt", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\n"),
    ("apps/plain/.env", "PASSWORD=hunter2\n"),
])
def test_sops_비밀_키_파일도_가린다(cluster, gh, monkeypatch, path, text):
    _cluster_apps(monkeypatch)
    gh.commits["c0"][path] = text
    result = sync(read_repo_file(_ctx(cluster), path))
    assert result.success and "내용을 가렸어요" in result.stdout
    assert "hunter2" not in result.stdout and "MIIabc" not in result.stdout and "ENC[" not in result.stdout


def test_큰_파일은_앞부분과_N줄_더_있음만(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/plain/big.txt"] = "".join(f"line {i}\n" for i in range(1000))
    result = sync(read_repo_file(_ctx(cluster), "apps/plain/big.txt"))
    assert result.success and "line 0" in result.stdout and "line 999" not in result.stdout
    _within_limits(result.stdout)
    shown = sum(line.startswith("line ") for line in result.stdout.splitlines())
    assert f"… {1000 - shown}줄 더 있음 — 전체는 GitHub 에서: https://github.com/acme/manifests/blob/c0/apps/plain/big.txt" \
        in result.stdout
    assert result.stdout.endswith("GitHub: https://github.com/acme/manifests/blob/c0/apps/plain/big.txt")


def _within_limits(out: str) -> None:
    """머리 줄·꼬리 줄까지 합친 전체가 상한 안이다 (agent#129 리뷰)."""
    assert len(out.splitlines()) <= repo_tools.MAX_LINES and len(out) <= repo_tools.MAX_CHARS


def test_앱이_많은_루트_폴더도_전체_출력이_상한_안이다(cluster, gh, monkeypatch):
    """루트는 레포의 모든 앱이 "앱 path 가 이 아래" 로 붙는다 — 머리 줄도 상한을 지킨다."""
    many = [_app(f"app-{i:03d}-{'x' * 50}", f"apps/team-{i:03d}") for i in range(500)]
    _cluster_apps(monkeypatch, many)
    for i in range(500):
        gh.commits["c0"][f"apps/team-{i:03d}/cm.yaml"] = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n"
    root = sync(list_repo_dir(_ctx(cluster), ""))
    assert root.success, root.stderr
    _within_limits(root.stdout)
    assert f"  … 외 {500 - repo_tools.MAX_HEAD_ITEMS}개" in root.stdout                    # 머리 줄
    assert f"app-019-{'x' * 50} 외 {500 - repo_tools.MAX_HEAD_ITEMS}개" in root.stdout      # apps/ 항목의 배지
    apps = sync(list_repo_dir(_ctx(cluster), "apps"))                                     # 항목 500개 — 본문이 잘린다
    assert apps.success, apps.stderr
    _within_limits(apps.stdout)
    assert "줄 더 있음 — 전체는 GitHub 에서" in apps.stdout and apps.stdout.endswith("/tree/c0/apps")


def test_레포_값이_아주_길어도_전체_출력이_상한_안이다(cluster, gh, monkeypatch):
    """머리 줄 수는 상한 안이어도 줄마다 아주 길면(긴 앱 path·targetRevision·객체 이름) 머리 줄만으로 넘을 수 있다 (agent#129 리뷰)."""
    long_apps = [_app(f"app-{i}", f"apps/{'p' * 1000}{i}", revision="r" * 1000) for i in range(30)]
    _cluster_apps(monkeypatch, long_apps)
    root = sync(list_repo_dir(_ctx(cluster), ""))
    assert root.success, root.stderr
    _within_limits(root.stdout)
    assert root.stdout.endswith(repo_tools.CUT) and "(줄이 길어 잘림)" in root.stdout
    gh.commits["c0"]["apps/plain/long.yaml"] = "\n---\n".join(
        f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {'n' * 5000}{i}}}" for i in range(20))
    long_file = sync(read_repo_file(_ctx(cluster), "apps/plain/long.yaml"))
    assert long_file.success
    _within_limits(long_file.stdout)
    assert "쿠버네티스 객체 20개: " in long_file.stdout and "외 " in long_file.stdout   # 긴 이름은 줄이고 몇 개 더인지는 남긴다


def test_객체가_많은_파일도_전체_출력이_상한_안이다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/plain/many.yaml"] = "\n---\n".join(
        f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: config-{i}-{'y' * 100}, namespace: shop}}\ndata: {{a: b}}"
        for i in range(2000))
    result = sync(read_repo_file(_ctx(cluster), "apps/plain/many.yaml"))
    assert result.success
    _within_limits(result.stdout)
    line = next(line for line in result.stdout.splitlines() if line.startswith("쿠버네티스 객체 2000개: "))
    shown = line.count("ConfigMap ")
    assert 0 < shown <= repo_tools.MAX_HEAD_ITEMS and line.endswith(f" 외 {2000 - shown}개")


def test_한_줄이_길어도_글자_상한을_넘지_않는다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/plain/wide.json"] = "\n".join("x" * 5000 for _ in range(50))
    result = sync(read_repo_file(_ctx(cluster), "apps/plain/wide.json"))
    assert result.success and "(줄이 길어 잘림)" in result.stdout and "줄 더 있음" in result.stdout
    _within_limits(result.stdout)


def test_상한을_넘는_파일과_바이너리는_내용_없이_까닭만(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    monkeypatch.setattr(browse, "MAX_FILE_BYTES", 10)
    gh.commits["c0"]["apps/plain/bin.dat"] = b"\x00\x01"
    assert "파일이 커서" in sync(read_repo_file(_ctx(cluster), "apps/plain/deployment.yaml")).stdout
    assert "바이너리" in sync(read_repo_file(_ctx(cluster), "apps/plain/bin.dat")).stdout


# ── 대상: 대화의 클러스터에 연결된 레포만 ─────────────────

@pytest.mark.parametrize("call", [
    lambda ctx: list_repo_dir(ctx, ""),
    lambda ctx: read_repo_file(ctx, "apps/helm/values.yaml"),
    lambda ctx: render_app(ctx, "shop-web"),
])
def test_레포가_연결_안_된_클러스터면_연결하라고_돌려주고_GitHub_를_부르지_않는다(fake_server, monkeypatch, call):
    ran = _cluster_apps(monkeypatch)
    lonely = fake_server.add_cluster(owner="u-1")
    for ctx in (_ctx(lonely), _ctx(None)):                  # None: flat 엔드포인트 — 클러스터 등록이 없다
        result = sync(call(ctx))
        assert not result.success and result.stderr == NOT_CONNECTED
    assert _token_calls(fake_server) == [] and ran == []     # conftest 의 github.connect 는 부르면 실패한다


def test_연결_밖_레포_이름을_주면_거절한다(cluster, fake_server, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    for name in ("acme/elsewhere", "https://github.com/acme/elsewhere.git", "../../etc"):
        result = sync(read_repo_file(_ctx(cluster), "README.md", repository=name))
        assert not result.success and "연결된 레포가 아니에요" in result.stderr and REPO in result.stderr
    assert _token_calls(fake_server) == [] and gh.tokens == []
    # 연결된 레포는 URL 모양·대소문자가 달라도 된다
    assert sync(read_repo_file(_ctx(cluster), "README.md", repository="https://github.com/Acme/Manifests")).success


def test_다른_클러스터에_연결된_레포도_거절한다(cluster, fake_server, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    other = fake_server.add_cluster(owner="u-1")
    fake_server.link_repo(other, "acme/elsewhere")
    result = sync(list_repo_dir(_ctx(cluster), "", repository="acme/elsewhere"))
    assert not result.success and "연결된 레포가 아니에요" in result.stderr


def test_연결된_레포가_여럿이면_고르라고_한다(cluster, fake_server, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    fake_server.link_repo(cluster, "acme/elsewhere")
    result = sync(list_repo_dir(_ctx(cluster), ""))
    assert not result.success and "repository 로 하나를 골라" in result.stderr
    assert sync(list_repo_dir(_ctx(cluster), "", repository=REPO)).success


def test_토큰을_못_받으면_server_의_까닭을_돌려준다(cluster, fake_server, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    from kukie_agent import server_api

    async def refuse(cluster_id, repository):
        raise server_api.ServerError(409, "GITHUB_APP_SUSPENDED", "GitHub App 이 정지됐어요")

    monkeypatch.setattr(server_api, "repo_token", refuse)
    result = sync(list_repo_dir(_ctx(cluster), ""))
    assert not result.success and "GitHub App 이 정지됐어요" in result.stderr


def test_GitHub_가_거절하면_까닭을_돌려주고_토큰은_싣지_않는다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)

    async def broken(path, ref):
        raise github.GitHubError(403, "API rate limit exceeded")

    monkeypatch.setattr(gh, "contents", broken)
    result = sync(read_repo_file(_ctx(cluster), "README.md"))
    assert not result.success and "GitHub 에서 레포" in result.stderr and "rate limit" in result.stderr
    assert "ghs_fake-token" not in result.stderr


# ── 앱 렌더 결과 ───────────────────────────────────────

def test_plain_앱을_렌더하고_Secret_값은_가린다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [*APPS, _app("secret-app", "apps/secret")])
    web = sync(render_app(_ctx(cluster), "shop-web"))
    assert web.success, web.stderr
    assert web.command == "repo render argocd/shop-web@main"          # targetRevision HEAD = 기본 브랜치
    assert "kind: Deployment" in web.stdout and "ArgoCD 앱 argocd/shop-web · 레포 acme/manifests" in web.stdout
    secret = sync(render_app(_ctx(cluster), "secret-app", namespace="argocd"))
    assert secret.success and "hunter2" not in secret.stdout and "Secret shop/web-db" in secret.stdout


@needs_kustomize
def test_Kustomize_앱은_overlay_를_합친_결과를_준다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    result = sync(render_app(_ctx(cluster), "shop-prod"))
    assert result.success, result.stderr
    assert "replicas: 2" in result.stdout and "nginx:1.29-alpine" in result.stdout


def test_큰_렌더_결과도_앞부분과_N줄_더_있음만(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/plain/many.yaml"] = "\n---\n".join(
        f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: c{i}}}\ndata: {{a: b}}" for i in range(200))
    result = sync(render_app(_ctx(cluster), "shop-web"))
    assert result.success and "줄 더 있음 — 폴더는 GitHub 에서" in result.stdout
    _within_limits(result.stdout)


def test_렌더가_실패하면_까닭을_돌려준다(cluster, gh, monkeypatch):
    """레포 밖 주소를 가져오는 Kustomize — 렌더는 네트워크 없이 돌고, kustomize 를 돌리지도 않는다 (browse·render 안전장치)."""
    _cluster_apps(monkeypatch, [_app("remote", "apps/remote", source_type="Kustomize")])
    gh.commits["c0"]["apps/remote/kustomization.yaml"] = \
        "resources:\n  - http://169.254.169.254/latest/meta-data/\n"
    ran: list = []
    monkeypatch.setattr("kukie_agent.manifests.render.subprocess.run", lambda *a, **k: ran.append(a))
    result = sync(render_app(_ctx(cluster), "remote"))
    assert not result.success and "렌더하지 못했어요" in result.stderr and ran == []


def test_렌더_시간이_넘으면_실패로_돌려주고_다음에_다시_한다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    calls: list = []

    async def slow(*args):
        calls.append(args)
        raise renderer.RenderTimeout("렌더가 60초를 넘었어요")

    monkeypatch.setattr(browse, "render", slow)
    for _ in range(2):
        result = sync(render_app(_ctx(cluster), "shop-web"))
        assert not result.success and "60초를 넘었어요" in result.stderr
    assert len(calls) == 2


def test_실패_까닭이_길어도_상한_안이다(cluster, gh, monkeypatch):
    """실패 경로(_stopped)도 성공 경로와 같은 상한을 지킨다 (agent#129 리뷰)."""
    _cluster_apps(monkeypatch)

    async def failed(*args):
        return {"status": "render_failed", "message": "\n".join("e" * 3000 for _ in range(500)),
                "resources": [], "total": 0, "truncated": False}

    monkeypatch.setattr(browse, "render", failed)
    result = sync(render_app(_ctx(cluster), "shop-web"))
    assert not result.success and "렌더하지 못했어요" in result.stderr
    _within_limits(result.stderr)


@pytest.mark.parametrize("name,namespace,why", [
    ("web-chart", None, "Helm 앱은 아직 렌더 결과를 보여 주지 못해요"),
    ("nope", None, "찾지 못했어요"),
    ("shop-web", "elsewhere", "찾지 못했어요"),
    ("other-repo", None, "acme/elsewhere 가 이 클러스터에 연결돼 있지 않아요"),
])
def test_렌더하지_못하는_앱은_까닭을_돌려준다(cluster, fake_server, gh, monkeypatch, name, namespace, why):
    _cluster_apps(monkeypatch)
    result = sync(render_app(_ctx(cluster), name, namespace=namespace))
    assert not result.success and why in result.stderr
    assert gh.tokens == []


def test_Helm_앱이면_파일은_read_repo_file_로_읽으라고_한다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    assert "read_repo_file" in sync(render_app(_ctx(cluster), "web-chart")).stderr


def test_같은_이름의_앱이_여러_namespace_에_있으면_고르라고_한다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [*APPS, _app("shop-web", "apps/plain", namespace="team-a")])
    result = sync(render_app(_ctx(cluster), "shop-web"))
    assert not result.success and "namespace 를 골라" in result.stderr
    assert sync(render_app(_ctx(cluster), "shop-web", namespace="team-a")).success


def test_ArgoCD_가_없으면_그렇다고_한다(cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, fail='error: the server doesn\'t have a resource type "applications"')
    result = sync(render_app(_ctx(cluster), "shop-web"))
    assert not result.success and "ArgoCD 가 없어요" in result.stderr


# ── 대화에서: 읽기 블록, 승인 카드 없음 ────────────────────

@pytest.mark.parametrize("shared", [False, True])
def test_대화에서는_읽기_블록으로_그리고_승인_카드가_없다(dev_auth, fake_server, kubectl_ok, plan_dir, gh, monkeypatch,
                                                shared):
    _cluster_apps(monkeypatch)
    room = new_room(fake_server, shared=shared)
    fake_server.link_repo(fake_server.conversation(room)["clusterId"], REPO)
    client = TestClient(server.app)
    with agent.override(model=chat_model(call_tools=("list_repo_dir",))):
        response = client.post(f"/conversations/{room}/chat", json={"text": "레포 구조 보여줘"}, headers=USER)
    assert response.status_code == 200, response.text
    body = response.json()
    steps = body["response"]["steps"]
    assert [(step["step_label"], step["access"]) for step in steps] == [("레포 폴더 보기", "read-only")]
    assert fake_server.plans == {}
