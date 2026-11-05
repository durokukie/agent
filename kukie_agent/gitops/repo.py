"""GitHub 레포 주소 정규화 — ArgoCD repoURL 과 server 에 연결된 레포를 맞춰 볼 때 쓴다.

kukie-server 의 RepoName.parse 가 받는 주소 모양을 받는다: `https://github.com/o/r(.git)`, `git@github.com:o/r.git`,
`ssh://git@github.com(:22)/o/r.git`, `github.com/o/r`, 브라우저 주소(`…/tree/main/…`). 결과는 소문자 `owner/repo`.
server 와 달리 `o/r` 만 쓴 것은 받지 않는다 — ArgoCD repoURL 은 늘 주소라서, 짧은 이름은 다른 저장소일 수 있다.
GitHub 가 아니면(GitLab·자체 호스팅·Helm 차트 저장소) None — PR 길이 없다.
"""
from __future__ import annotations

import re

_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")          # GitHub 계정 이름
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_FORMS = (
    re.compile(r"^(?:https?://)?(?:www\.)?github\.com/(?P<path>.+)$", re.I),
    re.compile(r"^(?:ssh://)?git@github\.com[:/](?:\d+/)?(?P<path>.+)$", re.I),
    re.compile(r"^ssh://git@github\.com(?::\d+)?/(?P<path>.+)$", re.I),
)


def repo_name(url: str | None) -> str | None:
    """repoURL → `owner/repo` (소문자). GitHub 레포가 아니면 None."""
    if not isinstance(url, str):
        return None
    text = url.strip()
    for form in _FORMS:
        found = form.match(text)
        if found:
            parts = found.group("path").split("/")
            break
    else:
        return None
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].split("?")[0].split("#")[0]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not _NAME.match(owner) or not _REPO.match(repo) or repo in (".", ".."):
        return None
    return f"{owner}/{repo}".lower()
