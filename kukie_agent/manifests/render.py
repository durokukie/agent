"""렌더링 — ArgoCD 가 클러스터에 올릴 객체를 같은 방법으로 만들어 본다 (DURO-133).

Directory 는 폴더의 YAML·JSON 을 그대로 읽고, Kustomize 는 `kustomize build` 를 돌린다.
kustomize 는 에이전트 이미지에 ArgoCD 와 같은 버전을 넣는다 (Dockerfile 의 KUSTOMIZE_VERSION) — 버전이 다르면 비교가 틀릴 수 있다.

**kustomize 는 네트워크 없이 돈다** (agent#128 리뷰). kustomize 는 resources·생성기 files·patches path 등 여러 칸의 http(s)
주소를 그대로 읽어 오고 원격 base 는 git 으로 받는다 — 고객 레포 내용만으로 에이전트 안쪽 주소(클라우드 메타데이터 등)를
읽어 렌더 결과로 내보낼 수 있다. 칸을 골라 막으면 새 칸·새 접두사로 샌다. 그래서 막는 것은 실행 환경이 맡는다 (fail-closed):
  - 프록시 환경변수를 닿지 않는 주소(OFFLINE_PROXY)로 — Go 의 http 는 이것을 따라 바깥으로 나가지 못한다
  - PATH 를 비우고 git 프로토콜을 막는다 — 원격 base 를 받는 git 을 못 돌린다
  - Go 는 이 컴퓨터 안 주소(localhost·127.x·::1)에는 프록시를 쓰지 않는다 — 레포의 kustomization 어디에든 그런 주소가 있으면
    돌리지 않는다 (loopback_reference)
remote_reference(칸 검사)는 막는 데 기대지 않는다 — 왜 렌더하지 않는지 먼저 알려 주는 문구용이다.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
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
#: kustomize 가 바깥에 닿지 못하게 프록시로 주는 주소 — 아무도 듣지 않는 포트(discard)
OFFLINE_PROXY = "http://127.0.0.1:9"
#: kustomization 안의 주소의 호스트 칸 (`scheme://[userinfo@]host[:port]`)
_URL_AUTHORITY = re.compile(r"[a-z][a-z0-9+.-]*://([^/\s'\"?#]*)", re.I)
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

    **막는 데 기대지 않는다** — 막는 것은 네트워크 없는 실행 환경이다(모듈 설명). 이것은 흔한 칸(resources·bases·components …)의
    원격 주소를 build 전에 찾아 "왜 렌더하지 않는지" 를 알아듣게 알려 주는 일만 한다. folder 에서 시작해 resources·bases·
    components 의 레포 안 폴더를 따라간다. YAML 로 못 읽는 파일은 건너뛴다(kustomize 가 오류를 낸다).
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


def _local_host(authority: str) -> bool:
    """주소의 호스트가 이 컴퓨터 안(Go 가 프록시를 쓰지 않는 곳)일 수 있나. 모르겠는 모양(`%`·`\\`)도 그렇다고 본다."""
    host = authority.rpartition("@")[2]
    if "%" in host or "\\" in host:
        return True                                     # Go 는 호스트의 %XX 를 풀어 읽는다 — 풀면 localhost 일 수 있다
    host = host[1:].partition("]")[0] if host.startswith("[") else host.partition(":")[0]
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or ip.is_unspecified or (mapped is not None and (mapped.is_loopback or mapped.is_unspecified))


def loopback_reference(root: Path) -> str | None:
    """레포의 kustomization 어디에든 이 컴퓨터 안 주소가 있으면 그 주소 하나. 프록시로 막지 못하는 곳이라 build 전에 본다.
    build 가 따라갈 것만 고르지 않고 레포 전체를 본다 — 고르는 규칙이 새면 그대로 뚫린다."""
    for name in KUSTOMIZATION_FILES:
        for file in root.rglob(name):
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for found in _URL_AUTHORITY.finditer(text):
                if _local_host(found.group(1)):
                    return found.group(0)
    return None


def _offline_env(home: str) -> dict[str, str]:
    """kustomize 를 바깥과 끊는 환경 (모듈 설명). 에이전트 프로세스의 토큰·모델 키도 넘기지 않는다."""
    proxy = {name: OFFLINE_PROXY for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                              "http_proxy", "https_proxy", "all_proxy")}
    return {
        "PATH": os.devnull, "HOME": home, **proxy, "NO_PROXY": "", "no_proxy": "",
        "GIT_ALLOW_PROTOCOL": "none", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def render_kustomize(folder: Path, *, timeout: int = 60) -> list[dict]:
    # 실행 파일은 에이전트의 PATH 로 찾고, kustomize 에게는 비운 PATH 를 준다 (_offline_env)
    binary = shutil.which(kustomize_bin())
    if binary is None:
        raise RenderError(f"kustomize build 를 돌리지 못했어요: {kustomize_bin()} 가 없어요")
    with tempfile.TemporaryDirectory(prefix="kukie-kustomize-home-") as home:
        try:
            proc = subprocess.run(
                [binary, "build", str(folder)],
                capture_output=True, text=True, timeout=timeout, env=_offline_env(home),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RenderError(f"kustomize build 를 돌리지 못했어요: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if OFFLINE_PROXY.removeprefix("http://") in stderr or "executable file not found" in stderr:
            raise RenderError("레포 밖 주소를 가져오려 해서 렌더하지 못했어요 — 렌더할 때는 네트워크를 쓰지 않아요. "
                              f"kustomize: {stderr[:_STDERR_LIMIT]}")
        raise RenderError(f"kustomize build 가 실패했어요: {stderr[:_STDERR_LIMIT]}")
    return parse_objects(proc.stdout, "kustomize build 결과")


def render(source_type: str, folder: Path, *, root: Path, recurse: bool = False) -> list[dict]:
    """folder 를 ArgoCD 처럼 렌더링한다. root 는 레포 루트 — folder 가 그 안에 있어야 한다."""
    if source_type == "Directory":
        return render_plain(folder, recurse=recurse, root=root)
    if source_type == "Kustomize":
        local = loopback_reference(root)
        if local is not None:
            raise RenderError(f"레포의 kustomization 에 이 컴퓨터 안 주소({local})가 있어 렌더하지 않아요")
        remote = remote_reference(folder, root)
        if remote is not None:
            raise RenderError(f"레포 밖 주소({remote})를 가져오는 Kustomize 는 렌더하지 않아요 — 렌더할 때는 네트워크를 쓰지 않아요")
        return render_kustomize(folder)
    raise RenderError(f"{source_type} 형식은 아직 렌더링하지 못해요")
