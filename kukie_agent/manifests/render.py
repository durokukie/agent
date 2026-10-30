"""렌더링 — ArgoCD 가 클러스터에 올릴 객체를 같은 방법으로 만들어 본다 (DURO-133).

Directory 는 폴더의 YAML·JSON 을 그대로 읽고, Kustomize 는 `kustomize build` 를 돌린다.
kustomize 는 에이전트 이미지에 ArgoCD 와 같은 버전을 넣는다 (Dockerfile 의 KUSTOMIZE_VERSION) — 버전이 다르면 비교가 틀릴 수 있다.

**kustomize 는 네트워크 없이 돈다** (agent#128 리뷰). kustomize 는 resources·생성기 files·patches path 등 여러 칸의 http(s)
주소를 그대로 읽어 오고 원격 base 는 git 으로 받는다 — 고객 레포 내용만으로 에이전트 안쪽 주소(클라우드 메타데이터 등)를
읽어 렌더 결과로 내보낼 수 있다. 칸을 골라 막으면 새 칸·새 접두사로 샌다. 그래서 막는 것은 실행 환경이 맡는다 (fail-closed):
  - 프록시 환경변수를 닿지 않는 주소(OFFLINE_PROXY)로 — Go 의 http 는 이것을 따라 바깥으로 나가지 못한다
  - PATH 를 비우고 git 프로토콜을 막는다 — 원격 base 를 받는 git 을 못 돌린다
  - Go 는 이 컴퓨터 안 주소(localhost·127.x·::1)에는 프록시를 쓰지 않는다 — 레포의 파일 어디에든(YAML 로 읽은 문자열)
    그런 주소가 있으면 돌리지 않는다 (loopback_reference)
remote_reference(칸 검사)는 막는 데 기대지 않는다 — 왜 렌더하지 않는지 먼저 알려 주는 문구용이다.
"""
from __future__ import annotations

import codecs
import logging
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML as _RuamelYAML
from ruamel.yaml.error import YAMLError as _RuamelYAMLError

logger = logging.getLogger(__name__)

KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
#: kustomize 가 레포 밖에서 가져오는 주소 — http(s) 파일·git 레포 (`https://…`, `git@…`, `github.com/o/r//path`, `ssh://…`,
#: `git::…`, `gh:o/r`). 문구용이다 — 막는 것은 네트워크 없는 실행 환경이다 (모듈 설명)
_REMOTE = re.compile(r"^(?:git::|gh:|[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:|(?:github\.com|gitlab\.com|bitbucket\.org)/)",
                     re.I)
#: 원격 주소를 받을 수 있는 kustomization 칸. resources·bases·components 의 로컬 폴더는 따라가서 그 안도 본다
_REFERENCE_KEYS = ("resources", "bases", "components", "generators", "transformers", "validators", "crds",
                   "configurations")
#: kustomize 가 바깥에 닿지 못하게 프록시로 주는 주소 — 아무도 듣지 않는 포트(discard)
OFFLINE_PROXY = "http://127.0.0.1:9"
#: 주소 찾기 — `://` 를 str.find 로 찾고 그 앞 _SCHEME_WINDOW 글자에서만 scheme 을 본다. 정규식으로 scheme 부터 찾으면
#: `://` 없는 긴 영숫자 덩어리에서 시작 자리마다 끝까지 되돌아가 O(n²) 이 된다 (agent#128 리뷰) — 이 방식은 선형이다
_SCHEME_WINDOW = 32
_SCHEME_TAIL = re.compile(r"[a-z][a-z0-9+.-]*$", re.I)
#: 호스트 칸(userinfo·host·port)이 끝나는 글자
_AUTHORITY = re.compile(r"[^/\s'\"?#]*")
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
        # 오류 문구에는 레포 값 조각이 실린다(Secret 값일 수 있다) — 줄 번호만 준다 (agent#128 리뷰)
        mark = getattr(exc, "problem_mark", None)
        where_line = f" ({mark.line + 1}번째 줄)" if mark is not None else ""
        raise RenderError(f"{where} 를 YAML 로 읽지 못했어요{where_line}") from None
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


