"""PR 길의 부품 — 레포 주소 · GitHub 클라이언트 · sync 지켜보기 · 길 판단 (DURO-134)."""
from __future__ import annotations

import io
import json
import tarfile

import httpx
import pytest

from fake_github import REPO, FakeCluster
from helpers import sync
from kukie_agent.gitops import github, route, watch
from kukie_agent.gitops.github import GitHubError, blob_sha
from kukie_agent.gitops.pull_requests import base_branch, secret_in
from kukie_agent.gitops.repo import repo_name
from kukie_agent.manifests import Other, Replicas


# ── 레포 주소 ─────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://github.com/Acme/Manifests.git", "https://github.com/acme/manifests", "http://github.com/acme/manifests/",
    "git@github.com:Acme/Manifests.git", "ssh://git@github.com/acme/manifests.git", "ssh://git@github.com:22/acme/manifests",
    "github.com/acme/manifests", "https://github.com/acme/manifests/tree/main/apps", "https://www.github.com/acme/manifests",
])
def test_GitHub_레포_주소는_모양이_달라도_같은_이름이다(url):
    assert repo_name(url) == "acme/manifests"


@pytest.mark.parametrize("url", [
    None, "", "https://gitlab.com/acme/manifests.git", "https://charts.bitnami.com/bitnami", "acme/manifests",
    "https://github.com/acme", "https://github.com/-bad/manifests", "https://github.com/acme/..",
])
def test_GitHub_레포가_아니면_None(url):
    assert repo_name(url) is None


# ── GitHub 클라이언트 ─────────────────────────────────────

def test_blob_id는_git과_같다():
    assert blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"   # git hash-object


