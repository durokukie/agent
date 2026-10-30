"""매니페스트 레포 보기 — ArgoCD 화면 "레포" 탭의 폴더·파일·렌더 결과 API (DURO-148).

GitHub 는 tests/fake_github.py (레포 내용은 tests/fixtures/manifests + 테스트가 더한 파일), 클러스터의 Application 목록은
clusters_api.run_kubectl 을 바꿔 끼운다. Kustomize 렌더는 진짜 `kustomize build` 를 돈다 — 없으면 건너뛴다(CI 는 깔고 돈다).
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from fake_github import REPO, FakeGitHub
from helpers import OTHER, USER
from kukie_agent import argocd, clusters_api, server
from kukie_agent.gitops import browse, github
from kukie_agent.kubectl import KubectlResult
from kukie_agent.manifests import render as renderer
from kukie_agent.manifests.render import kustomize_bin
from kukie_agent.server_api import ServerError

TOKEN = "ghs_fake-token"
needs_kustomize = pytest.mark.skipif(
    shutil.which(kustomize_bin()) is None and not os.environ.get("CI"),
    reason="kustomize 가 없다 (CI 에서는 깔고 돈다)")

SECRET = """apiVersion: v1
kind: Secret
metadata:
  name: web-db
  namespace: shop
  annotations:
    kubectl.kubernetes.io/last-applied-configuration: '{"data":{"url":"cG9zdGdyZXM6Ly8="}}'
type: Opaque
data:
  url: cG9zdGdyZXM6Ly8=
