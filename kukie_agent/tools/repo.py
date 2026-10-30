"""매니페스트 레포 읽기 툴 3종 — 채팅에서 연결된 레포의 폴더·파일·ArgoCD 앱 렌더 결과를 본다 (DURO-151).

읽기 전용이라 훅도 승인 카드도 없다(SAFE) — Private·Shared 대화 모두에 연다. 읽기는 레포 탭 API(DURO-148)와 같은
gitops/browse.py 를 그대로 탄다: Secret·stringData·sops·비밀 키 가림, 크기 상한, 렌더의 네트워크 차단·이 컴퓨터 안 주소 검사·
시간 상한·kustomize stderr 빼기. 여기서 하는 일은 둘뿐이다 — 대상을 대화의 클러스터에 연결된 레포로 좁히고, 결과를 모델 문맥에
넣을 만큼 자른다(MAX_LINES·MAX_CHARS).

툴 목록은 레포 연결 상태와 상관없이 늘 같다 (프롬프트 캐시, DURO-135) — 연결된 레포가 없으면 툴이 그렇다고 답한다.
레포 토큰은 server 가 주는 그 레포 범위 설치 토큰이다 — 결과·로그에 싣지 않는다.
결과는 KubectlResult 모양이다 — 화면 블록(response.collect_steps)이 읽기 블록으로 그린다. command 는 표시용 문자열이고
실제로 돈 kubectl 이 아니다 (GitOpsResult 와 같다).
"""
from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any

from anyio import to_thread
from pydantic_ai import RunContext

from kukie_agent import argocd, server_api
from kukie_agent.deps import Deps
from kukie_agent.gitops import browse, github
from kukie_agent.gitops.repo import repo_name
from kukie_agent.kubectl import KubectlResult, run_kubectl, thread_limiter

logger = logging.getLogger(__name__)

#: 모델에 넣는 결과 상한 — 줄 수·글자 수. 넘으면 앞부분 + "N줄 더 있음"
MAX_LINES = 300
MAX_CHARS = 24_000
#: 한 줄 상한 — 한 줄짜리 큰 JSON·base64 가 상한을 혼자 다 쓰지 않게
MAX_LINE_CHARS = 2_000
#: ArgoCD 앱 목록 조회 시간(초)·동시 수
APPS_TIMEOUT = 10
APPS_WORKERS = 4

NOT_CONNECTED = "이 클러스터에 연결된 매니페스트 레포가 없어요. 팀 설정 → 클러스터에서 레포를 연결하면 볼 수 있어요."


class _Stop(Exception):
    """더 읽지 않고 모델에게 까닭을 돌려준다 — 툴 결과(success=False)의 stderr 가 된다."""


# ── 결과 모양 ─────────────────────────────────────────────

def clip(text: str, *, more: str = "") -> str:
    """모델 문맥에 넣을 만큼 — 앞에서 MAX_LINES 줄·MAX_CHARS 자까지, 넘으면 "… N줄 더 있음" 한 줄. more 는 그 뒤에 붙일 안내."""
    lines = [line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " …(줄이 길어 잘림)"
             for line in text.splitlines()]
    kept: list[str] = []
    used = 0
    for line in lines:
        if len(kept) >= MAX_LINES or used + len(line) + 1 > MAX_CHARS:
            break
        kept.append(line)
        used += len(line) + 1
    rest = len(lines) - len(kept)
    if rest:
        kept.append(f"… {rest}줄 더 있음{' — ' + more if more else ''}")
    return "\n".join(kept)


def _done(command: str, stdout: str) -> KubectlResult:
    return KubectlResult(command=command, stdout=stdout, stderr="", success=True, exit_code=0)


def _stopped(command: str, why: str) -> KubectlResult:
    return KubectlResult(command=command, stdout="", stderr=why, success=False, exit_code=1)


def _short(sha: str) -> str:
    return sha[:7]


def _size(size: int | None) -> str:
    if size is None:
        return ""
    return f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB"


_RELATION = {"here": "이 경로가 앱 path", "above": "앱 path 안에 있음", "below": "앱 path 가 이 아래"}


