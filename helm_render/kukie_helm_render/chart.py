"""받은 차트를 안전하게 풀고 검사한다 — helm 을 돌리기 전에 위험한 것을 막는다.

helm 의 폴더 로더는 심볼릭 링크를 따라가 차트 밖 파일을 템플릿으로 읽는다. 그래서 링크는 어떤 것이든 풀지 않는다.
스키마(values.schema.json)의 `$ref` · `$schema` 는 파일과 http(s) 주소를 읽는다(helm 4.2 로 직접 확인). 문서 안(`#…`)과
라이브러리에 든 메타 스키마만 받는다. 안쪽 charts/*.tgz 도 helm 이 읽으므로 같은 검사를 한다.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from kukie_helm_render.config import Settings

SCHEMA_FILE = "values.schema.json"
_REFERENCE_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")
#: 네트워크 없이 helm 이 아는 메타 스키마. 뒤의 `#` 는 떼고 견준다
_KNOWN_META_SCHEMAS = frozenset({
    "http://json-schema.org/schema",
    "https://json-schema.org/schema",
    "http://json-schema.org/draft-04/schema",
    "https://json-schema.org/draft-04/schema",
    "http://json-schema.org/draft-06/schema",
    "https://json-schema.org/draft-06/schema",
    "http://json-schema.org/draft-07/schema",
    "https://json-schema.org/draft-07/schema",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
})


class ChartRejected(Exception):
    """helm 을 돌리지 않고 거절한다. reason 은 기계가 읽는 이름, 문구는 차트 안의 경로만 담는다 (값은 담지 않는다)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class _Budget:
    """차트 하나가 쓸 수 있는 파일 수와 푼 크기. 안쪽 tgz 까지 함께 센다."""

    def __init__(self, settings: Settings):
        self.bytes_left = settings.max_unpacked_bytes
        self.files_left = settings.max_chart_files

    def spend_bytes(self, size: int) -> None:
        self.bytes_left -= size
        if self.bytes_left < 0:
            raise ChartRejected("chart_too_large", "차트를 풀면 크기 제한을 넘어요")

    def spend_file(self) -> None:
        self.files_left -= 1
        if self.files_left < 0:
            raise ChartRejected("chart_too_large", "차트의 파일 수가 제한을 넘어요")


class _BudgetReader(io.RawIOBase):
    """푼 바이트를 읽는 대로 센다. tar 머리(pax · 긴 이름)도 여기를 지나므로 머리로 메모리를 채울 수 없다."""

    def __init__(self, raw: IO[bytes], budget: _Budget):
        self._raw = raw
        self._budget = budget

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._raw.read(len(buffer))
        self._budget.spend_bytes(len(data))
        buffer[:len(data)] = data
        return len(data)


@dataclass(frozen=True)
class _Entry:
    parts: tuple[str, ...]
    member: tarfile.TarInfo


def _checked_entry(member: tarfile.TarInfo) -> _Entry | None:
    """링크 · 장치 · 차트 밖 경로면 거절한다. 폴더는 None — 파일을 만들 때 부모를 함께 만든다."""
    if member.issym() or member.islnk():
        raise ChartRejected("chart_has_link", f"차트에 링크가 있어요: {member.name[:200]}")
    if not (member.isreg() or member.isdir()) or member.issparse():
        raise ChartRejected("chart_has_special_file", f"차트에 일반 파일이 아닌 항목이 있어요: {member.name[:200]}")
    name = member.name
    path = PurePosixPath(name)
    if "\\" in name or "\0" in name or path.is_absolute() or ".." in path.parts:
        raise ChartRejected("chart_path_outside", f"차트 밖을 가리키는 경로가 있어요: {name[:200]}")
    if member.isdir():
        return None
    if len(path.parts) < 2:
        raise ChartRejected("not_a_chart", "차트 파일은 한 폴더 아래에 있어야 해요")
    return _Entry(path.parts, member)


def _members(archive: IO[bytes], budget: _Budget):
    """압축된 tar 를 순서대로 읽어 (경로 조각, 내용을 읽는 파일) 을 내놓는다. 받은 크기를 믿지 않고 읽으면서 센다."""
    reader = io.BufferedReader(_BudgetReader(gzip.GzipFile(fileobj=archive), budget), buffer_size=64 * 1024)
    with tarfile.open(fileobj=reader, mode="r|") as tar:
        for member in tar:
            budget.spend_file()
            entry = _checked_entry(member)
            if entry is not None:
                yield entry.parts, tar.extractfile(entry.member)