"""


@pytest.fixture(autouse=True)
def _forget(monkeypatch):
    browse.forget()
    monkeypatch.setattr(renderer, "_loopback_scans", renderer.OrderedDict())
    yield
    browse.forget()


@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    fake.commits["c0"].update({
        "apps/secret/db.yaml": SECRET,
        "apps/secret/plain.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: web}\ndata: {a: b}\n",
        "apps/helm/Chart.yaml": "apiVersion: v2\nname: web\nversion: 0.1.0\n",
    })
    monkeypatch.setattr(github, "connect", fake.connect)
    return fake


@pytest.fixture
def cluster(fake_server):
    cluster_id = fake_server.add_cluster(owner="u-1")
    fake_server.link_repo(cluster_id, REPO)
    return cluster_id


def _ok(body) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout=json.dumps(body), stderr="", success=True, exit_code=0)


def _fail(stderr: str) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout="", stderr=stderr, success=False, exit_code=1)


def _app(name: str, path: str | None, *, source_type: str = "Directory", revision: str = "HEAD",
         repo_url: str = "https://github.com/Acme/Manifests.git", **source) -> dict:
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {"project": "default", "destination": {"namespace": "shop"},
                 "source": {"repoURL": repo_url, "targetRevision": revision,
                            **({"path": path} if path is not None else {}), **source}},
        "status": {"sourceType": source_type, "sync": {"status": "Synced", "revision": "c0"}},
    }


APPS = [
    _app("shop-web", "apps/plain"),
    _app("shop-prod", "apps/kustomize/overlays/prod", source_type="Kustomize", revision="main"),
    _app("shop-dev", "apps/kustomize/overlays/dev", source_type="Kustomize", revision="dev"),
    _app("other-repo", "apps/plain", repo_url="https://github.com/acme/elsewhere"),
]


def _cluster_apps(monkeypatch, items=APPS, *, fail: str | None = None) -> list:
    ran: list = []

    def run(args, **kwargs):
        ran.append(args)
        if fail is not None:
            return _fail(fail)
        if "-A" in args:
            return _ok({"items": items})
        found = next((item for item in items if item["metadata"]["name"] == args[2]), None)
        if found is None:
            return _fail(f'Error from server (NotFound): applications.argoproj.io "{args[2]}" not found')
        return _ok(found)

    monkeypatch.setattr(clusters_api, "run_kubectl", run)
    return ran


def _tree(client, cluster, **params):
    return client.get(f"/clusters/{cluster}/gitops/repos/Acme/Manifests/tree", params=params, headers=USER)


def _file(client, cluster, path, **params):
    return client.get(f"/clusters/{cluster}/gitops/repos/acme/manifests/file", params={"path": path, **params},
                      headers=USER)


def _rendered(client, cluster, name, **params):
    return client.get(f"/clusters/{cluster}/argocd/applications/argocd/{name}/rendered", params=params, headers=USER)


# ── 폴더 ──────────────────────────────────────────────

def test_폴더는_한_단계만_폴더먼저_이름순이고_앱_배지를_붙인다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    r = _tree(client, cluster, path="apps")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["repository"], body["ref"], body["sha"], body["default_branch"], body["path"]) == (
        REPO, "main", "c0", "main", "apps")
    assert body["html_url"] == "https://github.com/acme/manifests/tree/c0/apps"
    assert [(e["name"], e["type"]) for e in body["entries"]] == [
        ("helm", "dir"), ("kustomize", "dir"), ("plain", "dir"), ("secret", "dir")]
    assert (body["total"], body["truncated"], body["applications_status"]) == (4, False, "ok")
    badges = {e["name"]: [(a["name"], a["relation"], a["same_revision"]) for a in e["applications"]]
              for e in body["entries"]}
    # 다른 레포의 앱은 붙지 않는다. dev 브랜치를 따라가는 앱은 main 을 볼 때 same_revision 이 아니다
    assert badges == {
        "helm": [], "secret": [],
        "kustomize": [("shop-dev", "below", False), ("shop-prod", "below", True)],
        "plain": [("shop-web", "here", True)],
    }
    assert {a["name"] for a in body["applications"]} == {"shop-web", "shop-prod", "shop-dev"}
    assert TOKEN not in r.text
    assert gh.tokens == [TOKEN]


def test_앱_path_폴더와_그_아래는_here_above로_보인다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    here = _tree(client, cluster, path="apps/plain").json()
    assert [(a["name"], a["relation"], a["source_type"]) for a in here["applications"]] == [
        ("shop-web", "here", "Directory")]
    assert [e["name"] for e in here["entries"]] == ["deployment.yaml", "service.yaml", "worker.yaml"]
    assert all(e["type"] == "file" and e["size"] > 0 and e["applications"] == [] for e in here["entries"])
    base = _tree(client, cluster, path="apps/kustomize/overlays").json()
    assert [(a["name"], a["relation"]) for a in base["applications"]] == [("shop-dev", "below"), ("shop-prod", "below")]


def test_항목_상한을_넘으면_잘라서_truncated(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    monkeypatch.setattr(browse, "MAX_ENTRIES", 2)
    body = _tree(client, cluster, path="apps").json()
    assert ([e["name"] for e in body["entries"]], body["total"], body["truncated"]) == (["helm", "kustomize"], 4, True)


def test_같은_커밋의_폴더는_GitHub_를_다시_읽지_않고_ref_는_TTL_뒤에_ETag_로_묻는다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(browse, "_now", lambda: clock[0])
    for _ in range(2):
        assert _tree(client, cluster, path="apps").status_code == 200
    assert [call for call in gh.reads if call[0] != "contents"] == [("commit_sha", "main")]
    assert gh.reads.count(("contents", "apps")) == 1
    clock[0] += browse.REF_TTL + 1
    assert _tree(client, cluster, path="apps").json()["sha"] == "c0"
    assert gh.reads[-1] == ("commit_sha_304", "main") and gh.reads.count(("contents", "apps")) == 1
    gh.push_to_base("apps/new.yaml", "a: b\n")                  # 브랜치가 움직였다 — 새 커밋을 본다
    clock[0] += browse.REF_TTL + 1
    moved = _tree(client, cluster, path="apps").json()
    assert moved["sha"] != "c0" and "new.yaml" in [e["name"] for e in moved["entries"]]


def test_커밋_id_를_ref_로_주면_있는지_한_번만_묻는다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    sha = "a" * 40
    gh.commits[sha] = gh.commits["c0"]
    for _ in range(2):
        assert _tree(client, cluster, ref=sha).json()["sha"] == sha
        assert _file(client, cluster, "apps/plain/service.yaml", ref=sha).status_code == 200
    assert [call for call in gh.reads if call[0].startswith("commit_sha")] == [("commit_sha", sha)]


@pytest.mark.parametrize("call", [
    lambda client, cluster, sha: _tree(client, cluster, ref=sha, path="apps/kustomize"),
    lambda client, cluster, sha: _file(client, cluster, "apps/plain/worker.yaml", ref=sha),
    lambda client, cluster, sha: _rendered(client, cluster, "shop-web", ref=sha),
])
def test_기억한_커밋이_force_push_로_사라지면_404_GITOPS_REF_NOT_FOUND(client, cluster, gh, monkeypatch, call):
    _cluster_apps(monkeypatch)
    sha = "a" * 40
    gh.commits[sha] = gh.commits["c0"]
    assert _tree(client, cluster, ref=sha).status_code == 200            # 있다고 확인해 기억했다
    del gh.commits[sha]
    for _ in range(2):                                                   # 기억에서 빠져 두 번째도 같은 답
        r = call(client, cluster, sha)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "GITOPS_REF_NOT_FOUND", r.text


def test_브랜치가_TTL_안에_force_push_되어도_ref_없음으로_답하고_새_커밋을_다시_묻는다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    assert _tree(client, cluster, path="apps").json()["sha"] == "c0"
    gh.push_to_base("apps/plain/new.yaml", "a: b\n")
    del gh.commits["c0"]                                                 # main 을 force-push — c0 이 사라졌다
    r = _tree(client, cluster, path="apps/plain")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "GITOPS_REF_NOT_FOUND"
    again = _tree(client, cluster, path="apps/plain").json()
    assert again["sha"] != "c0" and "new.yaml" in [e["name"] for e in again["entries"]]


def test_경로만_없으면_커밋을_확인한_뒤_PATH_NOT_FOUND(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    r = _tree(client, cluster, path="apps/nope")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "GITOPS_PATH_NOT_FOUND"
    assert ("commit_sha", "c0") in gh.reads


@pytest.mark.parametrize("call", [
    lambda client, cluster: _tree(client, cluster, ref="b" * 40),
    lambda client, cluster: _file(client, cluster, "apps/plain/service.yaml", ref="b" * 40),
    lambda client, cluster: _rendered(client, cluster, "shop-web", ref="b" * 40),
])
def test_없는_커밋_id_는_404_GITOPS_REF_NOT_FOUND(client, cluster, gh, monkeypatch, call):
    _cluster_apps(monkeypatch)
    r = call(client, cluster)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "GITOPS_REF_NOT_FOUND"
    assert not [read for read in gh.reads if read[0] in ("contents", "download")]


def test_앱_목록을_못_읽어도_폴더는_준다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, fail='Error from server (Forbidden): applications.argoproj.io is forbidden: User "kukie"')
    r = _tree(client, cluster, path="apps")
    assert r.status_code == 200
    assert r.json()["applications_status"] == "forbidden" and r.json()["applications"] == []


@pytest.mark.parametrize(("params", "code"), [
    ({"path": "apps/../.."}, "GITOPS_PATH_NOT_FOUND"),
    ({"path": "apps//plain"}, "GITOPS_PATH_NOT_FOUND"),
    ({"ref": "main..dev"}, "GITOPS_REF_NOT_FOUND"),
    ({"ref": "a b"}, "GITOPS_REF_NOT_FOUND"),
])
def test_레포_밖_경로와_ref_꼴이_아닌_것은_GitHub_없이_404(client, cluster, gh, monkeypatch, params, code):
    _cluster_apps(monkeypatch)
    r = _tree(client, cluster, **params)
    assert r.status_code == 404 and r.json()["detail"]["code"] == code
    assert gh.reads == [] and gh.tokens == []


@pytest.mark.parametrize(("params", "code"), [
    ({"path": "apps/nope"}, "GITOPS_PATH_NOT_FOUND"),
    ({"path": "apps/plain/service.yaml"}, "GITOPS_PATH_NOT_FOUND"),      # 파일은 폴더가 아니다
    ({"ref": "release"}, "GITOPS_REF_NOT_FOUND"),
])
def test_레포에_없는_ref_경로는_404(client, cluster, gh, monkeypatch, params, code):
    _cluster_apps(monkeypatch)
    r = _tree(client, cluster, **params)
    assert r.status_code == 404 and r.json()["detail"]["code"] == code


# ── 레포 미연결 · 토큰 실패 · 권한 ───────────────────────

def test_연결_안_된_레포는_409이고_GitHub_를_부르지_않는다(client, fake_server, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    cluster = fake_server.add_cluster(owner="u-1")
    for url in (f"/clusters/{cluster}/gitops/repos/acme/manifests/tree",
                f"/clusters/{cluster}/gitops/repos/not%20a/name/tree",
                f"/clusters/{cluster}/gitops/repos/acme/manifests/file?path=apps/plain/service.yaml"):
        r = client.get(url, headers=USER)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "GITOPS_REPO_NOT_CONNECTED", url
    assert gh.tokens == [] and gh.reads == []


def test_다른_클러스터에_연결된_레포도_409(client, fake_server, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    other = fake_server.add_cluster(owner="u-1")
    r = _tree(client, other)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GITOPS_REPO_NOT_CONNECTED"


@pytest.mark.parametrize(("status", "code"), [
    (409, "GITHUB_APP_SUSPENDED"), (409, "GITHUB_APP_NOT_INSTALLED"), (502, "GITHUB_UNAVAILABLE")])
def test_토큰을_못_받으면_server_의_코드를_그대로_전한다(client, cluster, fake_server, gh, monkeypatch, status, code):
    _cluster_apps(monkeypatch)

    async def refuse(cluster_id, repository):
        raise ServerError(status, code, "토큰을 줄 수 없다")

    monkeypatch.setattr(fake_server, "repo_token", refuse)
    monkeypatch.setattr("kukie_agent.server_api.repo_token", refuse)
    r = _tree(client, cluster)
    assert r.status_code == status and r.json()["detail"]["code"] == code
    assert gh.reads == []


def test_GitHub_가_거절하면_502_GITHUB_UNAVAILABLE_이고_토큰은_싣지_않는다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)

    async def rate_limited(*args, **kwargs):
        raise github.GitHubError(403, "API rate limit exceeded for installation")

    monkeypatch.setattr(gh, "commit_sha", rate_limited)
    r = _tree(client, cluster)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "GITHUB_UNAVAILABLE"
    assert "rate limit" in r.json()["detail"]["message"] and TOKEN not in r.text


def test_볼_수_없는_클러스터는_404이고_토큰도_받지_않는다(client, cluster, fake_server, gh, monkeypatch):
    ran = _cluster_apps(monkeypatch)
    r = client.get(f"/clusters/{cluster}/gitops/repos/acme/manifests/tree", headers=OTHER)
    assert r.status_code == 404 and r.json()["detail"]["code"] != "GITOPS_REPO_NOT_CONNECTED"
    assert not [call for call in fake_server.calls if call[0] == "repo_token"] and ran == [] and gh.reads == []


# ── 파일 ──────────────────────────────────────────────

def test_파일은_내용과_객체마다_kind_name_namespace_를_준다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    r = _file(client, cluster, "apps/plain/deployment.yaml")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["name"], body["masked_reason"], body["parse_error"]) == (
        "ok", "deployment.yaml", None, None)
    assert body["content"] == gh.commits["c0"]["apps/plain/deployment.yaml"]
    assert body["objects"] == [{"api_version": "apps/v1", "kind": "Deployment", "name": "web", "namespace": None}]
    assert body["html_url"] == "https://github.com/acme/manifests/blob/c0/apps/plain/deployment.yaml"
    assert [(a["name"], a["relation"]) for a in body["applications"]] == [("shop-web", "here")]
    assert body["size"] == len(body["content"].encode())


def test_Secret_파일은_내용_대신_가리고_객체_이름은_준다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    body = _file(client, cluster, "apps/secret/db.yaml").json()
    assert (body["status"], body["content"], body["masked_reason"]) == ("masked", None, "kind: Secret")
    assert body["objects"] == [{"api_version": "v1", "kind": "Secret", "name": "web-db", "namespace": "shop"}]
    assert "cG9zdGdyZXM6Ly8=" not in json.dumps(body)


def test_큰_파일은_내용_없이_too_large(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/big.yaml"] = "a: " + "x" * browse.MAX_FILE_BYTES
    body = _file(client, cluster, "apps/big.yaml").json()
    assert (body["status"], body["content"], body["objects"]) == ("too_large", None, [])


def test_글자가_아닌_파일은_binary(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/logo.png"] = b"\x89PNG\r\n\x1a\n\x00\xff"
    body = _file(client, cluster, "apps/logo.png").json()
    assert (body["status"], body["content"]) == ("binary", None)


@pytest.mark.parametrize("data", [b"\xff\xd8\xff\xe0JFIF", b"caf\xe9\n"])     # NUL 없이 UTF-8 이 아닌 것 (JPEG, latin-1)
def test_UTF8_이_아닌_파일도_500_이_아니라_binary(client, cluster, gh, monkeypatch, data):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/photo.jpg"] = data
    r = _file(client, cluster, "apps/photo.jpg")
    assert r.status_code == 200 and (r.json()["status"], r.json()["content"]) == ("binary", None)


def test_YAML_로_못_읽는_파일도_내용은_준다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/helm/templates/svc.yaml"] = "kind: Service\nmetadata:\n  name: {{ .Release.Name }}\n  x: [\n"
    body = _file(client, cluster, "apps/helm/templates/svc.yaml").json()
    assert body["status"] == "ok" and body["objects"] == [] and body["parse_error"].startswith("YAML 로 읽지 못했어요")


@pytest.mark.parametrize("path", ["apps/plain", ""])
def test_폴더를_파일로_읽으면_404(client, cluster, gh, monkeypatch, path):
    _cluster_apps(monkeypatch)
    r = _file(client, cluster, path)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "GITOPS_PATH_NOT_FOUND"


@pytest.mark.parametrize(("path", "text", "reason"), [
    ("a/secret.yaml", SECRET, "kind: Secret"),
    ("a/s.yaml", "apiVersion: v1\nkind: Secret\nmetadata: {name: x}\nstringData: {password: hunter2}\n", "kind: Secret"),
    ("a/x.yaml", "apiVersion: v1\nkind: Whatever\nmetadata: {name: x}\nstringData: {password: hunter2}\n", "stringData"),
    ("a/db.yaml", "apiVersion: v1\nkind: ConfigMap\ndata:\n  k: ENC[AES256_GCM,data:abc]\nsops:\n  mac: x\n", "sops"),
    ("a/db.enc.yaml", "password: abc\n", "이름이 비밀 파일 꼴이에요"),
    ("a/db.enc", "abc", "이름이 비밀 파일 꼴이에요"),
    ("a/secret.env", "PASSWORD=abc\n", "이름이 비밀 파일 꼴이에요"),
    ("a/.env.prod", "PASSWORD=abc\n", "이름이 비밀 파일 꼴이에요"),
    ("a/tls.key", "abc", "이름이 비밀 파일 꼴이에요"),
    ("a/kustomization.yaml", "secretGenerator:\n  - name: db\n    literals: [password=abc]\n", "secretGenerator literals"),
    ("a/templates/secret.yaml", "kind: Secret\ndata:\n  p: {{ .Values.p | b64enc }}\n", "kind: Secret"),
    ("a/notes.txt", "-----BEGIN RSA PRIVATE KEY-----\nabc\n", "비밀 키"),
])
def test_Secret_처럼_보이는_것(path, text, reason):
    docs, _ = browse._docs(path, text)
    assert browse.secret_reason(path, text, docs) == reason


@pytest.mark.parametrize(("path", "text"), [
    ("apps/plain/deployment.yaml", FakeGitHub().commits["c0"]["apps/plain/deployment.yaml"]),   # secretKeyRef 는 값이 아니다
    ("a/sealed.yaml", "apiVersion: bitnami.com/v1alpha1\nkind: SealedSecret\nmetadata: {name: x}\n"
                      "spec: {encryptedData: {p: AgBy3i4OJSWK}}\n"),
    ("a/external.yaml", "apiVersion: external-secrets.io/v1\nkind: ExternalSecret\nmetadata: {name: x}\n"),
    ("a/kustomization.yaml", "secretGenerator:\n  - name: db\n    envs: [db.env]\n"),          # 값은 db.env 쪽에 있다
    ("a/README.md", "Secret 은 sops 로 암호화한다"),
])
def test_Secret_이_아닌_것은_가리지_않는다(path, text):
    docs, _ = browse._docs(path, text)
    assert browse.secret_reason(path, text, docs) is None


# ── 앱 렌더 결과 ────────────────────────────────────────

def test_plain_앱은_폴더의_YAML_을_그대로_렌더하고_Secret_값은_가린다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [_app("shop-secret", "apps/secret")])
    r = _rendered(client, cluster, "shop-secret")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["repository"], body["path"], body["source_type"]) == (
        "ok", REPO, "apps/secret", "Directory")
    assert (body["ref"], body["sha"], body["target_revision"], body["sync_revision"]) == ("main", "c0", "HEAD", "c0")
    assert body["destination_namespace"] == "shop" and body["application"] == {"namespace": "argocd", "name": "shop-secret"}
    assert [(x["kind"], x["name"], x["namespace"], x["masked"], x["masked_reason"]) for x in body["resources"]] == [
        ("Secret", "web-db", "shop", True, "kind: Secret"), ("ConfigMap", "web", None, False, None)]
    secret = body["resources"][0]["yaml"]
    assert "url: (가림)" in secret and "cG9zdGdyZXM6Ly8=" not in r.text and "last-applied" not in secret
    assert (body["total"], body["truncated"]) == (2, False)
    assert TOKEN not in r.text


def test_렌더_결과도_파일과_같은_규칙으로_가린다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [_app("leaky", "apps/leaky")])
    gh.commits["c0"].update({
        "apps/leaky/key.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: tls}\n"
                               "data: {key: \"-----BEGIN RSA PRIVATE KEY-----\\nMIIabc\"}\n",
        "apps/leaky/token.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: gh}\n"
                                 "data: {token: ghp_abcdefghijklmnopqrstuvwxyz0123}\n",
        "apps/leaky/aws.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: aws}\ndata: {id: AKIAABCDEFGHIJKLMNOP}\n",
        "apps/leaky/sops.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: enc}\n"
                                "data: {k: \"ENC[AES256_GCM,data:abc]\"}\nsops: {mac: x}\n",
        "apps/leaky/odd.yaml": "apiVersion: example.com/v1\nkind: Thing\nmetadata: {name: odd}\n"
                               "stringData: {password: hunter2}\n",
        "apps/leaky/fine.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: fine}\ndata: {a: b}\n",
        "apps/leaky/secret.yaml": SECRET,
        "apps/leaky/secret-note.yaml": SECRET.replace("name: web-db", "name: web-key").replace(
            "  annotations:\n", "  annotations:\n    note: AKIAABCDEFGHIJKLMNOP\n"),       # 값 밖에 키가 샌 Secret
    })
    r = _rendered(client, cluster, "leaky")
    body = r.json()
    got = {x["name"]: (x["masked"], x["masked_reason"], x["yaml"] is None) for x in body["resources"]}
    assert got == {
        "aws": (True, "비밀 키", True), "enc": (True, "sops", True), "fine": (False, None, False),
        "gh": (True, "비밀 키", True), "odd": (True, "stringData", True), "tls": (True, "비밀 키", True),
        "web-db": (True, "kind: Secret", False), "web-key": (True, "비밀 키", True),
    }
    for leaked in ("MIIabc", "ghp_abcdef", "AKIAABCDEFGHIJKLMNOP", "hunter2", "cG9zdGdyZXM6Ly8="):
        assert leaked not in r.text


@needs_kustomize
def test_Kustomize_앱은_kustomize_build_결과를_준다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    body = _rendered(client, cluster, "shop-prod").json()
    assert body["status"] == "ok", body
    names = {(x["kind"], x["name"], x["namespace"]) for x in body["resources"]}
    assert ("Deployment", "api", "shop-prod") in names and ("Service", "api", "shop-prod") in names
    api = next(x for x in body["resources"] if (x["kind"], x["name"]) == ("Deployment", "api"))
    assert "replicas: 2" in api["yaml"] and "nginx:1.29-alpine" in api["yaml"]


@needs_kustomize
def test_kustomize_가_실패하면_render_failed_이고_임시_경로는_싣지_않는다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [_app("broken", "apps/broken", source_type="Kustomize")])
    gh.commits["c0"]["apps/broken/kustomization.yaml"] = "resources:\n  - ../nope\n"
    body = _rendered(client, cluster, "broken").json()
    assert body["status"] == "render_failed" and body["resources"] == []
    assert "kukie-repo-view-" not in body["message"]


@pytest.mark.parametrize("remote", [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "https://raw.githubusercontent.com/acme/x/main/deploy.yaml",
    "github.com/kubernetes-sigs/kustomize//examples/multibases?ref=v5.8.1",
    "git@github.com:acme/x.git//base",
    "ssh://git@github.com/acme/x.git",
    "git::https://github.com/acme/x.git//base",
    "gh:acme/x//base",
])
def test_레포_밖_주소를_가져오는_Kustomize_는_돌리지_않는다(client, cluster, gh, monkeypatch, remote):
    _cluster_apps(monkeypatch, [_app("remote", "apps/remote/overlay", source_type="Kustomize")])
    # overlay 는 로컬 base 만 쓰고, 원격 주소는 base 안에 숨어 있다 — 따라가서 본다
    gh.commits["c0"].update({
        "apps/remote/overlay/kustomization.yaml": "resources:\n  - ../base\n",
        "apps/remote/base/kustomization.yaml": f"resources:\n  - deploy.yaml\n  - {remote}\n",
        "apps/remote/base/deploy.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n",
    })
    ran: list = []
    monkeypatch.setattr("kukie_agent.manifests.render.subprocess.run", lambda *a, **k: ran.append(a))
    body = _rendered(client, cluster, "remote").json()
    assert body["status"] == "render_failed" and renderer._redact(remote) in body["message"] and ran == []


# ── 렌더는 네트워크 없이 (manifests/render.py, agent#128 리뷰) ─────────────

@pytest.fixture
def listener():
    """요청을 받으면 적어 두고 403 으로 답하는 로컬 HTTP 서버 — 대상 서버·프록시 둘 다로 쓴다."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(403)
            self.end_headers()

        do_CONNECT = do_GET

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def _kustomization(tmp_path, text: str):
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    (app / "kustomization.yaml").write_text(text)
    return app


