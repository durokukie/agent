"""메모리 가짜 GitHub 와 ArgoCD 클러스터 — PR 길(DURO-134) 흐름 테스트가 같이 쓴다.

FakeGitHub 는 kukie_agent.gitops.github.GitHub 와 같은 메서드를 같은 모양으로 갖는다. 레포 내용은 tests/fixtures/manifests 다.
FakeCluster 는 route·watch 의 kubectl 을 대신한다 — 리소스 하나(ArgoCD 추적 표시가 붙은 Deployment web)와 Application 하나.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kukie_agent.gitops.github import BRANCH_PREFIX, GitHubError, PullRequest, RepoInfo, blob_sha
from kukie_agent.kubectl import KubectlResult

FIXTURE = Path(__file__).parent / "fixtures" / "manifests"
REPO = "acme/manifests"


def _files(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*")) if path.is_file()}


@dataclass
class FakeGitHub:
    repository: str = REPO
    commits: dict[str, dict[str, str]] = field(default_factory=dict)   # sha → 파일 전체
    branches: dict[str, str] = field(default_factory=dict)
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)
    merges: list[dict[str, Any]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    merge_methods: tuple[str, ...] = ("squash", "merge")
    #: 다음 머지에 낼 오류 (status, message) — 브랜치 보호 등
    merge_error: tuple[int, str] | None = None
    mergeable_state: str = "clean"

    def __post_init__(self) -> None:
        self.commits["c0"] = _files(FIXTURE)
        self.branches["main"] = "c0"

    # ── 테스트가 쓰는 도구 ───────────────────────────────

    def pull_of(self, number: int = 1) -> dict[str, Any]:
        return self.pulls[number]

    def push_to_pull(self, number: int, path: str, content: str) -> None:
        """누군가 PR 브랜치에 커밋을 더했다."""
        pull = self.pulls[number]
        files = dict(self.commits[pull["head_sha"]])
        files[path] = content
        sha = f"c{len(self.commits)}"
        self.commits[sha] = files
        self.branches[pull["head_ref"]] = sha
        pull["head_sha"] = sha

    def push_to_base(self, path: str, content: str, branch: str = "main") -> None:
        """누군가 기본 브랜치에 다른 커밋을 넣었다."""
        files = {**self.commits[self.branches[branch]], path: content}
        sha = f"c{len(self.commits)}"
        self.commits[sha] = files
        self.branches[branch] = sha

    def connect(self, repository: str, token: str) -> "FakeGitHub":
        assert repository == self.repository, repository
        self.tokens.append(token)
        return self

    # ── GitHub 와 같은 메서드 ─────────────────────────────

    async def __aenter__(self) -> "FakeGitHub":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def info(self) -> RepoInfo:
        return RepoInfo(default_branch="main", merge_methods=self.merge_methods)

    async def branch_head(self, branch: str) -> str | None:
        return self.branches.get(branch)

    async def download(self, ref: str, dest: Path) -> Path:
        root = dest / f"acme-manifests-{ref}"
        for path, content in self.commits[ref].items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return root

    def _pull(self, number: int) -> PullRequest:
        p = self.pulls[number]
        return PullRequest(number=number, url=f"https://github.com/{self.repository}/pull/{number}", state=p["state"],
                           merged=p["merged"], head_ref=p["head_ref"], head_sha=p["head_sha"], base_ref=p["base"],
                           mergeable_state=self.mergeable_state)

    async def pull(self, number: int) -> PullRequest:
        return self._pull(number)

    async def pull_files(self, number: int) -> dict[str, str]:
        p = self.pulls[number]
        base, head = self.commits[p["base_sha"]], self.commits[p["head_sha"]]
        return {path: blob_sha(content) for path, content in head.items() if base.get(path) != content}

    async def open_kukie_pulls(self) -> list[PullRequest]:
        return [self._pull(n) for n, p in self.pulls.items() if p["state"] == "open"
                and p["head_ref"].startswith(BRANCH_PREFIX)]

    async def create_branch_commit(self, *, base_sha: str, branch: str, files: dict[str, str], message: str) -> str:
        assert branch.startswith(BRANCH_PREFIX) and branch not in self.branches, branch
        merged = {**self.commits[base_sha], **files}
        sha = hashlib.sha1(json.dumps(merged, sort_keys=True).encode()).hexdigest()
        self.commits[sha] = merged
        self.branches[branch] = sha
        return sha

    async def open_pull(self, *, branch: str, base: str, title: str, body: str) -> PullRequest:
        number = len(self.pulls) + 1
        self.pulls[number] = {"state": "open", "merged": False, "head_ref": branch, "head_sha": self.branches[branch],
                              "base": base, "base_sha": self.branches[base], "title": title, "body": body}
        return self._pull(number)

    async def merge(self, number: int, *, head_sha: str, methods: tuple[str, ...], title: str) -> str:
        if self.merge_error is not None:
            raise GitHubError(*self.merge_error)
        p = self.pulls[number]
        if p["head_sha"] != head_sha:
            raise GitHubError(409, "Head branch was modified. Review and try the merge again.")
        sha = f"merge{number}{'0' * 34}"
        self.commits[sha] = self.commits[head_sha]
        self.branches[p["base"]] = sha
        p.update(state="closed", merged=True)
        self.merges.append({"number": number, "head_sha": head_sha, "method": methods[0], "title": title, "sha": sha})
        return sha

    async def close_pull(self, number: int) -> None:
        self.pulls[number]["state"] = "closed"

    async def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def delete_branch(self, branch: str) -> None:
        assert branch.startswith(BRANCH_PREFIX), branch
        self.branches.pop(branch, None)


def _ok(data: Any) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout=json.dumps(data), stderr="", success=True, exit_code=0)


def _missing(what: str) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout="", success=False, exit_code=1,
                         stderr=f'Error from server (NotFound): {what} not found')


@dataclass
class FakeCluster:
    """route·watch 가 보는 클러스터. Deployment shop/web 하나를 ArgoCD 앱 shop-web(argocd ns)이 관리한다."""

    managed: bool = True
    repo_url: str = "https://github.com/Acme/Manifests.git"
    source_type: str = "Directory"
    path: str = "apps/plain"
    revision: str = "HEAD"
    self_heal: bool = True
    auto_sync: bool = True
    hpa: str | None = None
    #: watch 가 볼 Application 상태 — None 이면 머지 커밋으로 Synced + Healthy
    app_status: dict[str, Any] | None = None
    seen: list[list[str]] = field(default_factory=list)
    merged_revision: str = ""

    def resource(self) -> dict[str, Any]:
        annotations = {"argocd.argoproj.io/tracking-id": "shop-web:apps/Deployment:shop/web"} if self.managed else {}
        return {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "web", "namespace": "shop", "annotations": annotations}}

    def application(self) -> dict[str, Any]:
        policy: dict[str, Any] = {}
        if self.auto_sync:
            policy["automated"] = {"selfHeal": self.self_heal, "prune": False}
        status = copy.deepcopy(self.app_status) if self.app_status is not None else {
            "sync": {"status": "Synced", "revision": self.merged_revision},
            "health": {"status": "Healthy"},
        }
        status["sourceType"] = self.source_type
        return {
            "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": "shop-web", "namespace": "argocd"},
            "spec": {"project": "default", "destination": {"namespace": "shop"}, "syncPolicy": policy,
                     "source": {"repoURL": self.repo_url, "path": self.path, "targetRevision": self.revision}},
            "status": status,
        }

    def run(self, args: list[str], **kwargs: Any) -> KubectlResult:
        self.seen.append(list(args))
        if args[:2] == ["get", "applications.argoproj.io"]:
            if "-A" in args:
                return _ok({"items": [self.application()]})
            return _ok(self.application())
        if args[:2] == ["get", "horizontalpodautoscalers"]:
            items = [{"metadata": {"name": self.hpa}, "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "web"}}}]
            return _ok({"items": items if self.hpa else []})
        if args[:3] in (["get", "deployment", "web"], ["get", "Deployment", "web"]):
            return _ok(self.resource())
        return _missing(" ".join(args[1:3]))