def _app_lines(badges: list[dict[str, Any]], apps_status: str) -> list[str]:
    if apps_status != "ok":
        why = {"absent": "이 클러스터에 ArgoCD 가 없어요", "forbidden": "ArgoCD 앱을 읽을 권한이 없어요"}
        return [f"이 경로를 쓰는 ArgoCD 앱: 모름 ({why.get(apps_status, 'ArgoCD 앱 목록을 읽지 못했어요')})"]
    if not badges:
        return ["이 경로를 쓰는 ArgoCD 앱: 없음"]
    lines = ["이 경로를 쓰는 ArgoCD 앱:"]
    for app in badges:
        revision = "" if app["same_revision"] else f" · 이 앱은 다른 revision({app['target_revision'] or 'HEAD'})을 따라가요"
        lines.append(f"  - {app['namespace']}/{app['name']} — path {app['path'] or '/'} · {app['source_type'] or '형식 모름'}"
                     f" · {_RELATION.get(app['relation'], app['relation'])}{revision}")
    return lines


# ── 대상 정하기 ───────────────────────────────────────────

async def _connected(deps: Deps) -> list[str]:
    """대화의 클러스터에 연결된 레포들 (소문자 owner/repo). 없으면 _Stop(NOT_CONNECTED)."""
    if deps.cluster_id is None:
        raise _Stop(NOT_CONNECTED)              # flat 엔드포인트 — 클러스터 등록·레포 연결이 없다
    try:
        repos = sorted({repo.repository for repo in await server_api.manifest_repos(deps.cluster_id)})
    except Exception:
        logger.exception("연결된 레포 목록을 읽지 못했다: cluster=%s", deps.cluster_id)
        raise _Stop("레포 연결 상태를 확인하지 못했어요. 잠시 뒤 다시 해 주세요.") from None
    if not repos:
        raise _Stop(NOT_CONNECTED)
    return repos


def _pick(repos: list[str], wanted: str | None) -> str:
    """읽을 레포 — 준 이름이 연결된 레포여야 한다. 안 주면 연결된 레포가 하나일 때 그것."""
    if wanted and wanted.strip():
        text = wanted.strip()
        name = repo_name(text) or repo_name(f"https://github.com/{text.strip('/')}")
        if name not in repos:
            raise _Stop(f"레포 {text} 는 이 클러스터에 연결된 레포가 아니에요 — 연결된 레포만 읽어요. "
                        f"연결된 레포: {', '.join(repos)}")
        return name
    if len(repos) == 1:
        return repos[0]
    raise _Stop(f"이 클러스터에 연결된 레포가 여럿이에요. repository 로 하나를 골라 주세요: {', '.join(repos)}")


async def _token(cluster_id: str, repository: str) -> str:
    """그 레포의 설치 토큰. server 가 연결된 레포인지 한 번 더 본다."""
    try:
        return (await server_api.repo_token(cluster_id, repository)).token
    except server_api.ServerError as exc:
        if exc.code == "MANIFEST_REPO_NOT_CONNECTED":
            raise _Stop(NOT_CONNECTED) from None
        raise _Stop(f"레포 {repository} 를 읽을 토큰을 받지 못했어요 — {exc.message}") from None
    except Exception:
        logger.exception("레포 토큰을 받지 못했다: cluster=%s repository=%s", cluster_id, repository)
        raise _Stop(f"레포 {repository} 를 읽을 토큰을 받지 못했어요. 잠시 뒤 다시 해 주세요.") from None


def _list_apps(context: str, kubeconfig: Path | None) -> tuple[str, list[dict[str, Any]]]:
    """클러스터의 ArgoCD Application 들 — (ok·absent·forbidden·error, items). 블로킹 — 스레드에서 돈다."""
    result = run_kubectl(argocd.LIST_COMMAND, context=context, kubeconfig=kubeconfig, timeout=APPS_TIMEOUT)
    if not result.success:
        return argocd.failure_kind(result.stderr or result.stdout), []
    try:
        return "ok", argocd.parse_applications(result.stdout)
    except ValueError:
        return "error", []


async def _apps(deps: Deps) -> tuple[str, list[dict[str, Any]]]:
    return await to_thread.run_sync(partial(_list_apps, deps.context, deps.kubeconfig),
                                    limiter=thread_limiter("repo_tools", APPS_WORKERS))