#: 칸 검사(remote_reference)가 보지 않는 칸 — 막는 것이 네트워크 차단인지 본다
GENERATOR = "configMapGenerator:\n- name: c\n  files:\n  - creds={url}\n"


@needs_kustomize
def test_kustomize_의_바깥_요청은_프록시로만_간다(tmp_path, monkeypatch, listener):
    address, seen = listener
    monkeypatch.setattr(renderer, "OFFLINE_PROXY", f"http://{address}")
    app = _kustomization(tmp_path, GENERATOR.format(url="http://kukie-blocked.invalid/latest/meta-data"))
    with pytest.raises(renderer.RenderError):
        renderer.render("Kustomize", app, root=tmp_path)
    assert seen == ["http://kukie-blocked.invalid/latest/meta-data"]          # 대상이 아니라 프록시가 받았다


@needs_kustomize
@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "https://example.com/creds"])
def test_닿지_않는_프록시라_바깥_주소는_렌더_실패(tmp_path, url):
    app = _kustomization(tmp_path, GENERATOR.format(url=url))
    with pytest.raises(renderer.RenderError, match="네트워크를 쓰지 않아요"):
        renderer.render("Kustomize", app, root=tmp_path)


@needs_kustomize
def test_git_원격_base_도_네트워크_없이_실패(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "remote_reference", lambda folder, root: None)    # 칸 검사가 없어도
    app = _kustomization(tmp_path, "resources:\n- https://github.com/kubernetes-sigs/kustomize//examples/helloWorld"
                                   "?ref=v5.8.1\n")
    with pytest.raises(renderer.RenderError):
        renderer.render("Kustomize", app, root=tmp_path)