def _strings(docs: list[Any]) -> Iterator[str]:
    """YAML 문서들의 문자열(키·값) 전부. 같은 객체를 두 번 따라가지 않는다 — 별칭(&·*)으로 부풀린 문서에 묶이지 않게."""
    seen: set[int] = set()
    stack: list[Any] = list(docs)
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, (dict, list)):
            if id(node) in seen:
                continue
            seen.add(id(node))
            stack.extend([*node.keys(), *node.values()] if isinstance(node, dict) else node)


def _redact(address: str) -> str:
    """문구에 실을 주소 — `scheme://host[:port]` 만. userinfo(`svc:pw@`)·경로는 뺀다 (agent#128 리뷰)."""
    scheme, sep, rest = address.partition("://")
    if not sep:
        return address
    authority = re.split(r"[/?#\s]", rest, maxsplit=1)[0]
    return f"{scheme}://{authority.rpartition('@')[2]}"


def _addresses(text: str) -> Iterator[tuple[int, int, str, str]]:
    """글자 속 주소마다 (시작, 호스트 칸 끝, scheme, 호스트 칸). 선형이다 — `://` 마다 앞 32글자·뒤 호스트 칸만 본다."""
    at = text.find("://")
    while at != -1:
        window = text[max(0, at - _SCHEME_WINDOW):at]
        scheme_found = _SCHEME_TAIL.search(window)
        scheme = scheme_found.group(0) if scheme_found else ""
        authority = _AUTHORITY.match(text, at + 3)
        end = authority.end() if authority else at + 3
        yield at - len(scheme), end, scheme, text[at + 3:end]
        at = text.find("://", at + 3)


def _loopback_in(text: str) -> str | None:
    for _, _, scheme, authority in _addresses(text):
        if _local_host(authority):
            return _redact(f"{scheme}://{authority}")
    return None


def _redact_text(text: str) -> str:
    """글자 속 주소들의 userinfo(`scheme://svc:pw@`)를 뺀다 — 서버 로그에 남기기 전에."""
    out, last = [], 0
    for start, end, scheme, authority in _addresses(text):
        if "@" in authority:
            out += [text[last:start], f"{scheme}://{authority.rpartition('@')[2]}"]
            last = end
    return "".join(out) + text[last:]


#: 이 크기까지는 YAML 로 읽어 풀린 문자열을 본다. 넘는 글자 파일(CRD 꾸러미 등)은 원시 글자로 흘려 읽으며 주소만 찾는다 —
#: 이 크기의 파일에 사람이 이스케이프로 주소를 숨긴 설정일 가능성은 낮고, 막아 버리면 그 레포의 렌더가 통째로 안 된다
MAX_SCAN_BYTES = 5 * 1024 * 1024
#: 레포 하나의 검사 시간(초). 넘으면 렌더하지 않는다 (fail-closed) — 파일이 아주 많거나 무거운 레포
SCAN_BUDGET = 10.0
_STREAM_CHUNK = 1024 * 1024
_STREAM_OVERLAP = 4096


class ScanTimeout(Exception):
    """레포 검사가 SCAN_BUDGET 을 넘었다."""


class RenderTimeout(RenderError):
    """레포 검사 시간이 넘어 렌더하지 않았다 — RenderError 와 같은 뜻이지만 그때의 부하 탓일 수 있어 결과를 기억하지 않는다."""


class _ParserLimit(Exception):
    """YAML 문법이 틀린 게 아니라 우리 파서의 한도(깊은 중첩의 RecursionError 등)에 걸렸다 — kustomize(go-yaml)는 읽을 수 있다."""