def _default_ref(apps: list[browse.AppPath], folder: str) -> str:
    """ref 를 안 줬을 때 — 그 경로를 쓰는 앱들이 한 revision(targetRevision)을 따라가면 그것, 아니면 기본 브랜치("")."""
    found = {browse.clean_ref(app.target_revision) for app in apps
             if app.path == folder or app.path == "" or folder.startswith(app.path + "/")}
    if len(found) != 1 or None in found:
        return ""
    return found.pop() or ""


def _ref(ref: str | None) -> str | None:
    """툴 인자의 ref — 안 줬으면 None, 줬으면 browse.clean_ref 로 본다."""
    if ref is None or not ref.strip():
        return None
    cleaned = browse.clean_ref(ref)
    if cleaned is None:
        raise _Stop(f"브랜치·태그·커밋 이름이 아니에요: {ref}")
    return cleaned


def _path(path: str | None) -> str:
    cleaned = browse.clean_path(path)
    if cleaned is None:
        raise _Stop(f"레포 안 경로가 아니에요: {path}")
    return cleaned


async def _read(read, repository: str):
    """browse 읽기의 실패를 모델에게 돌려줄 까닭으로."""
    try:
        return await read
    except browse.NotFound as exc:
        raise _Stop(exc.message) from None
    except github.GitHubError as exc:
        logger.warning("레포 읽기 툴의 GitHub 호출이 실패했다: %s", exc)
        raise _Stop(f"GitHub 에서 레포 {repository} 를 읽지 못했어요 — {exc}") from None


# ── 툴 ────────────────────────────────────────────────────

async def list_repo_dir(ctx: RunContext[Deps], path: str = "", ref: str | None = None,
                        repository: str | None = None) -> KubectlResult:
    """이 클러스터에 연결된 매니페스트 레포(ArgoCD 가 보는 GitHub 레포)의 폴더 하나를 본다 — 항목과 그 경로를 쓰는 ArgoCD 앱.

    레포 구조를 모를 때, 어느 폴더가 어느 앱인지·overlay 가 어떻게 나뉘었는지 볼 때 먼저 쓴다. path 는 레포 루트 기준(빈 값이면 루트).
    ref(브랜치·태그·커밋)를 안 주면 그 경로를 쓰는 ArgoCD 앱의 targetRevision, 없으면 기본 브랜치를 본다.
    연결된 레포가 여럿일 때만 repository(owner/repo)를 준다. 연결된 레포 밖은 읽지 않는다.
    """
    command = f"repo tree {repository or ''}:{path or '/'}"
    try:
        repos = await _connected(ctx.deps)
        name = _pick(repos, repository)
        folder = _path(path)
        wanted = _ref(ref)
        command = f"repo tree {name}:{folder or '/'}"
        token = await _token(ctx.deps.cluster_id or "", name)
        apps_status, items = await _apps(ctx.deps)
        apps = browse.app_paths(items, name)
        if wanted is None:
            wanted = _default_ref(apps, folder)
        revision, listing = await _read(browse.open_folder(name, token, wanted, folder), name)
    except _Stop as stop:
        return _stopped(command, str(stop))
    command = f"repo tree {name}:{folder or '/'}@{revision.ref}"
    entries = []
    for entry in listing["entries"]:
        if entry["type"] == "dir":
            used = browse.apps_for_folder(apps, entry["path"], revision)
            badge = f"  ← ArgoCD 앱 {', '.join(app['name'] for app in used)}" if used else ""
            entries.append(f"[폴더] {entry['name']}/{badge}")
        else:
            size = _size(entry["size"])
            entries.append(f"[{'파일' if entry['type'] == 'file' else entry['type']}] {entry['name']}"
                           f"{f' ({size})' if size else ''}")
    if listing["truncated"]:
        entries.append(f"… 항목이 많아 {len(listing['entries'])}개만 실었어요 (전체 {listing['total']}개)")
    url = browse.html_url(name, revision.sha, folder, folder=True)
    head = [f"레포 {name} · ref {revision.ref} (커밋 {_short(revision.sha)}) · 폴더 {folder or '/'}",
            *_app_lines(browse.apps_around(apps, folder, revision, is_file=False), apps_status),
            f"항목 {listing['total']}개:"]
    body = clip("\n".join(entries) or "(빈 폴더)", more=f"전체는 GitHub 에서: {url}")
    return _done(command, "\n".join([*head, body, f"GitHub: {url}"]))


