"""매니페스트 레포 보기 — ArgoCD 화면 "레포" 탭의 읽기 (DURO-148). 엔드포인트는 clusters_api 에 있다.

    폴더 하나      GitHub Contents API 로 그 폴더만 읽는다 (게으르게 — 앱이 폴더를 펼칠 때마다). 항목은 MAX_ENTRIES 개까지
    파일 하나      같은 API 로 읽는다. MAX_FILE_BYTES 를 넘으면 내용 없이, Secret 처럼 보이면 내용 대신 가림(masked)
    앱 렌더 결과   Application path 를 ArgoCD 처럼 렌더한다 — plain 그대로·Kustomize `kustomize build` (manifests/render.py).
                   Helm·Plugin 은 아직 "지원 전"(unsupported). Secret 리소스의 값은 가린다
    앱 ↔ 경로      클러스터의 Application 들(spec.source(s) 의 repoURL·path·targetRevision)을 폴더·파일에 붙인다

토큰은 server 가 주는 그 레포 범위 설치 토큰(server_api.repo_token)이다 — 응답·로그·캐시 키에 싣지 않는다.

비용
  - 보는 ref(브랜치·태그)는 먼저 커밋 id 로 바꾼다. REF_TTL 초 안에는 다시 묻지 않고, 지나면 ETag 로 묻는다(304 는 한도에 안 센다).
    커밋 id 로 주면 있는지 한 번만 묻는다
  - 커밋 id 가 같으면 내용도 같다 — 폴더·파일·렌더 결과를 (레포, 커밋, 경로) 로 기억한다 (바이트 상한 LRU, 프로세스 안)
  - 같은 렌더가 동시에 오면 한 번만 돈다
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import yaml
from anyio import to_thread

from kukie_agent.gitops import github
from kukie_agent.gitops.repo import repo_name
from kukie_agent.kubectl import thread_limiter
from kukie_agent.manifests import render as renderer

logger = logging.getLogger(__name__)

#: 폴더 하나에 싣는 항목 수 — 큰 레포(monorepo)의 폴더 하나가 응답을 키우지 않게. 넘으면 truncated
MAX_ENTRIES = 300
#: 파일 내용 상한(바이트). 넘으면 내용 없이 too_large — GitHub Contents API 도 1MB 까지만 내용을 준다
MAX_FILE_BYTES = 1_000_000
#: 렌더 결과 상한 — 리소스 수와 YAML 글자 수. 넘는 것은 빼고 truncated
MAX_RENDER_RESOURCES = 500
MAX_RENDER_BYTES = 2_000_000
#: 브랜치·태그 → 커밋 id 를 다시 묻지 않는 시간(초). 지나면 ETag 로 묻는다 — 바뀐 커밋이 그만큼 늦게 보인다
REF_TTL = 30
#: 기본 브랜치 이름을 다시 묻지 않는 시간(초) — 거의 바뀌지 않는다
DEFAULT_BRANCH_TTL = 300
#: 커밋 id 로 기억하는 폴더·파일과 렌더 결과의 크기 상한(바이트, 대략). 프로세스 안 값이라 복제본마다 따로다
CONTENT_CACHE_BYTES = 32 * 1024 * 1024
RENDER_CACHE_BYTES = 32 * 1024 * 1024
#: ref 기억의 수 상한 — 넘으면 오래된 것부터 버린다
MAX_REFS = 1000
#: 렌더(kustomize)는 CPU·디스크를 쓴다 — PR 길의 렌더 비교와 같은 묶음을 쓴다
RENDER_WORKERS = 4

MASKED = "(가림)"
#: 렌더할 수 있는 ArgoCD 형식 (status.sourceType). Helm 은 DURO-137
RENDERABLE = ("Directory", "Kustomize")
_SHA = re.compile(r"[0-9a-f]{40}")


class NotFound(Exception):
    """레포에 그 ref·경로가 없다. what 은 "ref" | "path"."""

    def __init__(self, what: str, message: str) -> None:
        super().__init__(message)
        self.what = what
        self.message = message


# ── 입력 검사 ────────────────────────────────────────────

def clean_path(path: str | None) -> str | None:
    """URL 의 경로 → 레포 루트 기준 경로 (앞뒤 `/` 를 뗀다, 루트는 빈 값). `..`·`.`·빈 칸·제어 문자가 있으면 None."""
    text = (path or "").strip().strip("/")
    if not text:
        return ""
    if len(text) > 1024 or "\\" in text or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        return None
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return text


def clean_ref(ref: str | None) -> str | None:
    """URL·Application 의 ref. 빈 값·HEAD 면 ""(기본 브랜치). git 이 받지 않는 모양이면 None — GitHub 경로에 그대로 넣기 전에 본다."""
    text = (ref or "").strip().removeprefix("refs/heads/")
    if text in ("", "HEAD"):
        return ""
    return text if github.branch_name_ok(text) else None


def repository_of(owner: str, repo: str) -> str | None:
    """URL 의 owner·repo → 소문자 `owner/repo`. GitHub 이름 꼴이 아니면 None (repo.repo_name 과 같은 규칙)."""
    return repo_name(f"https://github.com/{owner}/{repo}")


# ── 바이트 상한 LRU ──────────────────────────────────────

class _Cache:
    """크기(바이트, 대략)로 자르는 LRU. 커밋 id 가 열쇠에 있는 값만 넣는다 — 같은 커밋이면 내용이 바뀌지 않는다."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._items: OrderedDict[tuple, tuple[Any, int]] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple) -> Any:
        found = self._items.get(key)
        if found is None:
            return None
        self._items.move_to_end(key)
        return found[0]

    def put(self, key: tuple, value: Any, size: int) -> None:
        if size > self.limit:
            return
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        self._items[key] = (value, size)
        self._bytes += size
        while self._bytes > self.limit:
            _, (_, dropped) = self._items.popitem(last=False)
            self._bytes -= dropped

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0