@pytest.mark.parametrize("host", [
    "{address}", "localhost:{port}", "LOCALHOST.:{port}", "a.localhost:{port}", "[::1]:{port}",
    "[::ffff:127.0.0.1]:{port}", "%6cocalhost:{port}", "user@127.0.0.1:{port}", "0.0.0.0:{port}",
])
def test_이_컴퓨터_안_주소는_돌리지_않는다(tmp_path, monkeypatch, listener, host):
    address, seen = listener
    port = address.rpartition(":")[2]
    ran: list = []
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **k: ran.append(a))
    app = _kustomization(tmp_path, "resources: []\n")
    other = tmp_path / "elsewhere"                         # build 가 따라가지 않는 곳에 있어도 본다
    other.mkdir()
    (other / "kustomization.yaml").write_text(GENERATOR.format(url=f"http://{host.format(address=address, port=port)}/x"))
    with pytest.raises(renderer.RenderError, match="이 컴퓨터 안 주소"):
        renderer.render("Kustomize", app, root=tmp_path)
    assert ran == [] and seen == []


def test_플러그인_설정_파일의_이_컴퓨터_안_주소도_거절한다(tmp_path, monkeypatch, listener):
    """generators: 가 가리키는 builtin 설정 파일 — 이름이 kustomization 이 아니어도 files 를 같은 로더로 읽는다 (v5.8.1 실측)."""
    address, seen = listener
    ran: list = []
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **k: ran.append(a))
    app = _kustomization(tmp_path, "generators:\n- gen.yaml\n")
    (app / "gen.yaml").write_text("apiVersion: builtin\nkind: ConfigMapGenerator\nmetadata: {name: leak}\n"
                                  f"files:\n- x=http://localhost:{address.rpartition(':')[2]}/internal\n")
    with pytest.raises(renderer.RenderError, match="이 컴퓨터 안 주소"):
        renderer.render("Kustomize", app, root=tmp_path)
    assert ran == [] and seen == []