async def read_repo_file(ctx: RunContext[Deps], path: str, ref: str | None = None,
                         repository: str | None = None) -> KubectlResult:
    """이 클러스터에 연결된 매니페스트 레포의 파일 하나를 읽는다 — 실제 내용과 그 파일을 쓰는 ArgoCD 앱.

    "이 파일이 뭐 하는 거야?"(values.yaml·kustomization.yaml·Deployment 등), overlay 끼리 무엇이 다른지, OutOfSync 의 원인을
    git 쪽에서 찾을 때, PR 로 바꾸기 전에 지금 파일을 확인할 때 쓴다. 짐작하지 말고 이 툴로 읽은 내용으로 답한다.
    Secret·sops·비밀 키처럼 보이는 파일은 내용 대신 가린 까닭만, 큰 파일은 앞부분만 준다.
    ref·repository 는 list_repo_dir 과 같다.
    """
    command = f"repo file {repository or ''}:{path}"
    try:
        repos = await _connected(ctx.deps)
        name = _pick(repos, repository)
        file_path = _path(path)
        if not file_path:
            raise _Stop("파일 경로를 주세요. 폴더는 list_repo_dir 로 봐요.")
        wanted = _ref(ref)
        command = f"repo file {name}:{file_path}"
        token = await _token(ctx.deps.cluster_id or "", name)
        apps_status, items = await _apps(ctx.deps)
        apps = browse.app_paths(items, name)
        if wanted is None:
            wanted = _default_ref(apps, file_path.rsplit("/", 1)[0] if "/" in file_path else "")
        revision, view = await _read(browse.open_file(name, token, wanted, file_path), name)
    except _Stop as stop:
        return _stopped(command, str(stop))
    command = f"repo file {name}:{file_path}@{revision.ref}"
    url = browse.html_url(name, revision.sha, file_path, folder=False)
    head = [f"레포 {name} · ref {revision.ref} (커밋 {_short(revision.sha)}) · 파일 {file_path}"
            f"{f' ({_size(view['size'])})' if view['size'] is not None else ''}",
            *_app_lines(browse.apps_around(apps, file_path, revision, is_file=True), apps_status)]
    if view["objects"]:
        head.append("쿠버네티스 객체: " + ", ".join(
            f"{obj['kind']} {obj['namespace'] + '/' if obj['namespace'] else ''}{obj['name'] or '(이름 없음)'}"
            for obj in view["objects"]))
    if view["parse_error"]:
        head.append(f"참고: {view['parse_error']}")
    status = view["status"]
    if status == "ok":
        body = "--- 내용 ---\n" + clip(view["content"] or "(빈 파일)", more=f"전체는 GitHub 에서: {url}")
    elif status == "masked":
        body = f"내용을 가렸어요 — 비밀 값이 들어 있을 수 있는 파일이에요 ({view['masked_reason']}). 값은 보여 주지 않아요."
    elif status == "too_large":
        body = f"파일이 커서({browse.MAX_FILE_BYTES // 1_000_000}MB 넘음) 내용을 싣지 않았어요."
    elif status == "binary":
        body = "글자 파일이 아니라(바이너리) 내용을 싣지 않았어요."
    else:
        body = "심볼릭 링크·서브모듈이라 따라가지 않았어요."
    return _done(command, "\n".join([*head, body, f"GitHub: {url}"]))


