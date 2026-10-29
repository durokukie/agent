"""GitHub REST 클라이언트 — 매니페스트 레포를 받아 오고, 브랜치·커밋·PR 을 만들고, 머지하거나 닫는다 (DURO-134).

컨테이너에 git 이 없다 — 파일은 tarball 로 받고, 커밋은 Git Data API(트리·커밋·ref)로 만든다.
토큰은 kukie-server 가 준 **그 레포 하나**로 좁힌 1시간 설치 토큰이다 (server_api.repo_token). 로그·예외 문구에 싣지 않는다 —
예외에는 HTTP 상태와 GitHub 가 준 message 만 남긴다.

지키는 것
  - **기본 브랜치에 직접 쓰지 않는다.** 새 ref 는 `refs/heads/kukie/…` 만 만들고, 있는 ref 를 옮기는 호출(PATCH git/refs)은 없다.
    바뀐 내용은 PR 머지로만 들어간다 — 브랜치 보호가 있으면 GitHub 가 거기서 막는다(우회하지 않는다)
  - 머지는 승인한 head 커밋으로만 한다 (merge 의 sha) — 승인 뒤 누가 PR 브랜치에 커밋을 더하면 GitHub 가 409 로 거절한다
"""
from __future__ import annotations

import hashlib
import io
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = 30.0
#: Kukie 가 만드는 브랜치의 접두어. 이 밖의 ref 는 만들지도 지우지도 않는다
BRANCH_PREFIX = "kukie/"
#: 받아 올 레포 크기 상한 — 매니페스트 레포는 작다. 넘으면 PR 길을 포기한다(렌더 비교를 못 한다)
MAX_TARBALL_BYTES = 50 * 1024 * 1024
MAX_FILES = 20_000

#: 테스트가 httpx.MockTransport 로 바꿔 끼운다. None 이면 진짜 네트워크
transport: httpx.AsyncBaseTransport | None = None