def _parse(data: bytes) -> list[Any] | None:
    """YAML 문서들. YAML 문법으로 못 읽으면 None. bytes 로 넘긴다 — UTF-16(BOM) 도 kustomize(go-yaml)처럼 읽는다.

    PyYAML(빠르다)이 YAML 문법으로 거절한 파일만 ruamel(YAML 1.2, go-yaml 과 같은 판 — 순수 파이썬이라 느리다)로 다시 읽는다.
    그 밖의 실패(RecursionError·MemoryError …)는 _ParserLimit — 부르는 쪽이 원시 글자 검사로 보낸다. 건너뛰면 그 파일이
    검사에서 통째로 빠진다 (agent#128 리뷰).
    """
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        return list(yaml.load_all(data, Loader=loader))
    except yaml.YAMLError:
        pass
    except Exception as exc:
        raise _ParserLimit(type(exc).__name__) from None
    try:
        return list(_RuamelYAML(typ="safe", pure=True).load_all(data))
    except _RuamelYAMLError:
        return None
    except Exception as exc:
        raise _ParserLimit(type(exc).__name__) from None


def _binary(head: bytes) -> bool:
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False                                    # UTF-16 글자 파일
    return b"\0" in head


def _encoding(head: bytes) -> str:
    if head.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if head.startswith(b"\xfe\xff"):
        return "utf-16-be"
    return "utf-8"