async def render_app(ctx: RunContext[Deps], name: str, namespace: str | None = None,
                     ref: str | None = None) -> KubectlResult:
    """ArgoCD 앱이 클러스터에 올릴 매니페스트를 ArgoCD 처럼 렌더해 본다 — plain YAML 은 그대로, Kustomize 는 kustomize build.

    파일 하나가 아니라 "이 앱이 실제로 만드는 리소스 전체" 가 필요할 때 쓴다 — overlay 를 합친 최종 결과, OutOfSync 원인을
    클러스터 상태와 비교할 때, PR 전에 결과를 확인할 때. name·namespace 는 ArgoCD Application 의 것이다
    (namespace 는 보통 argocd — 모르면 비워 둔다). Helm 앱은 아직 렌더하지 못한다 — read_repo_file 로 values·템플릿을 읽는다.
    ref 를 안 주면 앱의 targetRevision 을 렌더한다. Secret 값은 가린다.
    """
    label = f"{namespace + '/' if namespace else ''}{name}"
    command = f"repo render {label}"
    try:
        repos = await _connected(ctx.deps)
        wanted_ref = _ref(ref)
        apps_status, items = await _apps(ctx.deps)
        if apps_status != "ok":
            raise _Stop({"absent": "이 클러스터에 ArgoCD 가 없어요.",
                         "forbidden": "ArgoCD 앱을 읽을 권한이 없어요."}.get(apps_status, "ArgoCD 앱 목록을 읽지 못했어요."))
        found = [item for item in items if item["metadata"].get("name") == name
                 and (namespace is None or item["metadata"].get("namespace") == namespace)]
        if not found:
            raise _Stop(f"ArgoCD 앱 {label} 을 이 클러스터에서 찾지 못했어요.")
        if len(found) > 1:
            where = ", ".join(str(item["metadata"].get("namespace")) for item in found)
            raise _Stop(f"ArgoCD 앱 {name} 이 여러 namespace 에 있어요({where}). namespace 를 골라 주세요.")
        item = found[0]
        label = f"{item['metadata'].get('namespace')}/{name}"
        command = f"repo render {label}"
        source = browse.render_source(item)
        if source.unsupported:
            raise _Stop(source.unsupported + " 레포 파일은 read_repo_file 로 읽을 수 있어요.")
        assert source.repository is not None
        if source.repository not in repos:
            raise _Stop(f"ArgoCD 앱 {label} 의 레포 {source.repository} 가 이 클러스터에 연결돼 있지 않아요. "
                        "팀 설정 → 클러스터에서 레포를 연결하면 볼 수 있어요.")
        if wanted_ref is None:
            wanted_ref = browse.clean_ref(source.target_revision)
            if wanted_ref is None:
                raise _Stop(f"앱의 targetRevision 이 브랜치·태그·커밋 이름이 아니에요: {source.target_revision}")
        token = await _token(ctx.deps.cluster_id or "", source.repository)
        revision, result = await _read(browse.render_at(source.repository, token, wanted_ref, source),
                                       source.repository)
    except _Stop as stop:
        return _stopped(command, str(stop))
    command = f"repo render {label}@{revision.ref}"
    url = browse.html_url(source.repository, revision.sha, source.path or "", folder=True)
    head = [f"ArgoCD 앱 {label} · 레포 {source.repository} · path {source.path or '/'} · {source.source_type}"
            f" · ref {revision.ref} (커밋 {_short(revision.sha)})",
            *(f"참고: {note}" for note in source.notes)]
    if result["status"] != "ok":
        return _stopped(command, "\n".join([*head, f"렌더하지 못했어요 — {result['message']}"]))
    parts = []
    for resource in result["resources"]:
        title = f"{resource.get('kind')} {resource.get('namespace') + '/' if resource.get('namespace') else ''}" \
                f"{resource.get('name') or '(이름 없음)'}"
        if resource["yaml"] is None:
            parts.append(f"# {title} — 비밀 값이 있을 수 있어 내용을 가렸어요 ({resource['masked_reason']})")
        else:
            prefix = f"# {title} — Secret 값은 가렸어요\n" if resource["masked"] else ""
            parts.append(prefix + resource["yaml"].rstrip("\n"))
    head.append(f"리소스 {result['total']}개" + (f" (상한을 넘어 {len(result['resources'])}개만)" if result["truncated"] else ""))
    body = clip("\n---\n".join(parts) or "(리소스 없음)", more=f"폴더는 GitHub 에서: {url}")
    return _done(command, "\n".join([*head, body]))


REPO_TOOLS = [list_repo_dir, read_repo_file, render_app]