_contents = _Cache(CONTENT_CACHE_BYTES)
_renders = _Cache(RENDER_CACHE_BYTES)
#: (레포, ref) → (커밋 id, ETag, 물어본 시각)
_refs: OrderedDict[tuple[str, str], tuple[str, str, float]] = OrderedDict()
#: 있다고 확인한 (레포, 커밋 id) — 커밋은 바뀌지 않아 TTL 이 없다
_commits: OrderedDict[tuple[str, str], None] = OrderedDict()
#: 레포 → (기본 브랜치, 물어본 시각)
_defaults: dict[str, tuple[str, float]] = {}
#: 진행 중인 렌더 — 같은 열쇠의 요청은 그 한 번을 같이 기다린다
_inflight: dict[tuple, asyncio.Future] = {}


def forget() -> None:
    """기억을 모두 비운다 (테스트)."""
    _contents.clear()
    _renders.clear()
    _refs.clear()
    _commits.clear()
    _defaults.clear()
    _inflight.clear()


# ── ref → 커밋 ──────────────────────────────────────────

def _now() -> float:
    """시각(초) — 테스트가 바꿔 끼운다."""
    return time.monotonic()


async def default_branch(gh: github.GitHub, repository: str) -> str:
    found = _defaults.get(repository)
    if found is not None and _now() - found[1] < DEFAULT_BRANCH_TTL:
        return found[0]
    branch = (await gh.info()).default_branch
    if len(_defaults) >= MAX_REFS:
        _defaults.clear()
    _defaults[repository] = (branch, _now())
    return branch