#: YAML 겹따옴표 이스케이프 — 원시 글자 검사에서도 풀어 본다: 줄 이음(`\` + 개행 + 들여쓰기), `\/`, `\xXX`·`\uXXXX`·`\UXXXXXXXX`
_ESCAPE = re.compile(r"\\\r?\n[ \t]*|\\/|\\x([0-9A-Fa-f]{2})|\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def _unescaped(text: str) -> str:
    def one(found: re.Match[str]) -> str:
        code = found.group(1) or found.group(2) or found.group(3)
        if code is None:
            return "/" if found.group(0) == "\\/" else ""          # `\\/` 는 `/`, 줄 이음은 없앤다
        try:
            return chr(int(code, 16))
        except (ValueError, OverflowError):
            return found.group(0)
    return _ESCAPE.sub(one, text)


def _stream_loopback(file: Path, head: bytes, deadline: float) -> str | None:
    """글자 파일을 원시 글자로 흘려 읽으며 이 컴퓨터 안 주소를 찾는다 — 조각 사이에 걸친 주소도 보게 겹쳐 읽는다.
    이스케이프를 푼 사본도 본다 — YAML 로 읽지 않아도 `\\u002f`·`\\/`·줄 이음으로 숨긴 주소가 빠지지 않게 (agent#128 리뷰)."""
    decoder = codecs.getincrementaldecoder(_encoding(head))(errors="replace")
    tail = ""
    with file.open("rb") as stream:
        while chunk := stream.read(_STREAM_CHUNK):
            if _scan_clock() > deadline:
                raise ScanTimeout(f"{SCAN_BUDGET:g}초")          # 파일 하나도 상한 안에서 끊는다
            text = tail + decoder.decode(chunk)
            if found := _loopback_in(text) or _loopback_in(_unescaped(text)):
                return found
            tail = text[-_STREAM_OVERLAP:]
    return None


def _loopback_in_file(file: Path, deadline: float) -> str | None:
    """파일 하나에서 이 컴퓨터 안 주소를 찾는다. 확장자·이름은 보지 않는다 — kustomize 는 generators 등이 가리키는 파일을
    이름과 상관없이 YAML 로 읽는다. 바이너리는 앞 8KB 만 보고 건너뛴다.

    MAX_SCAN_BYTES 까지는 YAML 로 읽어 **풀린** 문자열(키·값)을 본다 — 원시 글자로 먼저 거르지 않는다. 겹따옴표
    이스케이프(`\\/`·`\\u002f`)·줄 이음은 읽어야 `://` 가 된다. 주석은 보지 않는다. YAML 로 못 읽는 파일은 건너뛴다 —
    kustomize 도 설정으로 못 쓴다(글자로 보면 Helm 템플릿의 localhost 같은 오탐만 는다). 넘는 파일은 _stream_loopback.
    """
    try:
        with file.open("rb") as stream:
            head = stream.read(8192)
            if _binary(head):
                return None
            if file.stat().st_size > MAX_SCAN_BYTES:
                return _stream_loopback(file, head, deadline)
            data = head + stream.read()
    except OSError:
        return None
    try:
        docs = _parse(data)
    except _ParserLimit:
        return _stream_loopback(file, head, deadline)
    return next((found for value in _strings(docs or []) if (found := _loopback_in(value))), None)


def _repo_files(root: Path) -> list[Path]:
    """레포 안 파일 전부(.git 밖, 심볼릭 링크는 따라가지 않는다 — 레포 밖을 가리키는 링크는 렌더도 거절한다)."""
    return sorted(path for path in root.rglob("*")
                  if ".git" not in path.relative_to(root).parts and path.is_file() and not path.is_symlink())


#: 레포마다 훑은 결과. 열쇠는 부르는 쪽이 준 것(레포 보기는 `<레포>@<커밋 sha>` — tarball 은 커밋마다 같다), 없으면 레포
#: 폴더의 (경로, inode, ctime) — PR 길은 받은 레포·고친 사본을 각각 한 번 만들고 고친 뒤에 렌더한다.
#: PR 길의 렌더 비교는 같은 레포로 render() 를 kustomize 루트마다 전·후로 부른다 — 레포 전체를 그만큼 걷지 않게.
#: 시간 초과는 기억하지 않는다 — 그때의 부하 탓일 수 있다
_loopback_scans: OrderedDict[str, str | None] = OrderedDict()
_loopback_lock = threading.Lock()
_LOOPBACK_SCANS = 64


def _scan_key(root: Path) -> str:
    stat = root.stat()
    return f"{root.resolve()}|{stat.st_ino}|{stat.st_ctime_ns}"


def loopback_reference(root: Path, *, scan_key: str | None = None) -> str | None:
    """레포의 파일 어디에든 이 컴퓨터 안 주소가 있으면 그 주소 하나(`scheme://host[:port]`). 프록시로 막지 못하는 곳이라
    build 전에 본다. SCAN_BUDGET 을 넘으면 ScanTimeout.

    kustomize 가 가져오는 주소는 반드시 kustomize 가 YAML 로 읽는 어떤 파일 안에 있다. 그래서 고르지 않고 레포의 모든 파일을
    본다 — 고르는 규칙(이름·확장자·칸)이 새면 그대로 뚫린다. 가져오지 않는 자리(commonAnnotations·인라인 patch 값)의
    주소도 거절한다 — 안전 쪽 오탐이다.
    """
    key = scan_key or _scan_key(root)
    with _loopback_lock:
        if key in _loopback_scans:
            _loopback_scans.move_to_end(key)
            return _loopback_scans[key]
    deadline = _scan_clock() + SCAN_BUDGET
    found = None
    for file in _repo_files(root):
        if _scan_clock() > deadline:
            raise ScanTimeout(f"{SCAN_BUDGET:g}초")
        if found := _loopback_in_file(file, deadline):
            break
    with _loopback_lock:
        _loopback_scans[key] = found
        while len(_loopback_scans) > _LOOPBACK_SCANS:
            _loopback_scans.popitem(last=False)
    return found


def _scan_clock() -> float:
    """시각(초) — 테스트가 바꿔 끼운다."""
    return time.monotonic()


def _offline_env(home: str) -> dict[str, str]:
    """kustomize 를 바깥과 끊는 환경 (모듈 설명). 에이전트 프로세스의 토큰·모델 키도 넘기지 않는다."""
    proxy = {name: OFFLINE_PROXY for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                              "http_proxy", "https_proxy", "all_proxy")}
    return {
        "PATH": os.devnull, "HOME": home, **proxy, "NO_PROXY": "", "no_proxy": "",
        "GIT_ALLOW_PROTOCOL": "none", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


#: kustomize 오류의 까닭 — stderr 원문은 레포 값(patch·생성기 설정의 Secret 값 등)을 그대로 싣는다. 사용자 문구에는 이 분류만
#: 싣고 원문은 서버 로그에만 남긴다 (agent#128 리뷰). 위에 있는 것이 먼저다
_KUSTOMIZE_REASONS = (
    (("security; file", "is not in or below"), "kustomization 폴더 밖의 파일을 가리켜요"),
    (("no such file or directory", "must build at directory", "evalsymlink", "not a valid directory",
      "unable to find one of"), "가리키는 파일·폴더가 레포에 없어요"),
    (("yaml:", "malformedyaml", "could not parse", "error converting yaml", "invalid document"),
     "YAML 모양이 맞지 않아요"),
    (("already registered id", "found multiple"), "같은 리소스가 두 번 들어가요"),
    (("no matches for", "failed to find unique target", "unable to find patch target"),
     "patch 가 가리키는 리소스를 찾지 못했어요"),
)


def _kustomize_reason(stderr: str) -> str:
    lowered = stderr.lower()
    for markers, reason in _KUSTOMIZE_REASONS:
        if any(marker in lowered for marker in markers):
            return reason
    return "kustomize 오류예요 (자세한 내용은 서버 로그)"


def render_kustomize(folder: Path, *, timeout: int = 60, label: str = "") -> list[dict]:
    """label 은 문구에 싣는 폴더 이름(레포 기준)."""
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
        stderr = proc.stderr.strip()[:_STDERR_LIMIT]
        # 원문은 서버 로그에만 — 가져오려던 주소의 userinfo 는 빼고
        logger.warning("kustomize build 가 실패했다 (%s): %s", label or folder.name, _redact_text(stderr))
        where = f" ({label})" if label else ""
        if OFFLINE_PROXY.removeprefix("http://") in stderr or "executable file not found" in stderr:
            raise RenderError(f"레포 밖 주소를 가져오려 해서 렌더하지 못했어요{where} — 렌더할 때는 네트워크를 쓰지 않아요")
        raise RenderError(f"kustomize build 가 실패했어요{where}: {_kustomize_reason(stderr)}")
    return parse_objects(proc.stdout, "kustomize build 결과")


def render(source_type: str, folder: Path, *, root: Path, recurse: bool = False,
           scan_key: str | None = None) -> list[dict]:
    """folder 를 ArgoCD 처럼 렌더링한다. root 는 레포 루트 — folder 가 그 안에 있어야 한다.
    scan_key 는 레포 내용이 같으면 같은 값(레포·커밋 sha) — 이 컴퓨터 안 주소 검사를 그 값마다 한 번만 한다."""
    if source_type == "Directory":
        return render_plain(folder, recurse=recurse, root=root)
    if source_type == "Kustomize":
        try:
            local = loopback_reference(root, scan_key=scan_key)
        except ScanTimeout as exc:
            raise RenderTimeout(f"레포 검사 시간({exc})이 넘어 렌더하지 않아요 — 이 컴퓨터 안 주소를 다 확인하지 못했어요") from None
        if local is not None:
            raise RenderError(f"레포 안 파일에 이 컴퓨터 안 주소({local})가 있어 렌더하지 않아요")
        remote = remote_reference(folder, root)
        if remote is not None:
            raise RenderError(f"레포 밖 주소({_redact(remote)})를 가져오는 Kustomize 는 렌더하지 않아요 — 렌더할 때는 네트워크를 쓰지 않아요")
        label = folder.resolve().relative_to(root.resolve()).as_posix() if folder.resolve().is_relative_to(
            root.resolve()) else folder.name
        return render_kustomize(folder, label=label)
    raise RenderError(f"{source_type} 형식은 아직 렌더링하지 못해요")
