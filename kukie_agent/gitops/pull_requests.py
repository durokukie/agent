"""propose_git_change 의 몸통 — PR 만들기(승인 전) · 승인 순간 다시 보기 · 머지 · 닫기 (DURO-134).

Plan 상태를 바꾸는 일은 guardrail/gitops_hook.py 가 한다. 여기는 GitHub·렌더 비교로 "무엇을 할지" 만 정한다.

승인 전 (draft → open)
  1. targetRevision → 기본 브랜치(HEAD·빈 값) 또는 그 이름의 브랜치. 태그·커밋이면 PR 길이 없다
  2. 그 브랜치 끝 커밋의 레포를 받아 고칠 곳 찾기 + 렌더 비교 (kukie_agent/manifests)
  3. Secret 값이 든 diff 는 만들지 않는다 · 같은 파일을 고치는 Kukie PR 이 열려 있으면 기다린다 (Conflict Key)
  4. 새 브랜치 `kukie/<plan code>` 에 커밋 하나, 기본 브랜치로 PR

승인 순간 (recheck) — 승인한 것과 지금이 같아야 머지한다. 다르면 STALE
  PR 이 아직 열려 있고, base 브랜치 끝이 승인 카드를 만든 커밋(base_sha) 그대로이고, 바뀐 파일과 그 내용(blob id)이
  승인 카드의 것과 정확히 같아야 한다 — 그래야 머지 결과가 승인한 렌더 diff 와 같다
"""
from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anyio import to_thread

from kukie_agent import argocd, server_api
from kukie_agent.gitops import github
from kukie_agent.gitops.github import GitHubError, PullRequest
from kukie_agent.gitops.route import Route
from kukie_agent.kubectl import thread_limiter
from kukie_agent.manifests import Change, Refused, plan_change

logger = logging.getLogger(__name__)

#: 렌더 비교(kustomize)는 CPU·디스크를 쓴다 — dry-run 과 따로 묶는다
RENDER_WORKERS = 4
#: diff 에 이런 값이 더해지면 PR 을 만들지 않는다 — git 에 평문으로 영원히 남는다
_SECRET_VALUE = re.compile(
    r"(?:\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\b(?:Basic|Bearer)\s+[A-Za-z0-9+/=._-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|^\s*kind:\s*Secret\s*$)",
    re.IGNORECASE,
)
#: 승인 카드·PR 본문이 너무 길어지지 않게
DIFF_LIMIT = 20_000