async def commit_of(gh: github.GitHub, repository: str, ref: str) -> str | None:
    """ref → 커밋 id. 그런 ref 가 없으면 None.

    커밋 id(40자)도 한 번은 묻는다 — 없는 커밋을 그대로 쓰면 폴더·파일은 "경로 없음", 렌더는 GitHub 실패로 엉뚱하게 갈린다
    (agent#128 리뷰). 있다고 확인한 커밋 id 는 바뀌지 않으니 TTL 없이 기억한다.
    """
    if _SHA.fullmatch(ref) and (repository, ref) in _commits:
        _commits.move_to_end((repository, ref))
        return ref
    key = (repository, ref)
    if _SHA.fullmatch(ref):
        found = await gh.commit_sha(ref)
        if found is None:
            return None
        _commits[key] = None
        while len(_commits) > MAX_REFS:
            _commits.popitem(last=False)
        return found[0]
    cached = _refs.get(key)
    now = _now()
    if cached is not None and now - cached[2] < REF_TTL:
        return cached[0]
    found = await gh.commit_sha(ref, cached=(cached[0], cached[1]) if cached else None)
    if found is None:
        _refs.pop(key, None)
        return None
    _refs[key] = (found[0], found[1], now)
    _refs.move_to_end(key)
    while len(_refs) > MAX_REFS:
        _refs.popitem(last=False)
    return found[0]


@dataclass(frozen=True)
class Revision:
    """보는 커밋 — ref 는 사람이 고른 이름(빈 값이면 기본 브랜치 이름), sha 는 그 커밋 id."""
    ref: str
    sha: str
    default_branch: str


async def resolve(gh: github.GitHub, repository: str, ref: str) -> Revision:
    branch = await default_branch(gh, repository)
    name = ref or branch
    sha = await commit_of(gh, repository, name)
    if sha is None:
        raise NotFound("ref", f"레포 {repository} 에 '{name}' 브랜치·태그·커밋이 없어요")
    return Revision(ref=name, sha=sha, default_branch=branch)


# ── 앱 ↔ 경로 ──────────────────────────────────────────

@dataclass(frozen=True)
class AppPath:
    """Application 의 소스 하나가 이 레포에서 쓰는 경로."""
    name: str
    namespace: str
    path: str                   # 레포 루트 기준, 루트면 ""
    target_revision: str
    source_type: str | None


