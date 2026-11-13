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
from kukie_agent.manifests import Delete, MoveNamespace, Other, Patch, PatchOperation, Replicas


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


def test_내용이_None인_파일은_트리에서_지운다(api):
    """DURO-252: Git Data API 는 base_tree 위 트리 항목의 sha 를 null 로 주면 그 파일을 뺀다."""
    handlers, seen = api
    handlers[("GET", f"/repos/{REPO}/git/commits/c0")] = (200, {"tree": {"sha": "t0"}})
    handlers[("POST", f"/repos/{REPO}/git/trees")] = (201, {"sha": "t1"})
    handlers[("POST", f"/repos/{REPO}/git/commits")] = (201, {"sha": "c1"})
    handlers[("POST", f"/repos/{REPO}/git/refs")] = (201, {"ref": "refs/heads/kukie/x"})

    async def go():
        async with github.GitHub(REPO, "t") as gh:
            return await gh.create_branch_commit(base_sha="c0", branch="kukie/x",
                                                 files={"a.yaml": None, "k.yaml": "y\n"}, message="m")

    sync(go())
    tree = json.loads(next(r for r in seen if r.method == "POST").content)["tree"]
    assert tree == [{"path": "a.yaml", "mode": "100644", "type": "blob", "sha": None},
                    {"path": "k.yaml", "mode": "100644", "type": "blob", "content": "y\n"}]


def test_지운_파일의_승인_지문은_GitHub_PR_파일_목록처럼_빈_문자열이다():
    from kukie_agent.gitops.pull_requests import Draft
    draft = Draft(repository=REPO, base="main", base_sha="c0", files={"a.yaml": None, "b.yaml": "x"},
                  file_diff="", render_diff="")
    assert draft.file_entries() == [{"path": "a.yaml", "blob_sha": ""}, {"path": "b.yaml", "blob_sha": blob_sha("x")}]
    assert draft.conflict_keys() == [f"git:{REPO}:a.yaml", f"git:{REPO}:b.yaml"]


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


def test_Kustomize_앱의_경로_지정_변경은_렌더_전에_kubectl_길(monkeypatch):
    change = Patch(operations=[PatchOperation(op="set", path="spec.minReadySeconds", value=5)])
    found = _decide(monkeypatch, FakeCluster(source_type="Kustomize"), change=change)
    assert found.path == "kubectl" and "경로 지정 변경" in found.reason
    assert _decide(monkeypatch, FakeCluster(), change=change).path == "pr"


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



def test_슬래시가_든_브랜치_이름은_그대로_보낸다(api):
    """`env/prod` 를 `%2F` 로 보내면 GitHub 가 못 찾는다 (agent#126 리뷰)."""
    handlers, seen = api
    handlers[("GET", f"/repos/{REPO}/branches/env/prod")] = (200, {"commit": {"sha": "s1"}})

    async def go():
        async with github.GitHub(REPO, "t") as gh:
            return await gh.branch_head("env/prod")

    assert sync(go()) == "s1"
    assert seen[0].url.raw_path == f"/repos/{REPO}/branches/env/prod".encode()


def test_조회가_실패하면_길을_모른다고_표시한다(monkeypatch):
    """모름(known=False)은 길이 바뀐 것과 다르다 — 승인 순간 STALE 로 읽지 않는다 (agent#126 리뷰)."""
    assert _decide(monkeypatch, FakeCluster(unreachable=True)).known is False
    assert _decide(monkeypatch, FakeCluster(), repos_error=True).known is False
    found = _decide(monkeypatch, FakeCluster(managed=False))
    assert found.known is True and found.path == "kubectl"


# ── 클러스터에 없는 리소스 (DURO-248) ─────────────────────

#: sandbox-plain 실측(2026-11-13): 프로젝트가 막아 sync 가 거부된 Pod 는 health 없이 status OutOfSync 만 남는다
_NGINX = {"kind": "Pod", "name": "nginx", "namespace": "default", "status": "OutOfSync", "version": "v1"}
_WEB = {"group": "apps", "kind": "Deployment", "name": "web", "namespace": "shop", "status": "Synced", "version": "v1"}


def _cluster_with(*resources, **kwargs) -> FakeCluster:
    return FakeCluster(app_status={"sync": {"status": "OutOfSync"}, "health": {"status": "Healthy"},
                                   "resources": [dict(r) for r in resources]}, **kwargs)


def _decide_missing(monkeypatch, cluster: FakeCluster, *, kind="pod", name="nginx", namespace="default",
                    change=None):
    monkeypatch.setattr(route, "run_kubectl", cluster.run)

    async def manifest_repos(cluster_id):
        from kukie_agent import server_api
        return [server_api.ManifestRepo(id="r", cluster_id=cluster_id, repository=REPO, repository_id=1,
                                        installation_id=1)]

    from kukie_agent import server_api
    monkeypatch.setattr(server_api, "manifest_repos", manifest_repos)
    return sync(route.decide(kind=kind, name=name, namespace=namespace, context="c", kubeconfig=None,
                             cluster_id="cl-1", change=change or Replicas(count=1)))


