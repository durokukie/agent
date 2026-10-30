"""렌더링 — ArgoCD 가 클러스터에 올릴 객체를 같은 방법으로 만들어 본다 (DURO-133).

Directory 는 폴더의 YAML·JSON 을 그대로 읽고, Kustomize 는 `kustomize build` 를 돌린다.
kustomize 는 에이전트 이미지에 ArgoCD 와 같은 버전을 넣는다 (Dockerfile 의 KUSTOMIZE_VERSION) — 버전이 다르면 비교가 틀릴 수 있다.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
#: kustomize 가 레포 밖에서 가져오는 주소 — http(s) 파일·git 레포 (`https://…`, `git@…`, `github.com/o/r//path`, `ssh://…`)
_REMOTE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:|(?:github\.com|gitlab\.com|bitbucket\.org)/)", re.I)
#: 원격 주소를 받을 수 있는 kustomization 칸. resources·bases·components 의 로컬 폴더는 따라가서 그 안도 본다
_REFERENCE_KEYS = ("resources", "bases", "components", "generators", "transformers", "validators", "crds",
                   "configurations")
_PLAIN_SUFFIXES = (".yaml", ".yml", ".json")
_STDERR_LIMIT = 800


class RenderError(Exception):
    pass


def kustomize_bin() -> str:
    return os.environ.get("KUKIE_KUSTOMIZE", "kustomize")


def resolve_inside(root: Path, relative: str) -> Path | None:
    """레포 루트 기준 경로를 실제 경로로 — 레포 밖(.. · 절대 경로 · 밖을 가리키는 심볼릭 링크)이면 None."""
    if Path(relative).is_absolute():
        return None
    base = root.resolve()
    found = (base / relative).resolve()
    return found if found.is_relative_to(base) else None


def kustomization_file(folder: Path) -> Path | None:
    for name in KUSTOMIZATION_FILES:
        if (folder / name).is_file():
            return folder / name
    return None


def kustomize_roots(root: Path) -> list[str]:
    """레포 안에서 kustomization 파일이 있는 폴더 전부 (레포 루트 기준, 정렬)."""
    found = set()
    for name in KUSTOMIZATION_FILES:
        for file in root.rglob(name):
            if ".git" not in file.relative_to(root).parts:
                found.add(file.parent.relative_to(root).as_posix())
    return sorted(found)


def plain_files(folder: Path, *, recurse: bool) -> list[Path]:
    """ArgoCD Directory 형식이 읽는 파일 — 그 폴더(recurse 면 아래 폴더까지)의 .yaml · .yml · .json."""
    files = folder.rglob("*") if recurse else folder.iterdir()
    return sorted(f for f in files
                  if f.suffix in _PLAIN_SUFFIXES and f.is_file()
                  and ".git" not in f.relative_to(folder).parts)


def parse_objects(text: str, where: str) -> list[dict]:
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise RenderError(f"{where} 를 YAML 로 읽지 못했어요: {exc}") from exc
    # 빈 문서와 쿠버네티스 객체가 아닌 것(apiVersion·kind 가 없는 것)은 ArgoCD 도 올리지 않는다
    return [d for d in docs if isinstance(d, dict) and d.get("apiVersion") and d.get("kind")]


def render_plain(folder: Path, *, recurse: bool, root: Path) -> list[dict]:
    objects = []
    for file in plain_files(folder, recurse=recurse):
        # 레포 밖을 가리키는 심볼릭 링크는 읽지 않는다 — 에이전트 컨테이너의 파일이 렌더 결과에 섞이지 않게 (ArgoCD 도 거절한다)
        if not file.resolve().is_relative_to(root.resolve()):
            raise RenderError(f"{file.name} 가 레포 밖을 가리키는 링크예요")
        objects += parse_objects(file.read_text(encoding="utf-8"), file.name)
    return objects


def remote_reference(folder: Path, root: Path) -> str | None:
    """folder 의 kustomize build 가 레포 밖 주소를 가져오게 되면 그 주소 하나. 없으면 None.

    kustomize 는 `resources:` 의 http(s) 주소를 그대로 읽어 온다 — 고객 레포 내용만으로 에이전트 안쪽 주소(클라우드 메타데이터 등)를
    읽어 렌더 결과로 내보낼 수 있다 (agent#128 리뷰). 그래서 돌리기 전에 build 가 따라갈 kustomization 들을 본다 — folder 에서
    시작해 resources·bases·components 의 레포 안 폴더를 따라간다. YAML 로 못 읽는 파일은 건너뛴다(kustomize 가 오류를 낸다).
    """
    base = root.resolve()
    stack, seen = [folder.resolve()], set()
    while stack:
        here = stack.pop()
        if here in seen:
            continue
        seen.add(here)
        file = kustomization_file(here)
        if file is None:
            continue
        try:
            doc = yaml.safe_load(file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        for key in _REFERENCE_KEYS:
            entries = doc.get(key)
            for entry in entries if isinstance(entries, list) else []:
                text = entry.strip() if isinstance(entry, str) else ""
                if _REMOTE.match(text):
                    return text
                if text and key in ("resources", "bases", "components"):
                    target = (here / text).resolve()
                    if target.is_dir() and target.is_relative_to(base):
                        stack.append(target)
    return None


def render_kustomize(folder: Path, *, timeout: int = 60) -> list[dict]:
    # 환경변수는 PATH 와 빈 HOME 만 넘긴다 — 에이전트 프로세스의 토큰·모델 키가 kustomize 쪽에 닿지 않게
    with tempfile.TemporaryDirectory(prefix="kukie-kustomize-home-") as home:
        try:
            proc = subprocess.run(
                [kustomize_bin(), "build", str(folder)],
                capture_output=True, text=True, timeout=timeout,
                env={"PATH": os.environ.get("PATH", ""), "HOME": home},
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RenderError(f"kustomize build 를 돌리지 못했어요: {exc}") from exc
    if proc.returncode != 0:
        raise RenderError(f"kustomize build 가 실패했어요: {proc.stderr.strip()[:_STDERR_LIMIT]}")
    return parse_objects(proc.stdout, "kustomize build 결과")


def render(source_type: str, folder: Path, *, root: Path, recurse: bool = False) -> list[dict]:
    """folder 를 ArgoCD 처럼 렌더링한다. root 는 레포 루트 — folder 가 그 안에 있어야 한다."""
    if source_type == "Directory":
        return render_plain(folder, recurse=recurse, root=root)
    if source_type == "Kustomize":
        remote = remote_reference(folder, root)
        if remote is not None:
            raise RenderError(f"레포 밖 주소({remote})를 가져오는 Kustomize 는 렌더하지 않아요 — 에이전트가 레포 밖을 읽지 않게요")
        return render_kustomize(folder)
    raise RenderError(f"{source_type} 형식은 아직 렌더링하지 못해요")