class GitHubError(Exception):
    """GitHub 가 거절했거나 닿지 못했다. status 는 HTTP 상태(닿지 못했으면 None)."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"GitHub {status or '연결 실패'}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class RepoInfo:
    default_branch: str
    merge_methods: tuple[str, ...]      # 레포가 허용하는 머지 방식, 쓸 순서대로 (squash → merge → rebase)


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str                    # 사람이 여는 주소 (html_url)
    state: str                  # open | closed
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    mergeable_state: str        # clean | blocked(브랜치 보호) | dirty(충돌) | behind | unstable | unknown …

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PullRequest":
        head, base = data.get("head") or {}, data.get("base") or {}
        return cls(
            number=int(data["number"]), url=str(data.get("html_url") or ""), state=str(data.get("state") or ""),
            merged=bool(data.get("merged")), head_ref=str(head.get("ref") or ""), head_sha=str(head.get("sha") or ""),
            base_ref=str(base.get("ref") or ""), mergeable_state=str(data.get("mergeable_state") or "unknown"),
        )


def blob_sha(content: str) -> str:
    """git 이 그 내용에 붙일 blob id — PR 의 파일이 승인한 내용과 같은지 GitHub 에 다시 묻지 않고 맞춰 본다."""
    data = content.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _safe_branch(branch: str) -> str:
    if not branch.startswith(BRANCH_PREFIX) or ".." in branch or branch.endswith((".lock", "/")):
        raise ValueError(f"Kukie 가 쓰는 브랜치가 아니다: {branch}")
    return branch


class GitHub:
    """레포 하나에 대한 호출들. `async with connect(repo, token) as gh:` 로 쓴다."""

    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self._client = httpx.AsyncClient(
            base_url=API, timeout=TIMEOUT, transport=transport, follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "kukie-agent",
            },
        )

    async def __aenter__(self) -> "GitHub":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    # ── 공통 ──────────────────────────────────────────────

    def _repo_path(self, *segments: str) -> str:
        owner, name = self.repository.split("/", 1)
        tail = "/".join(segments)
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}" + (f"/{tail}" if tail else "")

    async def _request(self, method: str, path: str, *, body: Any = None, params: dict[str, Any] | None = None,
                       not_found_is_none: bool = False) -> Any:
        try:
            response = await self._client.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(None, type(exc).__name__) from exc
        if response.status_code == 404 and not_found_is_none:
            return None
        if response.is_success:
            return response.json() if response.content else None
        try:
            message = str((response.json() or {}).get("message") or "")
        except ValueError:
            message = ""
        raise GitHubError(response.status_code, message or response.reason_phrase)

    # ── 읽기 ──────────────────────────────────────────────

    async def info(self) -> RepoInfo:
        data = await self._request("GET", self._repo_path())
        methods = tuple(
            method for method, key in (("squash", "allow_squash_merge"), ("merge", "allow_merge_commit"),
                                       ("rebase", "allow_rebase_merge"))
            if data.get(key, True)
        )
        return RepoInfo(default_branch=str(data.get("default_branch") or "main"), merge_methods=methods or ("merge",))

    async def branch_head(self, branch: str) -> str | None:
        """브랜치 끝 커밋. 그런 브랜치가 없으면(태그·커밋 해시를 targetRevision 으로 쓴 앱) None."""
        data = await self._request("GET", self._repo_path("branches", quote(branch, safe="")), not_found_is_none=True)
        return str(data["commit"]["sha"]) if data else None

    async def download(self, ref: str, dest: Path) -> Path:
        """그 커밋의 레포 내용을 dest 아래에 푼다. 레포 루트 폴더를 돌려준다."""
        try:
            response = await self._client.get(self._repo_path("tarball", quote(ref, safe="")))
        except httpx.HTTPError as exc:
            raise GitHubError(None, type(exc).__name__) from exc
        if not response.is_success:
            raise GitHubError(response.status_code, "레포 내용을 받지 못했다")
        if len(response.content) > MAX_TARBALL_BYTES:
            raise GitHubError(None, "레포가 너무 크다")
        return _extract(response.content, dest)

    async def pull(self, number: int) -> PullRequest:
        return PullRequest.from_json(await self._request("GET", self._repo_path("pulls", str(number))))

    async def pull_files(self, number: int) -> dict[str, str]:
        """PR 이 바꾼 파일 → 바뀐 뒤의 blob id. 지운 파일은 빈 문자열."""
        files: dict[str, str] = {}
        for page in range(1, 31):                          # 한 쪽 100개, GitHub 는 3000개까지만 준다
            data = await self._request("GET", self._repo_path("pulls", str(number), "files"),
                                       params={"per_page": 100, "page": page})
            for item in data or []:
                files[str(item["filename"])] = "" if item.get("status") == "removed" else str(item.get("sha") or "")
            if not data or len(data) < 100:
                break
        return files

    async def open_kukie_pulls(self) -> list[PullRequest]:
        """Kukie 가 만든 열린 PR 들 — 같은 파일을 고치는 PR 이 둘 생기지 않게 본다 (Conflict Key)."""
        found: list[PullRequest] = []
        for page in range(1, 11):
            data = await self._request("GET", self._repo_path("pulls"),
                                       params={"state": "open", "per_page": 100, "page": page})
            found += [PullRequest.from_json(item) for item in data or []
                      if str((item.get("head") or {}).get("ref") or "").startswith(BRANCH_PREFIX)]
            if not data or len(data) < 100:
                break
        return found

    # ── 쓰기 ──────────────────────────────────────────────

    async def create_branch_commit(self, *, base_sha: str, branch: str, files: dict[str, str], message: str) -> str:
        """base_sha 위에 files 를 고친 커밋 하나를 만들고 새 브랜치 branch 로 가리킨다. 커밋 id 를 돌려준다."""
        _safe_branch(branch)
        base = await self._request("GET", self._repo_path("git", "commits", base_sha))
        tree = await self._request("POST", self._repo_path("git", "trees"), body={
            "base_tree": base["tree"]["sha"],
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}
                     for path, content in sorted(files.items())],
        })
        commit = await self._request("POST", self._repo_path("git", "commits"), body={
            "message": message, "tree": tree["sha"], "parents": [base_sha],
        })
        await self._request("POST", self._repo_path("git", "refs"), body={
            "ref": f"refs/heads/{branch}", "sha": commit["sha"],
        })
        return str(commit["sha"])

    async def open_pull(self, *, branch: str, base: str, title: str, body: str) -> PullRequest:
        data = await self._request("POST", self._repo_path("pulls"), body={
            "title": title, "head": _safe_branch(branch), "base": base, "body": body, "maintainer_can_modify": False,
        })
        return PullRequest.from_json(data)

    async def merge(self, number: int, *, head_sha: str, methods: tuple[str, ...], title: str) -> str:
        """승인한 head 커밋으로 머지한다. 머지 커밋 id 를 돌려준다.

        405 = 머지할 수 없다(브랜치 보호의 필수 리뷰·검사, 충돌) — 부르는 쪽이 PR 을 남기고 안내한다.
        409 = head 가 승인한 커밋이 아니다. 레포가 막은 방식(405 "… not allowed")이면 다음 방식으로 해 본다.
        """
        last: GitHubError | None = None
        for method in methods:
            try:
                data = await self._request("PUT", self._repo_path("pulls", str(number), "merge"), body={
                    "sha": head_sha, "merge_method": method, "commit_title": title,
                })
            except GitHubError as exc:
                if exc.status in (405, 422) and "not allowed" in exc.message.lower():
                    last = exc
                    continue
                raise
            return str(data["sha"])
        raise last or GitHubError(405, "허용된 머지 방식이 없다")

    async def close_pull(self, number: int) -> None:
        await self._request("PATCH", self._repo_path("pulls", str(number)), body={"state": "closed"})

    async def comment(self, number: int, body: str) -> None:
        await self._request("POST", self._repo_path("issues", str(number), "comments"), body={"body": body})

    async def delete_branch(self, branch: str) -> None:
        """Kukie 브랜치만 지운다. 이미 없으면(GitHub 의 머지 뒤 자동 삭제) 조용히 끝난다."""
        await self._request("DELETE", self._repo_path("git", "refs", "heads", _safe_branch(branch)),
                            not_found_is_none=True)


def connect(repository: str, token: str) -> GitHub:
    """`async with github.connect(repo, token) as gh:` — 테스트가 이 함수를 가짜로 바꿔 끼운다."""
    return GitHub(repository, token)


def _extract(data: bytes, dest: Path) -> Path:
    """GitHub tarball(`<owner>-<repo>-<sha>/…`) 을 푼다. 파일과 폴더만, 레포 밖을 가리키는 경로·링크는 버린다."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = [m for m in archive.getmembers() if m.isfile() or m.isdir()]
            if len(members) > MAX_FILES:
                raise GitHubError(None, "레포 파일이 너무 많다")
            archive.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise GitHubError(None, f"레포 내용을 풀지 못했다: {type(exc).__name__}") from exc
    roots = [path for path in dest.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise GitHubError(None, "레포 내용 모양이 아니다")
    return roots[0]