def test_sync_실패로_클러스터에_없어도_git에_있으면_그_앱의_PR_길(monkeypatch):
    """kukie-gitops-sandbox#10: 프로젝트가 막아 sync 되지 않은 Pod — 클러스터에 없다고 kubectl 길로 빠졌다."""
    found = _decide_missing(monkeypatch, _cluster_with(_NGINX, _WEB), change=Other(description="이미지 바꾸기"))
    assert found.managed and found.known and found.application["metadata"]["name"] == "shop-web"
    assert found.target.api_version == "v1" and found.target.kind == "Pod"           # 렌더 결과와 맞출 정식 이름
    assert found.target.namespace == "default" and found.target.name == "nginx"
    assert "클러스터에 없고 git 에만 있어요" in found.reason and "shop-web" in found.reason
    # 앱으로 판단했으니 나머지 규칙은 그대로 — 지원하지 않는 요청이면 그 이유가 붙는다
    assert found.path == "kubectl" and "PR 로 못 바꿔요" in found.reason

    found = _decide_missing(monkeypatch, _cluster_with(dict(_WEB, name="api", status="OutOfSync",
                                                            health={"status": "Missing"})),
                            kind="deployments", name="api", namespace="shop")
    assert found.path == "pr" and found.target.api_version == "apps/v1" and found.target.kind == "Deployment"
    assert found.fingerprint()["application"] == {"name": "shop-web", "namespace": "argocd"}


@pytest.mark.parametrize("kind", ["Pod", "pod", "pods", "po"])
def test_kind는_짧은_이름과_복수형도_맞춘다(monkeypatch, kind):
    found = _decide_missing(monkeypatch, _cluster_with(_NGINX), kind=kind)
    assert found.path == "pr" and found.target.kind == "Pod"


def test_어느_앱에도_없거나_이미_Synced면_지금처럼_kubectl_길(monkeypatch):
    found = _decide_missing(monkeypatch, _cluster_with(_WEB))
    assert found.path == "kubectl" and found.known and not found.managed
    assert "이 리소스를 가진 ArgoCD 앱도 없어요" in found.reason
    synced = _decide_missing(monkeypatch, _cluster_with(dict(_NGINX, status="Synced")))
    assert synced.path == "kubectl" and "앱도 없어요" in synced.reason          # 클러스터에 없는데 Synced 는 지운 직후 등
    other_ns = _decide_missing(monkeypatch, _cluster_with(_NGINX), namespace="sandbox-plain")
    assert other_ns.path == "kubectl" and "앱도 없어요" in other_ns.reason


def test_여러_앱에_있으면_정하지_않는다(monkeypatch):
    found = _decide_missing(monkeypatch, _cluster_with(_NGINX, same_name_in="team-b"))
    assert found.path == "kubectl" and found.managed and "여러 ArgoCD 앱" in found.reason
    # namespace 를 모르면 다른 네임스페이스의 같은 이름도 후보다
    two = _decide_missing(monkeypatch, _cluster_with(_NGINX, dict(_NGINX, namespace="other")), namespace=None)
    assert two.path == "kubectl" and "default" in two.reason and "other" in two.reason


def test_다른_클러스터로_배포하는_앱이나_정리_대상_항목은_후보가_아니다(monkeypatch):
    """agent#155 리뷰: 다른 클러스터 앱의 같은 이름 리소스를 고르면 엉뚱한 환경의 레포를 고친다.
    requiresPruning 은 반대로 클러스터에만 있고 git 에는 없는 항목이다."""
    remote = _decide_missing(monkeypatch, _cluster_with(_NGINX, destination_server="https://prod.example.com"))
    assert remote.path == "kubectl" and "앱도 없어요" in remote.reason
    pruning = _decide_missing(monkeypatch, _cluster_with(dict(_NGINX, requiresPruning=True)))
    assert pruning.path == "kubectl" and "앱도 없어요" in pruning.reason


def test_프로젝트가_그_네임스페이스를_막으면_PR을_열지_않고_까닭을_알린다(monkeypatch):
    """agent#155 리뷰: sync 가 거부된 까닭이 프로젝트면, 값을 고치는 PR 을 머지해도 다시 거부돼 반영되지 않는다."""
    sandbox = [{"server": "https://kubernetes.default.svc", "namespace": "sandbox-*"}]
    found = _decide_missing(monkeypatch, _cluster_with(_NGINX, project_destinations=sandbox))
    assert found.path == "kubectl" and found.managed and found.known
    assert "default 네임스페이스를 허락하지 않아 sync 가 거부돼요" in found.reason and "'default'" in found.reason
    allowed = [{"server": "https://kubernetes.default.svc", "namespace": "*"}]
    assert _decide_missing(monkeypatch, _cluster_with(_NGINX, project_destinations=allowed)).path == "pr"
    # DURO-252 전에는 "Kukie PR 로는 아직 네임스페이스를 못 바꿔요" 였다 — 이제 옮기거나 빼는 PR 을 알려 준다
    assert "what=namespace" in found.reason and "what=delete" in found.reason and "앱 기본은 shop" in found.reason