def _app_path(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    return "" if text in ("", ".") else clean_path(text)


def app_paths(items: list[dict[str, Any]], repository: str) -> list[AppPath]:
    """클러스터의 Application 목록에서 이 레포를 쓰는 소스들. 여러 소스(spec.sources)면 소스마다 한 줄, path 가 없는
    소스(Helm 저장소 차트 · values 만 주는 ref 소스)는 뺀다."""
    found: list[AppPath] = []
    for item in items:
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        multi = [entry for entry in spec.get("sources") or [] if isinstance(entry, dict)] \
            if isinstance(spec.get("sources"), list) else []
        sources = multi or ([spec["source"]] if isinstance(spec.get("source"), dict) else [])
        types = status.get("sourceTypes") if multi else [status.get("sourceType")]
        types = types if isinstance(types, list) else []
        for index, source in enumerate(sources):
            path = _app_path(source.get("path"))
            if path is None or repo_name(source.get("repoURL")) != repository:
                continue
            kind = types[index] if index < len(types) and isinstance(types[index], str) and types[index] else None
            found.append(AppPath(name=str(meta.get("name") or ""), namespace=str(meta.get("namespace") or ""),
                                 path=path, target_revision=str(source.get("targetRevision") or ""),
                                 source_type=kind))
    found.sort(key=lambda app: (app.path, app.namespace, app.name))
    return found


def _inside(path: str, folder: str) -> bool:
    """path 가 folder 아래에 있나 (같으면 아니다). folder 가 루트("")면 루트가 아닌 모든 경로."""
    return path != folder and (folder == "" or path.startswith(folder + "/"))


def _same_revision(app: AppPath, revision: Revision) -> bool:
    """앱이 따라가는 revision 이 지금 보는 것과 같은 이름·커밋인가. HEAD·빈 값은 기본 브랜치다."""
    wanted = app.target_revision.strip().removeprefix("refs/heads/")
    if wanted in ("", "HEAD"):
        wanted = revision.default_branch
    return wanted in (revision.ref, revision.sha)


def _app_badge(app: AppPath, revision: Revision, relation: str) -> dict[str, Any]:
    return {
        "name": app.name,
        "namespace": app.namespace,
        "path": app.path,
        "source_type": app.source_type,
        "target_revision": app.target_revision,
        "same_revision": _same_revision(app, revision),
        "relation": relation,
    }


def apps_for_folder(apps: list[AppPath], folder: str, revision: Revision) -> list[dict[str, Any]]:
    """폴더(또는 폴더 항목)에 붙일 앱 — here: 그 폴더가 앱 path, below: 앱 path 가 그 아래."""
    return [_app_badge(app, revision, "here" if app.path == folder else "below")
            for app in apps if app.path == folder or _inside(app.path, folder)]


def apps_around(apps: list[AppPath], path: str, revision: Revision, *, is_file: bool) -> list[dict[str, Any]]:
    """지금 보는 폴더·파일과 이어진 앱 — here(그 폴더가 앱 path · 파일이 앱 path 바로 아래), above(앱 path 의 더 아래에 있다),
    below(앱 path 가 지금 폴더 아래)."""
    folder = str(PurePosixPath(path).parent) if is_file else path
    folder = "" if folder == "." else folder
    found = []
    for app in apps:
        if app.path == folder:
            found.append(_app_badge(app, revision, "here"))
        elif _inside(folder, app.path):
            found.append(_app_badge(app, revision, "above"))
        elif not is_file and _inside(app.path, folder):
            found.append(_app_badge(app, revision, "below"))
    return found


# ── 폴더·파일 ──────────────────────────────────────────

async def _read(gh: github.GitHub, repository: str, revision: Revision, path: str) -> Any:
    """그 커밋의 경로 — GitHub 가 준 그대로가 아니라 쓸 모양으로 정리해 기억한다 (Secret 내용은 기억에도 남기지 않는다)."""
    key = (repository, revision.sha, path)
    found = _contents.get(key)
    if found is not None:
        return found
    data = await gh.contents(path, revision.sha)
    if data is None:
        raise NotFound("path", f"{revision.ref} 에 '{path or '/'}' 가 없어요")
    view = _folder_view(data) if isinstance(data, list) else _file_view(path, data)
    _contents.put(key, view, _size(view))
    return view


def _size(view: Any) -> int:
    if isinstance(view, dict) and view.get("kind") == "folder":
        return 200 * len(view["entries"]) + 200
    return len((view or {}).get("content") or "") + 200 * len((view or {}).get("objects") or []) + 200


def _folder_view(data: list[Any]) -> dict[str, Any]:
    entries = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name") or not item.get("path"):
            continue
        kind = item.get("type") if item.get("type") in ("file", "dir", "symlink", "submodule") else "file"
        entries.append({
            "name": str(item["name"]),
            "path": str(item["path"]),
            "type": kind,
            "size": int(item["size"]) if kind == "file" and isinstance(item.get("size"), int) else None,
        })
    # 폴더 먼저, 그다음 이름순 — GitHub 화면과 같다
    entries.sort(key=lambda entry: (entry["type"] != "dir", entry["name"].lower(), entry["name"]))
    return {"kind": "folder", "entries": entries}


async def folder(gh: github.GitHub, repository: str, revision: Revision, path: str) -> dict[str, Any]:
    """폴더 하나의 항목 (MAX_ENTRIES 개까지). 파일 경로면 NotFound."""
    view = await _read(gh, repository, revision, path)
    if view.get("kind") != "folder":
        raise NotFound("path", f"'{path}' 는 폴더가 아니에요")
    entries = view["entries"]
    return {"entries": [dict(entry) for entry in entries[:MAX_ENTRIES]], "total": len(entries),
            "truncated": len(entries) > MAX_ENTRIES}


#: 이름만으로 비밀로 보는 파일 — sops·암호화 파일, kustomize secretGenerator 가 읽는 .env, 키 파일
_SECRET_NAME = re.compile(
    r"(\.enc(\.[a-z0-9]+)?$|(^|[./_-])sops([./_-]|$)|(^|/)\.env(\..*)?$|\.env$|"
    r"\.(pem|key|p12|pfx|jks|keystore|gpg|asc)$|(^|/)id_(rsa|dsa|ecdsa|ed25519)$)",
    re.IGNORECASE,
)
#: 글자로 보는 비밀 — YAML 로 못 읽는 파일(Helm 템플릿 등)도 이것으로 본다
_SECRET_TEXT = (
    ("kind: Secret", re.compile(r"^\s*kind:\s*[\"']?Secret[\"']?\s*(#.*)?$", re.MULTILINE)),
    ("stringData", re.compile(r"^\s*[\"']?stringData[\"']?\s*:", re.MULTILINE)),
    ("sops", re.compile(r"ENC\[(AES256_GCM|PGP|age)|^sops:\s*$", re.MULTILINE)),
    ("비밀 키", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
)


def secret_reason(path: str, text: str, docs: list[Any] | None) -> str | None:
    """이 파일을 가려야 하는 까닭. 가리지 않아도 되면 None. docs 는 YAML 로 읽은 문서들(못 읽었으면 None)."""
    if _SECRET_NAME.search(PurePosixPath(path).name):
        return "이름이 비밀 파일 꼴이에요"
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "Secret":
            return "kind: Secret"
        if "stringData" in doc:
            return "stringData"
        if isinstance(doc.get("sops"), dict):
            return "sops"
        generators = doc.get("secretGenerator")
        if isinstance(generators, list) and any(isinstance(g, dict) and g.get("literals") for g in generators):
            return "secretGenerator literals"
    for reason, pattern in _SECRET_TEXT:
        if pattern.search(text):
            return reason
    return None


def _docs(path: str, text: str) -> tuple[list[Any] | None, str | None]:
    """(YAML 문서들, 못 읽은 까닭). YAML·JSON 이 아닌 파일은 (None, None)."""
    if PurePosixPath(path).suffix.lower() not in (".yaml", ".yml", ".json"):
        return None, None
    try:
        return list(yaml.safe_load_all(text)), None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" ({mark.line + 1}번째 줄)" if mark is not None else ""
        return None, f"YAML 로 읽지 못했어요{where}"


def describe(doc: Any) -> dict[str, Any] | None:
    """쿠버네티스 객체 하나 → {api_version, kind, name, namespace}. 객체가 아니면 None — 앱의 빠른 질문 버튼이 쓴다."""
    if not isinstance(doc, dict) or not doc.get("kind") or not doc.get("apiVersion"):
        return None
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    name, namespace = meta.get("name"), meta.get("namespace")
    return {
        "api_version": str(doc["apiVersion"]),
        "kind": str(doc["kind"]),
        "name": name if isinstance(name, str) and name else None,
        "namespace": namespace if isinstance(namespace, str) and namespace else None,
    }


def _file_view(path: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("GitHub Contents 응답 모양이 아니다")
    kind = data.get("type")
    size = data.get("size") if isinstance(data.get("size"), int) else None
    view: dict[str, Any] = {"kind": "file", "size": size, "status": "ok", "content": None, "masked_reason": None,
                            "objects": [], "parse_error": None}
    if kind != "file":
        return {**view, "status": "unsupported"}           # 심볼릭 링크·서브모듈 — 따라가지 않는다
    if size is None or size > MAX_FILE_BYTES or data.get("encoding") != "base64":
        return {**view, "status": "too_large"}
    try:
        raw = base64.b64decode(str(data.get("content") or ""), validate=False)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):     # UnicodeDecodeError 도 ValueError 지만 뜻을 적어 둔다
        return {**view, "status": "binary"}
    if "\x00" in text:
        return {**view, "status": "binary"}
    docs, parse_error = _docs(path, text)
    objects = [found for found in map(describe, docs or []) if found is not None]
    reason = secret_reason(path, text, docs)
    if reason is not None:
        return {**view, "status": "masked", "masked_reason": reason, "objects": objects, "parse_error": parse_error}
    return {**view, "content": text, "objects": objects, "parse_error": parse_error}


async def file(gh: github.GitHub, repository: str, revision: Revision, path: str) -> dict[str, Any]:
    """파일 하나. 폴더 경로면 NotFound."""
    if not path:
        raise NotFound("path", "레포 루트는 파일이 아니에요")
    view = await _read(gh, repository, revision, path)
    if view.get("kind") != "file":
        raise NotFound("path", f"'{path}' 는 파일이 아니에요")
    return {key: value for key, value in view.items() if key != "kind"}


def html_url(repository: str, sha: str, path: str, *, folder: bool) -> str:
    """GitHub 화면 주소 — 커밋 id 로 고정한다 (나중에 브랜치가 움직여도 같은 내용).
    경로는 조각마다 퍼센트 인코딩한다 — `#`·`?`·`%`·공백·한글 파일 이름도 링크가 깨지지 않게 (`/` 는 그대로)."""
    kind = "tree" if folder else "blob"
    return f"https://github.com/{repository}/{kind}/{sha}" + (f"/{quote(path, safe='/')}" if path else "")


# ── 앱 렌더 결과 ────────────────────────────────────────

@dataclass(frozen=True)
class RenderSource:
    """Application 하나에서 렌더에 쓰는 것."""
    repository: str | None      # GitHub 레포가 아니면 None
    repo_url: str
    path: str | None            # 레포 루트 기준. 없거나 레포 밖이면 None
    target_revision: str
    source_type: str
    recurse: bool
    notes: tuple[str, ...]      # 렌더에 반영하지 못한 Application 설정
    unsupported: str | None     # 렌더하지 못하는 까닭 (여러 소스·Helm·GitHub 아님 …)


def render_source(item: dict[str, Any]) -> RenderSource:
    """Application(kubectl -o json 한 개) → 렌더할 것. 판단은 PR 길(route.app_source)과 같다."""
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    sources = spec.get("sources")
    source = spec.get("source") if isinstance(spec.get("source"), dict) else {}
    multi = isinstance(sources, list) and bool(sources)
    if multi:
        source = sources[0] if isinstance(sources[0], dict) else {}
    source_type = str(status.get("sourceType") or ("Helm" if source.get("chart") else "")) if not multi else ""
    repo_url = str(source.get("repoURL") or "")
    path = _app_path(source.get("path"))
    directory = source.get("directory") if isinstance(source.get("directory"), dict) else {}
    notes = []
    if source_type == "Kustomize" and isinstance(source.get("kustomize"), dict) and source["kustomize"]:
        notes.append("ArgoCD 앱의 kustomize 덮어쓰기(spec.source.kustomize — 이미지·namePrefix 등)는 아직 반영하지 않았어요. "
                     "클러스터에 들어간 것과 다를 수 있어요.")
    if source_type == "Directory" and any(directory.get(key) for key in ("include", "exclude", "jsonnet")):
        notes.append("ArgoCD 앱의 directory include·exclude·jsonnet 설정은 아직 반영하지 않았어요. "
                     "클러스터에 들어간 것과 다를 수 있어요.")
    unsupported = None
    if multi:
        unsupported = "소스가 여러 개인 ArgoCD 앱은 아직 렌더 결과를 보여 주지 못해요."
    elif source_type not in RENDERABLE:
        what = "Helm" if source_type == "Helm" else source_type or "알 수 없는 형식"
        unsupported = f"{what} 앱은 아직 렌더 결과를 보여 주지 못해요. plain YAML 과 Kustomize 만 돼요."
    elif repo_name(repo_url) is None:
        unsupported = "소스가 GitHub 레포가 아니라 렌더 결과를 보여 주지 못해요."
    elif path is None:
        unsupported = "ArgoCD 앱의 path 를 레포 안 경로로 읽지 못했어요."
    return RenderSource(
        repository=repo_name(repo_url), repo_url=repo_url, path=path,
        target_revision=str(source.get("targetRevision") or ""), source_type=source_type,
        recurse=directory.get("recurse") is True, notes=tuple(notes), unsupported=unsupported,
    )


def mask_secret(obj: dict[str, Any]) -> dict[str, Any]:
    """Secret 리소스의 값을 가린다 — 키 이름은 남긴다. last-applied 어노테이션(값이 그대로 든다)은 뺀다."""
    if obj.get("kind") != "Secret":
        return obj
    masked = dict(obj)
    for field_name in ("data", "stringData"):
        values = masked.get(field_name)
        if isinstance(values, dict):
            masked[field_name] = {key: MASKED for key in values}
    meta = masked.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("annotations"), dict):
        annotations = {key: value for key, value in meta["annotations"].items()
                       if key != "kubectl.kubernetes.io/last-applied-configuration"}
        masked["metadata"] = {**meta, "annotations": annotations}
    return masked


def _dump(obj: dict[str, Any]) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, default_flow_style=False, width=4096)