def _tarball(entries: dict[str, str], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in entries.items():
            data = content.encode()
            info = tarfile.TarInfo(f"acme-manifests-abc/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo("acme-manifests-abc/link")
            info.type, info.linkname = tarfile.SYMTYPE, symlink
            archive.addfile(info)
    return buffer.getvalue()


@pytest.fixture
def api(monkeypatch):
    """GitHub REST 를 MockTransport 로. handlers[(method, path)] = (status, json 또는 bytes)."""
    handlers: dict[tuple[str, str], tuple[int, object]] = {}
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = handlers.get((request.method, request.url.path), (404, {"message": "Not Found"}))
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(github, "transport", httpx.MockTransport(handle))
    return handlers, seen


def test_레포를_받아_풀고_링크는_버린다(api, tmp_path):
    handlers, seen = api
    handlers[("GET", f"/repos/{REPO}/tarball/abc")] = (200, _tarball({"apps/a.yaml": "kind: A\n"}, symlink="/etc/passwd"))

    async def go():
        async with github.GitHub(REPO, "ghs_secret") as gh:
            return await gh.download("abc", tmp_path / "repo")

    root = sync(go())
    assert (root / "apps/a.yaml").read_text() == "kind: A\n"
    assert not (root / "link").exists()
    assert seen[0].headers["Authorization"] == "Bearer ghs_secret"


def test_오류에는_토큰이_실리지_않고_GitHub_message만_남는다(api):
    handlers, _ = api
    handlers[("GET", f"/repos/{REPO}")] = (403, {"message": "Resource not accessible by integration"})

    async def go():
        async with github.GitHub(REPO, "ghs_secret") as gh:
            await gh.info()

    with pytest.raises(GitHubError) as raised:
        sync(go())
    assert raised.value.status == 403 and "ghs_secret" not in str(raised.value)


def test_Kukie_브랜치가_아니면_만들지도_지우지도_않는다(api):
    async def go(call):
        async with github.GitHub(REPO, "t") as gh:
            await call(gh)

    with pytest.raises(ValueError):
        sync(go(lambda gh: gh.create_branch_commit(base_sha="c0", branch="main", files={}, message="m")))
    with pytest.raises(ValueError):
        sync(go(lambda gh: gh.delete_branch("main")))
    assert api[1] == []                         # GitHub 에 한 번도 닿지 않았다


def test_커밋은_새_kukie_브랜치_ref로만_간다(api):
    handlers, seen = api
    handlers[("GET", f"/repos/{REPO}/git/commits/c0")] = (200, {"tree": {"sha": "t0"}})
    handlers[("POST", f"/repos/{REPO}/git/trees")] = (201, {"sha": "t1"})
    handlers[("POST", f"/repos/{REPO}/git/commits")] = (201, {"sha": "c1"})
    handlers[("POST", f"/repos/{REPO}/git/refs")] = (201, {"ref": "refs/heads/kukie/x"})

    async def go():
        async with github.GitHub(REPO, "t") as gh:
            return await gh.create_branch_commit(base_sha="c0", branch="kukie/x", files={"a.yaml": "x\n"}, message="m")

    assert sync(go()) == "c1"
    bodies = [json.loads(r.content) for r in seen if r.method == "POST"]
    assert bodies[0] == {"base_tree": "t0", "tree": [{"path": "a.yaml", "mode": "100644", "type": "blob", "content": "x\n"}]}
    assert bodies[1] == {"message": "m", "tree": "t1", "parents": ["c0"]}
    assert bodies[2] == {"ref": "refs/heads/kukie/x", "sha": "c1"}
    assert not [r for r in seen if r.method == "PATCH"]         # 있는 ref 를 옮기는 호출은 없다


def test_머지는_승인한_head로_하고_막힌_방식은_건너뛴다(api):
    handlers, seen = api
    calls = iter([(405, {"message": "Squash merges are not allowed on this repository."}), (200, {"sha": "m1", "merged": True})])

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = next(calls)
        return httpx.Response(status, json=body)

    github.transport = httpx.MockTransport(handle)

    async def go():
        async with github.GitHub(REPO, "t") as gh:
            return await gh.merge(3, head_sha="h1", methods=("squash", "merge"), title="t")

    assert sync(go()) == "m1"
    assert [json.loads(r.content)["merge_method"] for r in seen] == ["squash", "merge"]
    assert all(json.loads(r.content)["sha"] == "h1" for r in seen)


def test_브랜치_보호의_405는_그대로_올린다(api):
    handlers, _ = api
    handlers[("PUT", f"/repos/{REPO}/pulls/3/merge")] = (405, {"message": "Required status check is expected."})

    async def go():
        async with github.GitHub(REPO, "t") as gh:
            await gh.merge(3, head_sha="h1", methods=("squash", "merge"), title="t")

    with pytest.raises(GitHubError) as raised:
        sync(go())
    assert raised.value.status == 405


# ── 작은 규칙들 ───────────────────────────────────────────

@pytest.mark.parametrize("revision,expected", [("", "main"), ("HEAD", "main"), ("release", "release"),
                                               ("refs/heads/prod", "prod")])
def test_targetRevision에서_base_브랜치(revision, expected):
    assert base_branch(revision, "main") == expected


@pytest.mark.parametrize("line,found", [
    ("+          value: Bearer abcdefghijklmnop", True), ("+  key: -----BEGIN RSA PRIVATE KEY-----", True),
    ("+kind: Secret", True), ("+  token: ghp_abcdefghijklmnopqrstuvwxyz0123", True),
    ("+  replicas: 3", False), ("-  value: Bearer abcdefghijklmnop", False), ("+++ b/secret.yaml", False),
])
def test_diff가_더하는_비밀_값을_찾는다(line, found):
    assert secret_in(f"--- a/x\n+++ b/x\n{line}\n") is found


# ── sync 지켜보기 ─────────────────────────────────────────

def _app(sync_status="Synced", revision="m1", health="Healthy", operation=None, message=None):
    status = {"sync": {"status": sync_status, "revision": revision}, "health": {"status": health}}
    if message:
        status["health"]["message"] = message
    if operation:
        status["operationState"] = operation
    return {"status": status}


def _watch(monkeypatch, states, **kwargs):
    states = list(states)

    def run(args, **kw):
        state = states.pop(0) if len(states) > 1 else states[0]
        from kukie_agent.kubectl import KubectlResult
        if state is None:
            return KubectlResult(command="k", stdout="", stderr="boom", success=False)
        return KubectlResult(command="k", stdout=json.dumps(state), stderr="", success=True, exit_code=0)

    monkeypatch.setattr(watch, "run_kubectl", run)
    clock = iter(range(0, 1000, 5))
    return watch.watch("shop-web", "argocd", "m1", auto_sync=kwargs.pop("auto_sync", True), context="c",
                       kubeconfig=None, wait=kwargs.pop("wait", 30), clock=lambda: next(clock), sleep=lambda s: None)


def test_머지_커밋으로_Synced_Healthy가_되면_healthy(monkeypatch):
    result = _watch(monkeypatch, [_app(revision="old"), _app(health="Progressing"), _app()])
    assert result["status"] == "healthy" and result["revision"] == "m1"


def test_sync가_실패하면_sync_failed(monkeypatch):
    op = {"phase": "Failed", "message": "one or more objects failed to apply", "syncResult": {"revision": "m1"}}
    result = _watch(monkeypatch, [_app(sync_status="OutOfSync", revision="old", operation=op)])
    assert result["status"] == "sync_failed" and "failed to apply" in result["message"]


def test_sync됐지만_Degraded면_degraded(monkeypatch):
    result = _watch(monkeypatch, [_app(health="Degraded", message="CrashLoopBackOff")])
    assert result["status"] == "degraded" and "CrashLoopBackOff" in result["message"]


def test_시간_안에_가져가지_않으면_timeout(monkeypatch):
    result = _watch(monkeypatch, [_app(revision="old", sync_status="Synced")], wait=10)
    assert result["status"] == "timeout" and "가져가지 않았어요" in result["message"]


def test_자동_sync가_꺼졌으면_기다리지_않는다(monkeypatch):
    result = _watch(monkeypatch, [None], auto_sync=False)
    assert result["status"] == "manual"


def test_앱을_계속_못_읽으면_unreadable(monkeypatch):
    assert _watch(monkeypatch, [None], wait=10)["status"] == "unreadable"


# ── 길 판단 ───────────────────────────────────────────────

def _decide(monkeypatch, cluster: FakeCluster, *, connected=(REPO,), change=None, repos_error=False):
    monkeypatch.setattr(route, "run_kubectl", cluster.run)

    async def manifest_repos(cluster_id):
        if repos_error:
            raise RuntimeError("server down")
        from kukie_agent import server_api
        return [server_api.ManifestRepo(id="r", cluster_id=cluster_id, repository=name, repository_id=1,
                                        installation_id=1) for name in connected]

    from kukie_agent import server_api
    monkeypatch.setattr(server_api, "manifest_repos", manifest_repos)
    return sync(route.decide(kind="deployment", name="web", namespace="shop", context="c", kubeconfig=None,
                             cluster_id="cl-1", change=change or Replicas(count=3)))


def test_관리_연결_지원이면_PR_길(monkeypatch):
    found = _decide(monkeypatch, FakeCluster())
    assert found.path == "pr" and found.repository == REPO
    assert found.target.api_version == "apps/v1" and found.source.path == "apps/plain"
    assert found.fingerprint()["application"] == {"name": "shop-web", "namespace": "argocd"}


@pytest.mark.parametrize("cluster,why", [
    (FakeCluster(managed=False), "관리하지 않아요"),
    (FakeCluster(source_type="Helm"), "Helm 앱"),
    (FakeCluster(repo_url="https://gitlab.com/acme/manifests.git"), "GitHub 레포가 아니라"),
])
def test_PR_길이_아니면_이유를_준다(monkeypatch, cluster, why):
    found = _decide(monkeypatch, cluster)
    assert found.path == "kubectl" and why in found.reason


def test_레포가_연결_안_됐으면_연결하라고_안내한다(monkeypatch):
    found = _decide(monkeypatch, FakeCluster(), connected=())
    assert found.path == "kubectl" and found.managed and "레포를 연결하면 PR 로" in found.reason


def test_지원하지_않는_요청이면_kubectl_길(monkeypatch):
    found = _decide(monkeypatch, FakeCluster(), change=Other(description="노드 셀렉터 바꾸기"))
    assert found.path == "kubectl" and "노드 셀렉터" in found.reason


def test_연결_목록을_못_읽으면_kubectl_길로_둔다(monkeypatch):
    assert _decide(monkeypatch, FakeCluster(), repos_error=True).path == "kubectl"


def test_받는_도중_한도를_넘으면_멈춘다(api, tmp_path, monkeypatch):
    """다 받은 뒤가 아니라 받으면서 센다 (agent#126 리뷰)."""
    handlers, _ = api
    handlers[("GET", f"/repos/{REPO}/tarball/abc")] = (200, _tarball({"a.yaml": "x" * 5000}))
    monkeypatch.setattr(github, "MAX_TARBALL_BYTES", 100)

    async def go():
        async with github.GitHub(REPO, "t") as gh:
            await gh.download("abc", tmp_path / "repo")

    with pytest.raises(GitHubError, match="너무 크다"):
        sync(go())
    assert not (tmp_path / "repo").exists()


def test_풀었을_때_크기가_한도를_넘으면_풀지_않는다(api, tmp_path, monkeypatch):
    handlers, _ = api
    handlers[("GET", f"/repos/{REPO}/tarball/abc")] = (200, _tarball({"big.yaml": "0" * 100_000}))   # 잘 압축된다
    monkeypatch.setattr(github, "MAX_EXTRACTED_BYTES", 10_000)

    async def go():
        async with github.GitHub(REPO, "t") as gh:
            await gh.download("abc", tmp_path / "repo")

    with pytest.raises(GitHubError, match="너무 크다"):
        sync(go())
    assert not (tmp_path / "repo" / "acme-manifests-abc" / "big.yaml").exists()