def check_schema(data: bytes, where: str) -> None:
    """`$ref` 류는 문서 안(`#…`)만, `$schema` 는 아는 메타 스키마만 받는다. 같은 키가 둘이어도 모두 본다."""
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_Pairs)
    except (ValueError, RecursionError) as exc:
        raise ChartRejected("schema_unreadable", f"{where} 를 JSON 으로 읽지 못했어요") from exc
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, _Pairs):
            for key, value in node:
                if isinstance(value, str):
                    if key in _REFERENCE_KEYS and not value.startswith("#"):
                        raise ChartRejected("schema_external_ref", f"{where} 가 바깥 파일이나 주소를 가리켜요")
                    if key == "$schema" and value.removesuffix("#") not in _KNOWN_META_SCHEMAS:
                        raise ChartRejected("schema_external_ref", f"{where} 의 $schema 가 아는 메타 스키마가 아니에요")
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)


class _Pairs(list):
    """JSON 객체를 키 · 값 쌍 그대로 둔다. dict 로 만들면 같은 키의 앞쪽 값이 사라져 검사를 비켜 갈 수 있다."""


def _read_limited(stream: IO[bytes], limit: int, reason: str, what: str) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ChartRejected(reason, f"{what} 가 제한보다 커요")
    return data


def _is_subchart_archive(parts: tuple[str, ...]) -> bool:
    return parts[-1].endswith(".tgz") and "charts" in parts[:-1]


def _check_nested(data: bytes, where: str, settings: Settings, budget: _Budget, depth: int) -> None:
    """charts/ 안의 tgz — helm 이 직접 읽으므로 풀지 않고 같은 규칙으로 읽기만 한다."""
    if depth > settings.max_chart_depth:
        raise ChartRejected("chart_too_deep", f"{where}: 하위 차트가 너무 깊어요")
    for parts, stream in _members(io.BytesIO(data), budget):
        name = "/".join(parts)
        if parts[-1] == SCHEMA_FILE:
            check_schema(_read_limited(stream, settings.max_schema_bytes, "schema_too_large", name), name)
        elif _is_subchart_archive(parts):
            nested = _read_limited(stream, settings.max_chart_bytes, "chart_too_large", name)
            _check_nested(nested, name, settings, budget, depth + 1)


def unpack_chart(archive: bytes, dest: Path, settings: Settings) -> Path:
    """tgz 를 dest 아래에 풀고 차트 폴더를 돌려준다. 링크를 만들지 않으므로 푼 폴더에는 링크가 없다."""
    if len(archive) > settings.max_chart_bytes:
        raise ChartRejected("chart_too_large", "차트 파일이 제한보다 커요")
    try:
        root_name = _unpack(archive, dest, settings)
    except (tarfile.TarError, gzip.BadGzipFile, zlib.error, EOFError, OSError, ValueError, RecursionError) as exc:
        raise ChartRejected("chart_unreadable", "차트 압축 파일을 읽지 못했어요") from exc
    if root_name is None or not (dest / root_name / "Chart.yaml").is_file():
        raise ChartRejected("not_a_chart", "Chart.yaml 이 없어요")
    return dest / root_name


def _unpack(archive: bytes, dest: Path, settings: Settings) -> str | None:
    budget = _Budget(settings)
    root_name: str | None = None
    for parts, stream in _members(io.BytesIO(archive), budget):
        if root_name is None:
            root_name = parts[0]
        elif parts[0] != root_name:
            raise ChartRejected("not_a_chart", "차트 파일은 한 폴더 아래에 있어야 해요")
        name = "/".join(parts[1:])
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 같은 경로가 두 번 오면 앞의 파일을 덮어쓰지 않고 거절한다 (O_EXCL)
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            if parts[-1] == SCHEMA_FILE:
                data = _read_limited(stream, settings.max_schema_bytes, "schema_too_large", name)
                check_schema(data, name)
                file.write(data)
            elif _is_subchart_archive(parts[1:]):
                data = _read_limited(stream, settings.max_chart_bytes, "chart_too_large", name)
                _check_nested(data, name, settings, budget, 1)
                file.write(data)
            else:
                shutil.copyfileobj(stream, file)
    return root_name