def rendered_view(objects: list[dict[str, Any]]) -> dict[str, Any]:
    """렌더한 객체들 → 응답의 resources (객체마다 kind·name·namespace 와 YAML). 상한을 넘는 것은 뺀다."""
    resources: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for obj in objects:
        masked = mask_secret(obj)
        text = _dump(masked)
        if len(resources) >= MAX_RENDER_RESOURCES or used + len(text) > MAX_RENDER_BYTES:
            truncated = True
            break
        used += len(text)
        resources.append({**(describe(masked) or {}), "masked": masked is not obj, "yaml": text})
    return {"status": "ok", "message": None, "resources": resources, "total": len(objects), "truncated": truncated}


def _render_folder(root: Path, source: RenderSource) -> dict[str, Any]:
    """레포를 푼 곳에서 렌더한다. 블로킹 — 스레드에서 돈다. 실패는 render_failed 로 돌려준다 (예외로 올리지 않는다)."""
    folder = renderer.resolve_inside(root, source.path or "")
    if folder is None or not folder.is_dir():
        return {"status": "render_failed", "message": f"레포에 앱 path '{source.path}' 폴더가 없어요.",
                "resources": [], "total": 0, "truncated": False}
    if source.source_type == "Kustomize" and renderer.kustomization_file(folder) is None:
        return {"status": "render_failed", "message": f"'{source.path}' 에 kustomization 파일이 없어요.",
                "resources": [], "total": 0, "truncated": False}
    try:
        objects = renderer.render(source.source_type, folder, root=root, recurse=source.recurse)
    except renderer.RenderError as exc:
        # kustomize 오류에는 레포를 푼 임시 경로가 찍힌다 — 레포 기준으로 바꿔 싣는다
        message = str(exc).replace(str(root.resolve()) + "/", "").replace(str(root) + "/", "")
        return {"status": "render_failed", "message": message, "resources": [], "total": 0, "truncated": False}
    except (OSError, UnicodeDecodeError) as exc:
        return {"status": "render_failed", "message": f"파일을 읽지 못했어요: {type(exc).__name__}",
                "resources": [], "total": 0, "truncated": False}
    return rendered_view(objects)