def test_주석의_이_컴퓨터_안_주소는_보지_않는다(tmp_path):
    _kustomization(tmp_path, "# 로컬에서 볼 때: http://localhost:8080\nresources: []  # http://127.0.0.1:3000\n")
    assert renderer.loopback_reference(tmp_path, scan_key="a") is None
    (tmp_path / "app" / "patch.yaml").write_text("value: http://localhost:9090\n")   # 값이면 가져오지 않는 자리여도 거절(안전 쪽)
    assert renderer.loopback_reference(tmp_path, scan_key="b") == "http://localhost:9090"


@pytest.mark.parametrize(("name", "text"), [
    ("gen.yaml", 'files:\n- "x=http:\\u002f/127.0.0.1:15000/config_dump"\n'),          # 겹따옴표 \u 이스케이프
    ("gen.yaml", 'files:\n- "x=http:\\/\\/localhost:15000/a"\n'),                     # \/ 이스케이프
    ("gen.yaml", 'files:\n- "x=http:\\\n  //localhost:9999/a"\n'),                     # 줄 이음
    ("gen.conf", "files:\n- x=http://127.0.0.1:15000/config_dump\n"),                  # 확장자 우회
    ("generator", "files:\n- x=http://[::1]:15000/a\n"),                                # 확장자 없음
])
def test_이스케이프_줄이음_확장자로_숨긴_주소도_거절한다(tmp_path, monkeypatch, name, text):
    ran: list = []
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **k: ran.append(a))
    app = _kustomization(tmp_path, f"generators:\n- {name}\n")
    (app / name).write_text("apiVersion: builtin\nkind: ConfigMapGenerator\nmetadata: {name: leak}\n" + text)
    with pytest.raises(renderer.RenderError, match="이 컴퓨터 안 주소"):
        renderer.render("Kustomize", app, root=tmp_path)
    assert ran == []


