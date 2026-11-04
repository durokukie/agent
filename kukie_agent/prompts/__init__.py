"""프롬프트 원문과 그 버전 — 어느 프롬프트로 돈 run 인지 trace 에서 가르기 위해.

프롬프트는 이 폴더의 .md 파일이고, 코드는 이름으로 읽기만 한다 (load). 버전은 두 가지다.
  - 파일 지문(files): git 이 파일마다 계산하는 blob ID 와 같은 값. `git log --find-object=<지문>` 이
    그 내용이 들어간 커밋을 찾아 주므로, 지문과 내용을 짝지어 적어 둘 장부가 따로 없다
  - 합친 버전(version, p-xxxxxxxx): 파일 지문들을 이름 순서로 이어 다시 해시한 값. 모델이 보는 건 합친
    프롬프트라 비교·그룹은 이 값으로 하고, 어느 파일이 바뀌었는지는 files 로 본다
프롬프트와 상관없는 코드 수정에는 둘 다 그대로다(커밋 SHA 와 다른 점). 이미지 안에는 git 이 없어도 같은 값을
계산한다. 대화마다 달라지는 부분(agent.add_target 의 context·namespace)은 파일에 없으므로 버전에 들지 않는다.
툴 설명(docstring)도 모델에 가지만 여기 들지 않는다.
"""
from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


def load(name: str) -> str:
    """프롬프트 원문. 파일 끝 줄바꿈은 뗀다 — 에디터가 붙이는 마지막 줄바꿈이 모델 입력에 섞이지 않게."""
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def blob_id(data: bytes) -> str:
    """`git hash-object` 와 같은 값 (sha1 over "blob <길이>\\0" + 내용)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def compute(directory: Path) -> tuple[str, dict[str, str]]:
    """(합친 버전, {파일 이름: blob ID}) — 파일은 이름 순. 합친 버전은 전체 blob ID 로 계산한다."""
    files = {path.name: blob_id(path.read_bytes()) for path in sorted(directory.glob("*.md"))}
    joined = "\n".join(f"{name}={blob}" for name, blob in files.items())
    return "p-" + hashlib.sha256(joined.encode()).hexdigest()[:8], files


@cache
def resource_attributes() -> dict[str, str]:
    """trace resource 에 붙일 값. 지문은 앞 12자리 — git 이 짧은 ID 로도 찾는다."""
    version, files = compute(_DIR)
    return {
        "kukie.prompt.version": version,
        "kukie.prompt.files": ",".join(f"{name}={blob[:12]}" for name, blob in files.items()),
    }