async def render(repository: str, token: str, sha: str, source: RenderSource) -> dict[str, Any]:
    """그 커밋에서 앱 path 를 렌더한 결과. 같은 (레포, 커밋, 경로, 형식) 이면 기억한 것을, 진행 중이면 그 한 번을 같이 쓴다.

    렌더는 요청과 따로 돈다 — 먼저 부른 요청이 끊겨도 같이 기다리는 요청이 있을 수 있다. 그래서 GitHub 연결도 그 안에서 연다.
    """
    key = (repository, sha, source.path, source.source_type, source.recurse)
    found = _renders.get(key)
    if found is not None:
        return found
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_render_now(repository, token, sha, source))
        _inflight[key] = task

        def finished(done: asyncio.Future) -> None:
            if _inflight.get(key) is done:
                del _inflight[key]
            if not done.cancelled() and done.exception() is None:
                body = done.result()
                # 렌더 실패도 같은 커밋이면 다시 해도 같다 — 기억한다. GitHub 실패(예외)는 기억하지 않는다
                _renders.put(key, body, sum(len(r["yaml"]) for r in body["resources"]) + 1000)

        task.add_done_callback(finished)
    return await asyncio.shield(task)


async def _render_now(repository: str, token: str, sha: str, source: RenderSource) -> dict[str, Any]:
    async with github.connect(repository, token) as gh:
        with tempfile.TemporaryDirectory(prefix="kukie-repo-view-") as work:
            root = await gh.download(sha, Path(work) / "repo")
            return await to_thread.run_sync(partial(_render_folder, root, source),
                                            limiter=thread_limiter("gitops_render", RENDER_WORKERS))