def test_UTF16_파일도_읽어_본다(tmp_path):
    _kustomization(tmp_path, "resources: []\n")
    (tmp_path / "app" / "gen.yaml").write_bytes("files:\n- x=http://localhost:1/a\n".encode("utf-16"))
    assert renderer.loopback_reference(tmp_path) == "http://localhost:1"


def test_YAML_로_못_읽는_파일과_바이너리는_건너뛴다(tmp_path):
    _kustomization(tmp_path, "resources: []\n")
    (tmp_path / "chart").mkdir()
    (tmp_path / "chart" / "deployment.yaml").write_text(            # Helm 템플릿 — kustomize 가 설정으로 못 읽는다
        "{{- if .Values.x }}\nenv:\n  - value: http://localhost:8080\n  {{ end }\n: : [\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00http://localhost:1/")
    assert renderer.loopback_reference(tmp_path) is None


def test_큰_글자_파일은_막지_않고_흘려_읽으며_주소만_찾는다(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "MAX_SCAN_BYTES", 100)
    monkeypatch.setattr(renderer, "_STREAM_CHUNK", 64)
    _kustomization(tmp_path, "resources: []\n")
    (tmp_path / "crds.yaml").write_text("a: " + "x" * 500 + "\n")           # CRD 꾸러미 같은 큰 파일
    (tmp_path / "big.bin").write_bytes(b"\x00" + b"http://localhost:1/" * 20)  # 큰 바이너리는 건너뛴다
    assert renderer.loopback_reference(tmp_path, scan_key="1") is None
    # 조각(64바이트) 경계에 걸친 주소도 찾는다
    (tmp_path / "crds.yaml").write_text("a: " + "x" * 50 + " see http://127.0.0.1:15000/config_dump " + "y" * 300)
    assert renderer.loopback_reference(tmp_path, scan_key="2") == "http://127.0.0.1:15000"


def test_깊은_중첩도_500_이_아니라_글자_검사로_간다(tmp_path, monkeypatch):
    _kustomization(tmp_path, "resources: []\n")
    (tmp_path / "deep.yaml").write_text("a: " + "[" * 50_000 + "]" * 50_000 + "\n")   # 실제 파서가 RecursionError 를 낸다
    assert renderer.loopback_reference(tmp_path, scan_key="1") is None
    tried: list = []
    monkeypatch.setattr(renderer.yaml, "load_all", lambda *a, **k: (_ for _ in ()).throw(RecursionError()))
    monkeypatch.setattr(renderer, "_RuamelYAML", lambda **k: tried.append(k))     # YAMLError 가 아니면 다시 읽지 않는다
    with pytest.raises(renderer._ParserLimit):                                    # 부르는 쪽이 글자 검사로 보낸다
        renderer._parse(b"a: b\n")
    assert tried == []


def test_검사_시간이_넘으면_렌더하지_않고_기억하지도_않는다(tmp_path, monkeypatch):
    app = _kustomization(tmp_path, "resources: []\n")
    ticks = iter(range(0, 1000, 20))
    monkeypatch.setattr(renderer, "_scan_clock", lambda: next(ticks))
    with pytest.raises(renderer.RenderError, match="레포 검사 시간"):
        renderer.render("Kustomize", app, root=tmp_path, scan_key="slow")
    monkeypatch.setattr(renderer, "_scan_clock", lambda: 0.0)
    assert renderer.loopback_reference(tmp_path, scan_key="slow") is None


def test_문구의_주소에는_userinfo_와_경로를_싣지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **k: None)
    app = _kustomization(tmp_path, "resources: []\n")
    (tmp_path / "values.dev.yaml").write_text("db: postgres://svc:hunter2@localhost:5432/app?sslmode=off\n")
    with pytest.raises(renderer.RenderError) as caught:
        renderer.render("Kustomize", app, root=tmp_path)
    assert "postgres://localhost:5432" in str(caught.value) and "hunter2" not in str(caught.value)
    (tmp_path / "values.dev.yaml").unlink()
    (app / "kustomization.yaml").write_text("resources:\n- https://bot:ghp_secret@github.com/acme/x//base\n")
    with pytest.raises(renderer.RenderError) as caught:
        renderer.render("Kustomize", app, root=tmp_path, scan_key="2")
    assert "https://github.com" in str(caught.value) and "ghp_secret" not in str(caught.value)


@pytest.mark.parametrize("text", [
    'files:\n- "x=http:\\u002f/127.0.0.1:15000/config_dump"\n',
    'files:\n- "x=http:\\/\\/localhost:15000/a"\n',
    'files:\n- "x=http:\\\n  //localhost:9999/a"\n',
    'files:\n- "x=http\\x3a//[::1]:1/a"\n',
])
def test_큰_파일의_원시_글자_검사도_이스케이프를_풀어_본다(tmp_path, monkeypatch, text):
    monkeypatch.setattr(renderer, "MAX_SCAN_BYTES", 100)
    _kustomization(tmp_path, "generators:\n- gen.yaml\n")
    (tmp_path / "app" / "gen.yaml").write_text("# " + "x" * 200 + "\nkind: ConfigMapGenerator\n" + text)
    assert renderer.loopback_reference(tmp_path) is not None


