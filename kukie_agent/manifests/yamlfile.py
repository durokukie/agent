"""레포의 YAML 을 주석·모양 그대로 고친다 — ruamel.yaml 왕복(round-trip).

pyyaml 로 읽고 다시 쓰면 주석이 사라지고 `{app: web}` 같은 모양이 풀려 PR diff 가 파일 전체가 된다.
들여쓰기는 파일에서 짐작한다 — 쿠버네티스 매니페스트는 대개 mapping 2칸, 목록은 `  - ` (부모보다 2칸 안쪽).
"""
from __future__ import annotations

import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

# 키 바로 아래 목록의 `-` 가 키와 같은 칸이면 들여쓰지 않는 목록이다
_SEQUENCE = re.compile(r"^( *)[^\s#-][^\n]*:[ \t]*\n(?:[ \t]*#[^\n]*\n)*( *)- ", re.M)


class YamlFile:
    def __init__(self, text: str):
        self.text = text
        found = _SEQUENCE.search(text)
        offset = 0 if found and len(found.group(1)) == len(found.group(2)) else 2
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.width = 4096
        self._yaml.indent(mapping=2, sequence=offset + 2, offset=offset)
        self._yaml.explicit_start = text.lstrip().startswith("---")
        try:
            self.docs: list[Any] = list(self._yaml.load_all(text))
        except YAMLError as exc:
            raise ValueError(f"YAML 로 읽지 못했어요: {exc}") from exc

    def dump(self) -> str:
        out = io.StringIO()
        self._yaml.dump_all(self.docs, out)
        return out.getvalue()

    def keeps_shape(self) -> bool:
        """고치지 않고 다시 써도 글자 하나 안 바뀌는지 — 아니면 PR diff 에 모양 변화가 섞인다."""
        return YamlFile(self.text).dump() == self.text


def dump_new(doc: Any, header: str = "") -> str:
    """새 파일 — 쿠버네티스 매니페스트 모양(mapping 2칸, 목록 `  - `)으로."""
    yaml_ = YAML()
    yaml_.width = 4096
    yaml_.indent(mapping=2, sequence=4, offset=2)
    out = io.StringIO()
    yaml_.dump(doc, out)
    return header + out.getvalue()


def remove_item(parent: Any, key: str, index: int) -> None:
    """`parent[key]` 목록의 index 번째 항목을 지운다 — 주석은 그 항목 위의 것만 같이 지운다.

    ruamel 은 항목 사이 주석을 앞 항목(첫 항목이면 목록 시작)에 매단다. 그대로 지우면 아래 항목의 주석이 같이 사라지고,
    위 주석은 엉뚱한 항목 위에 남는다. 그래서 지울 항목에 매달린 주석(= 다음 항목 위 주석)을 앞자리로 옮긴다.
    """
    seq = parent[key]
    item = seq[index]
    below = None
    if isinstance(item, dict) and len(item):
        below = item.ca.items.get(list(item)[-1], [None] * 4)[2]
    del seq[index]
    if index > 0:
        previous = seq[index - 1]
        if isinstance(previous, dict) and len(previous):
            slots = previous.ca.items.setdefault(list(previous)[-1], [None] * 4)
            slots[2] = below
        return
    if key in parent.ca.items:
        parent.ca.items[key][3] = None
    seq.ca.comment = None
    if below is not None:
        lines = [line.strip() for line in below.value.splitlines() if line.strip().startswith("#")]
        if lines:
            column = len(below.value.lstrip("\n")) - len(below.value.lstrip("\n").lstrip(" "))
            seq.yaml_set_start_comment("\n".join(line[1:].lstrip() for line in lines), indent=column)


def remove_key(parent: Any, key: str) -> None:
    """맵의 칸 하나를 지운다 — 주석은 remove_item 처럼 그 칸 위의 것만 같이 지운다.

    ruamel 은 칸 사이 주석을 앞 칸의 가장 깊은 마지막 값에 매단다. 그대로 지우면 다음 칸 위 주석이 같이 사라진다.
    첫 칸을 지울 때 그 위 주석은 윗 맵에 매달려 있어 남는다 — 지워지는 것보다 남는 편이 낫다.
    """
    holder, slot_key, at = _tail(parent, key)
    slot = holder.ca.items.get(slot_key)
    below = slot[at] if slot else None
    keys = list(parent)
    position = keys.index(key)
    del parent[key]
    if position > 0:
        holder, slot_key, at = _tail(parent, keys[position - 1])
        holder.ca.items.setdefault(slot_key, [None] * 4)[at] = below
    elif below is not None and len(parent):
        lines = [line.strip()[1:].lstrip() for line in below.value.splitlines() if line.strip().startswith("#")]
        if lines:
            column = len(below.value.lstrip("\n")) - len(below.value.lstrip("\n").lstrip(" "))
            parent.yaml_set_comment_before_after_key(list(parent)[0], before="\n".join(lines), indent=column)


def _tail(node: Any, key: Any) -> tuple[Any, Any, int]:
    """node[key] 뒤에 오는 주석이 매달리는 자리 — (주석을 가진 객체, ca.items 키, 칸 번호)."""
    value = node[key]
    if isinstance(value, CommentedMap) and len(value):
        return _tail(value, list(value)[-1])
    if isinstance(value, CommentedSeq) and len(value):
        last = value[-1]
        if isinstance(last, CommentedMap) and len(last):
            return _tail(last, list(last)[-1])
        return value, len(value) - 1, 0
    return node, key, 2