class Refuse(Exception):
    """PR 을 만들 수 없다. kubectl=True 면 PR 로는 못 하는 일이라 kubectl 변경 툴(경고 카드)로 가라는 뜻이다."""

    def __init__(self, message: str, *, kubectl: bool = False, retry: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.kubectl = kubectl
        self.retry = retry


@dataclass
class Draft:
    repository: str
    base: str
    base_sha: str
    files: dict[str, str]                     # 레포 루트 기준 경로 → 고친 뒤 전체 내용
    file_diff: str
    render_diff: str
    warnings: list[str] = field(default_factory=list)
    merge_methods: tuple[str, ...] = ("squash",)

    def file_entries(self) -> list[dict[str, str]]:
        return [{"path": path, "blob_sha": github.blob_sha(content)} for path, content in sorted(self.files.items())]

    def conflict_keys(self) -> list[str]:
        """기획 07 Conflict Key 의 레포·파일 단위 키 — 같은 파일을 고치는 변경 둘이 겹치지 않게 (설계 4절)."""
        return [f"git:{self.repository}:{path}" for path in sorted(self.files)]


def _clip(text: str) -> str:
    return text if len(text) <= DIFF_LIMIT else text[:DIFF_LIMIT] + "\n… (너무 길어 잘랐어요)"


def secret_in(file_diff: str) -> bool:
    """diff 가 더하는 줄에 비밀 값처럼 보이는 것이 있나."""
    return any(
        _SECRET_VALUE.search(line[1:])
        for line in file_diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def base_branch(revision: str, default_branch: str) -> str:
    text = (revision or "").strip()
    if text in ("", "HEAD"):
        return default_branch
    return text.removeprefix("refs/heads/")


async def token_for(cluster_id: str | None, repository: str) -> server_api.RepoToken:
    """레포 범위 설치 토큰. 연결이 끊겼으면 Refuse(kubectl) — 판단과 토큰 발급 사이에 끊긴 경우다."""
    if cluster_id is None:
        raise Refuse("이 대화는 클러스터에 묶여 있지 않아 PR 을 만들 수 없어요.", kubectl=True)
    try:
        return await server_api.repo_token(cluster_id, repository)
    except server_api.ServerError as exc:
        if exc.code == "MANIFEST_REPO_NOT_CONNECTED":
            raise Refuse(f"레포 {repository} 의 Kukie 연결이 끊겼어요. 레포를 연결하면 PR 로 바꿀 수 있어요.",
                         kubectl=True) from None
        raise


async def draft(route: Route, change: Change, token: server_api.RepoToken, *, hpa: str | None) -> Draft:
    """고칠 곳을 찾아 고치고 렌더 비교로 확인한다. PR 은 아직 만들지 않는다."""
    assert route.repository and route.source and route.target
    async with github.connect(route.repository, token.token) as gh:
        info = await gh.info()
        base = base_branch(route.revision, info.default_branch)
        base_sha = await gh.branch_head(base)
        if base_sha is None:
            raise Refuse(f"ArgoCD 앱이 브랜치가 아닌 '{route.revision}'(태그·커밋)을 따라가요 — PR 을 머지해도 "
                         "반영되지 않아 PR 로 못 바꿔요.", kubectl=True)
        with tempfile.TemporaryDirectory(prefix="kukie-gitops-") as work:
            root = await gh.download(base_sha, Path(work) / "repo")
            result = await to_thread.run_sync(
                lambda: plan_change(root, route.source, route.target, change, hpa=hpa),
                limiter=thread_limiter("gitops_render", RENDER_WORKERS),
            )
        if isinstance(result, Refused):
            raise Refuse(result.message, kubectl=not result.retry, retry=result.retry)
        if secret_in(result.file_diff):
            raise Refuse("바뀔 내용에 비밀 값(키·토큰·Secret)이 보여 PR 을 만들지 않았어요 — git 에 평문으로 남아요. "
                         "Sealed Secrets 나 External Secrets 를 쓰세요.")
        busy = await _busy_files(gh, set(result.files))
        if busy:
            raise Refuse(f"같은 파일을 고치는 Kukie PR 이 이미 열려 있어요 ({busy}). 그 PR 이 머지되거나 닫힌 뒤에 다시 하세요 "
                         "(채팅에서 결정하거나 GitHub 에서 닫으면 돼요).")
    return Draft(repository=route.repository, base=base, base_sha=base_sha, files=result.files,
                 file_diff=_clip(result.file_diff), render_diff=_clip(result.render_diff),
                 warnings=list(result.warnings), merge_methods=info.merge_methods)


#: PR 본문에 남기는 계획 표시 — 열린 Kukie PR 의 계획이 이미 닫혔는지(결정 없이 만료) 알아보려고
_PLAN_MARK = re.compile(r"<!-- kukie-plan: ([0-9A-Za-z-]+) -->")


#: 그 PR 을 닫아도 되는 계획 상태 — 승인 없이 끝났다(만료·전제 바뀜·거절). 허용 목록이다: FAILED(브랜치 보호로 일부러
#: 남긴 PR)·UNKNOWN(머지 여부 모름)·APPLIED 등 나머지는 손대지 않는다 — 승인된 변경을 지우면 안 된다 (agent#126 리뷰)
_ABANDONED = {
    "EXPIRED": "승인 카드가 결정 없이 만료돼",
    "STALE": "승인 순간 전제가 바뀌어",
    "REJECTED": "채팅에서 거절돼",
}


async def _abandoned(pull: PullRequest) -> str | None:
    """그 PR 을 연 계획이 승인 없이 끝났으면 그 까닭. 표시가 없거나 못 읽으면 None — 닫지 않는다."""
    found = _PLAN_MARK.search(pull.body)
    if not found:
        return None
    try:
        plan = await server_api.get_plan(found.group(1))
    except Exception:
        return None
    if plan.decision is not None and plan.decision.get("approved"):
        return None
    return _ABANDONED.get(plan.status)


async def _busy_files(gh: github.GitHub, paths: set[str]) -> str:
    """같은 파일을 고치는 열린 Kukie PR. 계획이 결정 없이 닫힌 고아 PR 은 여기서 닫는다 — 아무도 안 닫으면 그 파일의
    PR 길이 영영 막힌다 (agent#126 리뷰. 카드가 결정 없이 만료되면 server 가 계획만 닫고 agent 는 통보받지 않는다)."""
    found = []
    for pull in await gh.open_kukie_pulls():
        if not paths & set(await gh.pull_files(pull.number)):
            continue
        why = await _abandoned(pull)
        if why is not None:
            try:
                await gh.close_pull(pull.number)        # 닫은 뒤에 "닫았어요" 를 단다 — 닫기가 실패하면 말도 안 남긴다
            except GitHubError:
                logger.warning("만료된 카드의 PR 을 닫지 못했다: %s #%s", gh.repository, pull.number)
            else:
                # 잠금을 푸는 것은 닫기 하나다 — 코멘트·브랜치 삭제가 실패해도 이 PR 때문에 막지 않는다 (agent#126 리뷰)
                try:
                    await gh.comment(pull.number, f"이 PR 의 {why} Kukie 가 닫았어요.")
                    await gh.delete_branch(pull.head_ref)
                except (GitHubError, ValueError):
                    logger.warning("닫은 PR 의 코멘트·브랜치 정리를 못 했다: %s #%s", gh.repository, pull.number)
                continue
        found.append(f"#{pull.number} {pull.url}")
    return ", ".join(found)


def pull_body(*, intent: str, expected_effects: list[str], side_effects: list[str], draft: Draft,
              application: dict[str, Any], plan_code: str, plan_id: str) -> str:
    effects = "\n".join(f"- {item}" for item in expected_effects)
    sides = "\n".join(f"- {item}" for item in side_effects)
    return (
        f"Kukie 채팅에서 제안한 변경이에요 (Action Plan `{plan_code}`).\n\n"
        f"**왜**: {intent}\n\n**예상 영향**\n{effects}\n\n**부작용**\n{sides}\n\n"
        f"**대상 ArgoCD 앱**: `{application.get('namespace')}/{application.get('name')}` "
        f"({'self-heal 켜짐' if application.get('self_heal') else '자동 sync' if application.get('auto_sync') else '수동 sync'})\n\n"
        "**클러스터에서 바뀔 것 (렌더 비교)**\n\n```diff\n" + draft.render_diff + "\n```\n\n"
        "채팅에서 승인하면 Kukie 가 이 PR 을 머지하고 ArgoCD sync 를 지켜봐요. 거절하면 닫혀요.\n\n"
        f"<!-- kukie-plan: {plan_id} -->"
    )


async def open_pull(draft: Draft, token: server_api.RepoToken, *, branch: str, title: str, body: str) -> PullRequest:
    async with github.connect(draft.repository, token.token) as gh:
        await gh.create_branch_commit(base_sha=draft.base_sha, branch=branch, files=draft.files, message=title)
        try:
            return await gh.open_pull(branch=branch, base=draft.base, title=title, body=body)
        except GitHubError:
            try:
                await gh.delete_branch(branch)
            except GitHubError:
                logger.warning("PR 을 못 열고 남은 브랜치를 지우지 못했다: %s %s", draft.repository, branch)
            raise


def application_summary(route: Route) -> dict[str, Any]:
    """승인 카드·Plan 의 대상 Application — argocd 판별 카드와 같은 칸 (이름·소스·sync 정책)."""
    assert route.application is not None
    return argocd._app_summary(route.application)


@dataclass
class Recheck:
    pull: PullRequest
    stale: str | None = None        # 승인한 것과 달라진 이유 — 있으면 머지하지 않는다(STALE)
    merged: bool = False            # 승인한 내용 그대로 GitHub 에서 이미 머지됐다 — 반영된 것으로 보고한다


async def recheck(gitops: dict[str, Any], pull_request: dict[str, Any], token: server_api.RepoToken) -> Recheck:
    """승인 순간 PR 을 다시 본다."""
    approved = {entry["path"]: entry["blob_sha"] for entry in gitops["files"]}
    async with github.connect(gitops["repository"], token.token) as gh:
        pull = await gh.pull(int(pull_request["number"]))
        if pull.merged:
            # 누가 GitHub 에서 먼저 머지했다. 승인한 내용 그대로, 승인 카드를 만든 base 위에 머지됐으면 변경은 이미 들어갔다 —
            # STALE("실행 안 함")은 사실과 다르다. 다른 커밋 위에 머지됐으면 렌더 결과가 다를 수 있어 반영으로 보지 않는다 (agent#126 리뷰)
            same = (pull.merge_commit and await gh.pull_files(pull.number) == approved
                    # 첫 부모만 본다 — squash·rebase 는 부모가 하나, 머지 커밋은 [base, PR head] 둘이다 (agent#126 리뷰)
                    and (await gh.commit_parents(pull.merge_commit))[:1] == [gitops["base_sha"]])
            if same:
                return Recheck(pull, merged=True)
            return Recheck(pull, f"PR #{pull.number} 이 승인 카드와 다른 내용·다른 base 로 GitHub 에서 이미 머지됐어요 — "
                                 "클러스터 상태를 직접 확인해야 해요.")
        if pull.state != "open":
            return Recheck(pull, f"PR #{pull.number} 이 닫혔어요.")
        if pull.base_ref != gitops["base"] or pull.head_ref != gitops["branch"]:
            return Recheck(pull, f"PR #{pull.number} 의 브랜치가 바뀌었어요.")
        # base 가 앞서면 PR 파일이 그대로여도 렌더 결과가 달라질 수 있다 (다른 파일 — kustomization.yaml 등 — 이 바뀌었다).
        # 승인한 렌더 diff 는 base_sha 기준이라, 새 base 로 다시 렌더 비교해 다시 제안하게 한다 (agent#126 리뷰)
        head = await gh.branch_head(gitops["base"])
        if head != gitops["base_sha"]:
            return Recheck(pull, f"승인 카드를 만든 뒤 {gitops['base']} 브랜치에 다른 커밋이 들어왔어요 — "
                                 "지금 레포로 렌더 비교를 다시 해야 해요.")
        if await gh.pull_files(pull.number) != approved:
            return Recheck(pull, f"PR #{pull.number} 의 내용이 승인 카드와 달라졌어요(누가 커밋을 더했어요).")
        return Recheck(pull)


async def merge(gitops: dict[str, Any], pull: PullRequest, token: server_api.RepoToken, *, title: str) -> str:
    async with github.connect(gitops["repository"], token.token) as gh:
        return await gh.merge(pull.number, head_sha=pull.head_sha,
                              methods=tuple(gitops.get("merge_methods") or ("squash", "merge", "rebase")), title=title)


async def tidy_after_merge(gitops: dict[str, Any], token: server_api.RepoToken) -> None:
    """머지한 브랜치를 지운다. 못 지워도 그만이다 (레포 설정의 자동 삭제가 이미 지웠을 수 있다)."""
    try:
        async with github.connect(gitops["repository"], token.token) as gh:
            await gh.delete_branch(gitops["branch"])
    except (GitHubError, ValueError):
        logger.warning("머지한 브랜치를 지우지 못했다: %s %s", gitops.get("repository"), gitops.get("branch"))


async def close(cluster_id: str | None, gitops: dict[str, Any] | None, pull_request: dict[str, Any] | None,
                why: str) -> bool:
    """PR 을 닫고 브랜치를 지운다(거절·STALE). 어떤 실패도 올리지 않는다 — 결정 기록이 먼저다. 닫았으면 True."""
    if not gitops or not pull_request or cluster_id is None:
        return False
    try:
        token = await server_api.repo_token(cluster_id, gitops["repository"])
        async with github.connect(gitops["repository"], token.token) as gh:
            await gh.comment(int(pull_request["number"]), why)
            await gh.close_pull(int(pull_request["number"]))
            await gh.delete_branch(gitops["branch"])
        return True
    except Exception:
        logger.exception("PR 을 닫지 못했다: %s #%s", gitops.get("repository"), pull_request.get("number"))
        return False