def test_파서_한도에_걸린_파일은_건너뛰지_않고_글자로_본다(tmp_path):
    _kustomization(tmp_path, "resources: []\n")
    (tmp_path / "deep.yaml").write_text("b: http://localhost:1/x\na: " + "[" * 50_000 + "]" * 50_000 + "\n")
    assert renderer.loopback_reference(tmp_path) == "http://localhost:1"


def test_kustomize_오류_문구에도_userinfo_를_싣지_않는다(tmp_path, monkeypatch):
    app = _kustomization(tmp_path, "resources: []\n")
    failed = type("P", (), {"returncode": 1, "stdout": "",
                            "stderr": 'Error: Get "https://bot:ghp_secret@example.com/a": proxyconnect tcp: '
                                      'dial tcp 127.0.0.1:9: connect: connection refused'})()
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **k: failed)
    with pytest.raises(renderer.RenderError) as caught:
        renderer.render("Kustomize", app, root=tmp_path)
    assert "ghp_secret" not in str(caught.value) and "https://example.com/a" in str(caught.value)


def test_검사_시간_초과는_렌더_결과로_기억하지_않는다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [_app("a", "apps/kustomize/overlays/prod", source_type="Kustomize")])
    answers = iter([renderer.ScanTimeout("10초"), None])

    def scan(root, **kwargs):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(renderer, "loopback_reference", scan)
    monkeypatch.setattr(renderer, "render_kustomize", lambda folder, **k: [])
    first = _rendered(client, cluster, "a").json()
    assert first["status"] == "render_failed" and "레포 검사 시간" in first["message"] and first["sha"] == "c0"
    assert _rendered(client, cluster, "a").json()["status"] == "ok"             # 다시 하면 된다
    assert [read for read in gh.reads if read[0] == "download"] == [("download", "c0")] * 2


def test_별칭으로_부풀린_YAML_에_묶이지_않는다(tmp_path):
    lines = ["a0: &a0 [x]"] + [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}, *a{i - 1}, *a{i - 1}]" for i in range(1, 40)]
    (tmp_path / "bomb.yaml").write_text("\n".join(lines) + "\n# http://x\n")
    assert renderer.loopback_reference(tmp_path) is None


def test_같은_커밋은_한_번만_훑는다(tmp_path, monkeypatch):
    app = _kustomization(tmp_path, "resources: []\n")
    (app / "a.yaml").write_text("k: https://example.com/a\n")
    walked: list = []
    original = renderer._repo_files
    monkeypatch.setattr(renderer, "_repo_files", lambda root: walked.append(root) or original(root))
    for _ in range(4):                                       # PR 길은 kustomize 루트마다 전·후로 부른다
        assert renderer.loopback_reference(tmp_path, scan_key="acme/manifests@c0") is None
        assert renderer.loopback_reference(tmp_path) is None             # 열쇠가 없으면 레포 폴더로
    assert len(walked) == 2


def test_렌더_API_는_커밋마다_한_번만_훑는다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [_app("a", "apps/kustomize/overlays/prod", source_type="Kustomize"),
                                _app("b", "apps/kustomize/overlays/dev", source_type="Kustomize")])
    walked: list = []
    monkeypatch.setattr(renderer, "_repo_files", lambda root: walked.append(root) or [])
    monkeypatch.setattr(renderer, "render_kustomize", lambda folder, **k: [])
    for name in ("a", "b"):
        assert _rendered(client, cluster, name).json()["status"] == "ok"
    assert len(walked) == 1


def test_바깥_주소는_이_컴퓨터_안_주소가_아니다():
    for authority in ("example.com", "169.254.169.254", "10.0.0.1:443", "[2001:db8::1]", "127.0.0.1.nip.io"):
        assert not renderer._local_host(authority), authority


def test_kustomize_에는_에이전트_환경변수를_넘기지_않는다(monkeypatch):
    monkeypatch.setenv("KUKIE_INTERNAL_TOKEN", "internal-secret")
    env = renderer._offline_env("/tmp/home")
    assert "internal-secret" not in env.values() and env["PATH"] == os.devnull
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == renderer.OFFLINE_PROXY and env["NO_PROXY"] == ""
    assert env["GIT_ALLOW_PROTOCOL"] == "none"


@needs_kustomize
def test_렌더_API_도_생성기의_바깥_주소를_가져오지_않는다(client, cluster, gh, monkeypatch, listener):
    address, seen = listener
    monkeypatch.setattr(renderer, "OFFLINE_PROXY", f"http://{address}")
    _cluster_apps(monkeypatch, [_app("gen", "apps/gen", source_type="Kustomize")])
    gh.commits["c0"]["apps/gen/kustomization.yaml"] = GENERATOR.format(url="http://kukie-blocked.invalid/creds")
    body = _rendered(client, cluster, "gen").json()
    assert body["status"] == "render_failed" and body["resources"] == []
    assert seen == ["http://kukie-blocked.invalid/creds"]


def test_로컬만_쓰는_kustomization_은_원격이_아니다(tmp_path):
    from kukie_agent.manifests.render import remote_reference
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "kustomization.yaml").write_text("resources: [deploy.yaml]\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "kustomization.yaml").write_text(
        "resources: [../base, ../base]\ncomponents: [../base]\npatches:\n  - path: p.yaml\n")
    assert remote_reference(tmp_path / "app", tmp_path) is None


def test_같은_커밋의_렌더는_기억한다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    first = _rendered(client, cluster, "shop-web").json()
    again = _rendered(client, cluster, "shop-web").json()
    assert first == again and first["status"] == "ok"
    assert [call for call in gh.reads if call[0] == "download"] == [("download", "c0")]