def test_프로젝트가_막은_리소스도_옮기거나_지우는_변경은_PR_길이다(monkeypatch):
    """DURO-252 완료 기준 — sandbox nginx: default 를 막는 프로젝트의 Pod 를 허락된 곳으로 옮기는 PR 을 연다.
    옮길 곳의 허락은 manifests 가 본다(project_destinations 를 넘긴다)."""
    sandbox = [{"server": "https://kubernetes.default.svc", "namespace": "sandbox-*"}]
    moved = _decide_missing(monkeypatch, _cluster_with(_NGINX, project_destinations=sandbox),
                            change=MoveNamespace(to="sandbox-plain"))
    assert moved.path == "pr" and moved.source.project_destinations == sandbox
    assert moved.target.in_cluster is False and moved.source.destination_namespace == "shop"
    deleted = _decide_missing(monkeypatch, _cluster_with(_NGINX, project_destinations=sandbox), change=Delete())
    assert deleted.path == "pr" and deleted.source.project_destinations is None


def test_클러스터에_있는_리소스를_옮기면_프로젝트_규칙을_읽어_넘기고_prune을_읽는다(monkeypatch):
    allowed = [{"server": "https://kubernetes.default.svc", "namespace": "*"}]
    found = _decide(monkeypatch, FakeCluster(project_destinations=allowed), change=MoveNamespace(to="other"))
    assert found.path == "pr" and found.source.project_destinations == allowed and found.target.in_cluster is True
    assert found.source.auto_sync is True and found.source.prune is False             # FakeCluster: automated, prune 꺼짐
    plain = _decide(monkeypatch, FakeCluster(project_destinations=allowed), change=Delete())
    assert plain.path == "pr" and plain.source.project_destinations is None           # 지우기는 읽지 않는다
    kustomize = _decide(monkeypatch, FakeCluster(source_type="Kustomize"), change=Delete())
    assert kustomize.path == "pr"                                                     # 둘 다 Kustomize 앱도 된다


def test_namespace_없이_물었는데_앱이_적은_ns에_실제로_있으면_git에만_있는_게_아니다(monkeypatch):
    """agent#155 리뷰: 처음 조회는 context 기본 ns 라, 다른 ns 에 있는 drift(OutOfSync) 리소스를 없다고 볼 수 있다."""
    from kukie_agent.kubectl import KubectlResult
    cluster = _cluster_with(dict(_NGINX, namespace="team"))
    original = cluster.run

    def run(args, **kwargs):
        if args[:3] == ["get", "Pod", "nginx"] and args[-2:] == ["-n", "team"]:
            return KubectlResult(command="kubectl get", stdout=json.dumps({"kind": "Pod"}), stderr="", success=True)
        return original(args, **kwargs)

    cluster.run = run
    found = _decide_missing(monkeypatch, cluster, namespace=None)
    assert found.path == "kubectl" and "team 네임스페이스에 있어요" in found.reason
    cluster.run = original                                       # 그 ns 에도 없으면 git 에만 있다
    assert _decide_missing(monkeypatch, cluster, namespace=None).path == "pr"


def test_ArgoCD가_없거나_앱_목록을_못_읽으면(monkeypatch):
    absent = _decide_missing(monkeypatch, _cluster_with(_NGINX, no_argocd=True))
    assert absent.path == "kubectl" and absent.known and absent.reason == "pod default/nginx 가 클러스터에 없어요."  # 요청한 kind 그대로 (지금과 같다)
    cluster = _cluster_with(_NGINX)
    original = cluster.run

    def forbidden(args, **kwargs):
        if args[:2] == ["get", "applications.argoproj.io"]:
            from kukie_agent.kubectl import KubectlResult
            return KubectlResult(command="kubectl get", stdout="", success=False, exit_code=1,
                                 stderr='applications.argoproj.io is forbidden: User "x" cannot list resource')
        return original(args, **kwargs)

    cluster.run = forbidden
    found = _decide_missing(monkeypatch, cluster)
    # 정하지 못했다 — 승인 순간 다시 판단할 때 STALE 이 아니라 다시 시도한다
    assert found.path == "kubectl" and found.known is False and "확인하지 못했어요" in found.reason


@pytest.mark.parametrize("name,ok", [
    ("main", True), ("env/prod", True), ("release-1.0", True),
    ("../../pulls", False), ("a//b", False), ("/main", False), ("main/", False), ("a b", False),
    ("x.lock", False), (".hidden", False), ("a@{1}", False), ("a~1", False), ("", False),
])
def test_브랜치_이름은_경로에_넣기_전에_본다(name, ok):
    assert github.branch_name_ok(name) is ok


def test_이상한_targetRevision은_GitHub를_부르지_않고_없는_브랜치다(api):
    async def go():
        async with github.GitHub(REPO, "t") as gh:
            return await gh.branch_head("../../../user")

    assert sync(go()) is None and api[1] == []