def test_렌더할_Application_조회도_상세처럼_나눠_쓴다(client, cluster, gh, monkeypatch):
    monkeypatch.setattr(clusters_api, "OVERVIEW_SHARE_TTL", 10)
    ran = _cluster_apps(monkeypatch)
    for _ in range(2):
        assert _rendered(client, cluster, "shop-web").json()["status"] == "ok"
    _rendered(client, cluster, "shop-prod")
    gets = [args[:3] for args in ran if "-A" not in args]
    assert gets == [["get", "applications.argoproj.io", "shop-web"], ["get", "applications.argoproj.io", "shop-prod"]]
    assert _rendered(client, cluster, "nope").status_code == 404
    assert _rendered(client, cluster, "nope").status_code == 404 and len(ran) == 4       # 404 는 나눠 쓰지 않는다


def test_ref_를_주면_그_커밋을_렌더한다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.push_to_base("apps/plain/service.yaml", "apiVersion: v1\nkind: Service\nmetadata: {name: renamed}\n")
    head = _rendered(client, cluster, "shop-web").json()
    synced = _rendered(client, cluster, "shop-web", ref="c0").json()
    assert head["sha"] != "c0" and synced["sha"] == "c0"
    assert "renamed" in {x["name"] for x in head["resources"]} and "renamed" not in {x["name"] for x in synced["resources"]}


@pytest.mark.parametrize(("item", "why"), [
    (_app("chart", "apps/helm", source_type="Helm"), "Helm"),
    (_app("remote", None, source_type="Helm", chart="nginx", repo_url="https://charts.example.com"), "Helm"),
    ({**_app("multi", "apps/plain"), "spec": {"sources": [{"repoURL": "https://github.com/acme/manifests",
                                                           "path": "apps/plain"}]}}, "여러 개"),
    (_app("gitlab", "apps/plain", repo_url="https://gitlab.com/acme/manifests"), "GitHub"),
])
def test_Helm_여러소스_GitHub_아닌_앱은_unsupported_이고_레포를_읽지_않는다(client, cluster, fake_server, gh, monkeypatch,
                                                                     item, why):
    _cluster_apps(monkeypatch, [item])
    body = _rendered(client, cluster, item["metadata"]["name"]).json()
    assert body["status"] == "unsupported" and why in body["message"] and body["resources"] == []
    assert gh.reads == [] and not [call for call in fake_server.calls if call[0] == "repo_token"]


def test_렌더할_앱의_레포가_연결_안_됐으면_409(client, fake_server, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    cluster = fake_server.add_cluster(owner="u-1")
    r = _rendered(client, cluster, "shop-web")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GITOPS_REPO_NOT_CONNECTED"


def test_앱_path_가_레포에_없으면_render_failed(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, [_app("gone", "apps/gone")])
    body = _rendered(client, cluster, "gone").json()
    assert body["status"] == "render_failed" and "apps/gone" in body["message"]


def test_렌더할_앱이_없으면_404_ARGOCD_APP_NOT_FOUND(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    r = _rendered(client, cluster, "nope")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "ARGOCD_APP_NOT_FOUND"


def test_ArgoCD_가_없으면_렌더도_absent(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch, fail='error: the server doesn\'t have a resource type "applications"')
    body = _rendered(client, cluster, "shop-web").json()
    assert (body["status"], body["resources"]) == ("absent", [])


def test_kustomize_덮어쓰기가_있으면_notes_로_알린다():
    item = _app("x", "apps/kustomize/overlays/prod", source_type="Kustomize", kustomize={"namePrefix": "p-"})
    source = browse.render_source(item)
    assert source.unsupported is None and len(source.notes) == 1 and "kustomize" in source.notes[0]


# ── 작은 것들 ──────────────────────────────────────────

@pytest.mark.parametrize(("path", "tail"), [
    ("apps/a b/#1?.yaml", "apps/a%20b/%231%3F.yaml"),
    ("apps/100%.yaml", "apps/100%25.yaml"),
    ("apps/한글.yaml", "apps/%ED%95%9C%EA%B8%80.yaml"),
])
def test_GitHub_링크는_경로_조각을_인코딩한다(path, tail):
    assert browse.html_url(REPO, "c0", path, folder=False) == f"https://github.com/{REPO}/blob/c0/{tail}"


def test_파일_응답의_링크도_인코딩된다(client, cluster, gh, monkeypatch):
    _cluster_apps(monkeypatch)
    gh.commits["c0"]["apps/my file#2.yaml"] = "a: b\n"
    body = _file(client, cluster, "apps/my file#2.yaml").json()
    assert body["html_url"].endswith("/blob/c0/apps/my%20file%232.yaml") and body["name"] == "my file#2.yaml"


@pytest.mark.parametrize(("raw", "clean"), [
    ("", ""), ("/", ""), ("/apps/plain/", "apps/plain"), ("apps/../x", None), ("./apps", None), ("a\\b", None),
    ("a/\x00", None),
])
def test_경로_정리(raw, clean):
    assert browse.clean_path(raw) == clean


@pytest.mark.parametrize(("raw", "clean"), [
    ("", ""), ("HEAD", ""), ("refs/heads/env/prod", "env/prod"), ("v1.2.0", "v1.2.0"), ("a..b", None), ("x y", None),
])
def test_ref_정리(raw, clean):
    assert browse.clean_ref(raw) == clean


def test_앱_경로는_점과_빗금을_떼고_여러_소스는_소스마다(monkeypatch):
    items = [
        _app("dot", "./apps/plain/"),
        _app("root", "."),
        {**_app("multi", None), "spec": {"sources": [
            {"repoURL": "git@github.com:acme/manifests.git", "path": "apps/a", "targetRevision": "main"},
            {"repoURL": "https://charts.example.com", "chart": "x"},
            {"repoURL": "https://github.com/acme/manifests", "ref": "values"},
        ]}, "status": {"sourceTypes": ["Kustomize", "Helm", "Directory"]}},
    ]
    found = browse.app_paths(items, REPO)
    assert [(a.name, a.path, a.source_type) for a in found] == [
        ("root", "", "Directory"), ("multi", "apps/a", "Kustomize"), ("dot", "apps/plain", "Directory")]


def test_LIST_COMMAND_을_그대로_쓴다(client, cluster, gh, monkeypatch):
    ran = _cluster_apps(monkeypatch)
    _tree(client, cluster)
    assert ran == [argocd.LIST_COMMAND]
