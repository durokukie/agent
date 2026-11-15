"""경로 지정 변경(what=patch) — 정해진 넷 밖의 칸을 "이 경로를 이 값으로 / 이 경로를 지워라" 로 고친다 (DURO-249).

AI 는 무엇을 바꿀지(경로와 값)만 낸다. 어느 문서를 고칠지·고친 결과가 맞는지는 코드가 본다:
  - 기대값 = 지금 렌더 결과에 같은 패치를 적용한 것. 고친 뒤 렌더 결과가 이것과 칸 하나까지 같아야 PR 이 열린다 (change.verify)
  - 그래서 칸마다 따로 짠 계산 코드가 필요 없다 — 경로를 따라가 적용하는 규칙 하나를 렌더 결과(dict)와 레포 문서(ruamel)에 같이 쓴다

경로 모양
    spec.template.spec.containers[api].readinessProbe        점으로 키를 잇고, 이름(name) 있는 목록 항목은 [이름]
    metadata.labels["app.kubernetes.io/part-of"]              점·슬래시가 든 키는 ["…"]
  번호로 목록 항목을 가리키지 않는다 — 순서가 바뀌면 다른 항목을 고친다. 이름 없는 목록(args · tolerations · ports …)은
  그 목록을 통째로 set 한다.

못 하는 것 (거절)
  - 리소스를 가리키는 칸: apiVersion · kind · metadata.name · metadata.namespace — 바꾸면 다른 리소스가 된다 (DURO-252)
  - metadata 아래는 labels · annotations 만. status 는 git 에 두는 칸이 아니다
  - ArgoCD 가 리소스를 알아보는 표시(tracking-id 어노테이션 · app.kubernetes.io/instance 레이블) — 바꾸면 ArgoCD 가 놓친다
  - 만든 뒤 바꿀 수 없는 칸(Deployment selector · immutable ConfigMap 등), 셀렉터(matchLabels · matchExpressions)와
    맞지 않게 되는 파드 레이블 — 머지해도 쿠버네티스가 거절해 sync 가 실패한다. 막는 기준은 경로가 아니라 결과다: spec 을
    통째로 set 해도 그 값들이 그대로면 된다. 경로를 바꿔도 풀리지 않아 다시 고르게 하지 않고 툴 실패로 끝낸다
  - HPA 가 맡은 레플리카 · 평문 비밀 값 — 넷(replicas · env)과 같은 규칙 (change.py)
  - 위 칸이 없는 경로 — 오타로 엉뚱한 칸이 생기지 않게. 위 칸부터 통째로 set 하게 돌려보낸다

위험해 보이는 값(privileged · hostPath · hostNetwork · serviceAccountName …)은 막지 않고 승인 카드에 경고로 올린다 —
새 리소스와 같은 목록(risks.py)이다. 막지 않는 이유는 새 리소스와 같다: 전체 권한 클러스터에서는 같은 변경을
kubectl 로도 할 수 있어, PR 길만 막으면 기록이 남지 않는 길로 빠진다 (블로그 GitOps 02편 7.2).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

import yaml
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from kukie_agent.manifests import compare
from kukie_agent.manifests.compare import FieldPath, brief, show
from kukie_agent.manifests.models import Patch, PatchOperation
from kukie_agent.manifests.yamlfile import remove_item, remove_key

#: ArgoCD 가 리소스를 알아보는 표시 (argocd.py). ArgoCD 가 apply 때 채우는 칸이라 git 에 적을 까닭이 없다.
#: app.kubernetes.io/instance 레이블은 막지 않는다 — label 추적이면 ArgoCD 가 apply 때 앱 이름으로 덮어쓰고,
#: annotation 추적(v3 기본)이면 평범한 레이블이다. 어느 쪽인지는 레포만 봐서는 모른다
TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
#: 만든 뒤 바꿀 수 없는 칸 — 종류별. 바꾸면 apply 가 거절돼 sync 가 실패한다 (지우고 다시 만들어야 한다)
IMMUTABLE: dict[str, tuple[FieldPath, ...]] = {
    "Deployment": (("spec", "selector"),),
    "ReplicaSet": (("spec", "selector"),),
    "DaemonSet": (("spec", "selector"),),
    "Job": (("spec", "selector"), ("spec", "template")),
    "Service": (("spec", "clusterIP"), ("spec", "clusterIPs")),
    "PersistentVolumeClaim": (("spec", "accessModes"), ("spec", "storageClassName"), ("spec", "volumeName"),
                              ("spec", "volumeMode"), ("spec", "selector"), ("spec", "dataSource"),
                              ("spec", "dataSourceRef")),
}

#: StatefulSet spec 중 만든 뒤에도 바꿀 수 있는 칸 — 나머지는 apply 가 거절한다 (허용 목록)
_STATEFULSET_MUTABLE = frozenset({"replicas", "template", "updateStrategy", "persistentVolumeClaimRetentionPolicy",
                                  "minReadySeconds", "ordinals"})

#: 셀렉터가 파드 템플릿 레이블을 골라야 하는 종류 — 맞지 않으면 apply 가 거절된다
_SELECTED = ("Deployment", "ReplicaSet", "DaemonSet", "StatefulSet")

_SEGMENT = re.compile(r'\.?(?:([A-Za-z0-9_$-][A-Za-z0-9_$-]*)|\["((?:[^"\\]|\\.)+)"\]|\[([^\]"]+)\])')


class PathError(ValueError):
    """경로가 틀렸다 — AI 가 경로를 고쳐 다시 내면 된다 (retry)."""


class Key(str):
    """dict 키 조각."""


class Named(str):
    """이름 있는 목록 항목 조각 — `[api]`."""


Parsed = list[tuple[PatchOperation, tuple["Key | Named", ...]]]


@dataclass(frozen=True)
class Applied:
    """prepare 가 한 번 읽은 경로와 기대값(지금 렌더 결과에 패치만 적용한 것)."""
    parsed: Parsed
    want: dict


def parse(path: str) -> tuple[Key | Named, ...]:
    if not path or path.startswith(".") or path.endswith("."):
        raise PathError(f"경로 '{path}' 모양이 아니에요")
    parts: list[Key | Named] = []
    at = 0
    while at < len(path):
        found = _SEGMENT.match(path, at)
        if found is None or (at > 0 and found.group(0)[0] not in ".[") or (at == 0 and found.group(0).startswith(".")):
            raise PathError(f"경로 '{path}' 의 '{path[at:]}' 부분을 읽지 못했어요. 키는 점으로 잇고, "
                            "이름 있는 목록 항목은 [이름], 점·슬래시가 든 키는 [\"키\"] 로 적어요")
        plain_key, quoted_key, name = found.groups()
        if name is not None:
            if name.strip().isdigit():
                raise PathError(f"경로 '{path}' 에 번호([{name}])가 있어요 — 목록 항목은 이름으로만 가리켜요. "
                                "이름 없는 목록은 그 목록을 통째로 set 하세요")
            parts.append(Named(name))
        else:
            parts.append(Key(plain_key if plain_key is not None else quoted_key.replace('\\"', '"')))
        at = found.end()
    if not isinstance(parts[0], Key):
        raise PathError(f"경로 '{path}' 는 키로 시작해야 해요")
    return tuple(parts)


def field_path(parts: tuple[Key | Named, ...]) -> FieldPath:
    """compare 의 경로 모양으로 — `[이름]` 은 그대로 조각 하나."""
    return tuple(compare.item(p) if isinstance(p, Named) else str(p) for p in parts)


# ── 고칠 수 있는 경로인지 ────────────────────────────────


def check_paths(change: Patch) -> Parsed:
    """경로 모양과 고칠 수 없는 칸을 본다. 틀리면 PathError."""
    parsed = []
    seen: list[FieldPath] = []
    for operation in change.operations:
        parts = parse(operation.path)
        head = str(parts[0])
        if head in ("apiVersion", "kind"):
            raise PathError(f"{head} 는 바꿀 수 없어요 — 바꾸면 다른 리소스가 돼요")
        if head == "status":
            raise PathError("status 는 쿠버네티스가 채우는 칸이라 git 에서 고치지 않아요")
        if head == "metadata":
            if len(parts) < 2 or str(parts[1]) not in ("labels", "annotations"):
                raise PathError("metadata 아래는 labels · annotations 만 바꿀 수 있어요. 이름 · 네임스페이스를 바꾸는 것은 "
                                "리소스를 지우고 새로 만드는 일이라 아직 PR 로 못 해요")
        path = field_path(parts)
        for other in seen:
            if compare.under(path, other) or compare.under(other, path):
                raise PathError(f"'{show(path)}' 와 '{show(other)}' 가 겹쳐요 — 한 칸은 한 번만 바꿔 주세요")
        seen.append(path)
        parsed.append((operation, parts))
    return parsed


def guarded(kind: str, before: dict, after: dict) -> str | None:
    """지금 → 기대값에서 바뀌면 안 되는 칸이 바뀌었으면 그 이유."""
    path = ("metadata", "annotations", TRACKING_ANNOTATION)
    if compare.value_at(before, path) != compare.value_at(after, path):
        return (f"metadata.annotations[\"{TRACKING_ANNOTATION}\"] 는 ArgoCD 가 이 리소스를 알아보는 표시예요 — "
                "ArgoCD 가 채우는 칸이라 git 에 적지 않아요")
    if kind == "Pod":
        changed = [p for p in compare.diff_paths(before.get("spec"), after.get("spec"))
                   if not _pod_mutable(p)]
        if changed:
            return (f"Pod 의 spec.{show(changed[0])} 는 만든 뒤 바꿀 수 없는 칸이에요 — 머지해도 쿠버네티스가 거절해 sync 가 "
                    "실패해요. Pod 는 이미지 · activeDeadlineSeconds 만 바꿀 수 있어요. Deployment 로 옮기는 것을 권해요")
    if kind == "StatefulSet":
        fixed = sorted({str(p[0]) for p in compare.diff_paths(before.get("spec"), after.get("spec"))} - _STATEFULSET_MUTABLE)
        if fixed:
            return (f"StatefulSet 의 spec.{fixed[0]} 는 만든 뒤 바꿀 수 없는 칸이에요 — 머지해도 쿠버네티스가 거절해 sync 가 "
                    "실패해요. StatefulSet 은 replicas · template · updateStrategy · minReadySeconds 등만 바꿀 수 있어요")
    for path in IMMUTABLE.get(kind, ()):
        if compare.value_at(before, path) != compare.value_at(after, path):
            return (f"{kind} 의 {show(path)} 는 만든 뒤 바꿀 수 없는 칸이에요 — 머지해도 쿠버네티스가 거절해 sync 가 "
                    "실패해요. 지우고 다시 만들어야 해서 아직 PR 로 못 해요")
    if kind in _SELECTED:
        selector = compare.value_at(after, ("spec", "selector"))
        labels = compare.value_at(after, ("spec", "template", "metadata", "labels"))
        if isinstance(selector, dict) and not selects(selector, labels if isinstance(labels, dict) else {}):
            return (f"spec.template.metadata.labels 가 spec.selector({brief(selector)})와 맞지 않게 돼요 — "
                    "쿠버네티스가 거절해 sync 가 실패해요. 셀렉터가 고르는 레이블은 남겨 두세요")
    if before.get("immutable") is True:
        if after.get("immutable") is not True:
            return f"이 {kind} 는 immutable 이라 immutable 칸을 풀 수 없어요 — 새 이름으로 만들어야 해요"
        for section in ("data", "binaryData"):
            if before.get(section) != after.get(section):
                return f"이 {kind} 는 immutable 이라 {section} 를 바꿀 수 없어요 — 새 이름으로 만들어야 해요"
    return None


def _pod_mutable(path: FieldPath) -> bool:
    """Pod spec 중 만든 뒤에도 바꿀 수 있는 칸 — 컨테이너 이미지와 activeDeadlineSeconds (tolerations 는 더하기만 돼 막는다)."""
    if path == ("activeDeadlineSeconds",):
        return True
    return len(path) == 3 and path[0] in ("containers", "initContainers") and path[2] == "image"


def selects(selector: dict, labels: dict) -> bool:
    """레이블 셀렉터(matchLabels + matchExpressions)가 이 레이블을 고르는지. 모르는 연산자는 고르지 않는 것으로 본다 —
    쿠버네티스도 받지 않는다."""
    match = selector.get("matchLabels") or {}
    if not isinstance(match, dict) or any(labels.get(k) != v for k, v in match.items()):
        return False
    for rule in selector.get("matchExpressions") or []:
        if not isinstance(rule, dict):
            return False
        key, operator, values = rule.get("key"), rule.get("operator"), rule.get("values") or []
        if operator == "In":
            ok = key in labels and labels[key] in values
        elif operator == "NotIn":
            ok = key not in labels or labels[key] not in values
        elif operator == "Exists":
            ok = key in labels
        elif operator == "DoesNotExist":
            ok = key not in labels
        else:
            ok = False
        if not ok:
            return False
    return True


def touches(change: Patch, path: FieldPath) -> bool:
    """패치가 이 칸(또는 그 위·아래 칸)을 건드리는지 — 경로가 틀리면 False (prepare 가 따로 거절한다)."""
    for operation in change.operations:
        try:
            mine = field_path(parse(operation.path))
        except PathError:
            continue
        if compare.under(mine, path) or compare.under(path, mine):
            return True
    return False


# ── 적용 — 렌더 결과(dict)와 레포 문서(ruamel) 둘 다 ──────


def apply(obj: Any, parsed: Parsed, *, ruamel: bool = False) -> None:
    """패치를 그 자리에서 적용한다. 경로가 맞지 않으면 PathError — 렌더 결과에 먼저 적용해 걸러 낸다."""
    for operation, parts in parsed:
        parent = _walk(obj, parts[:-1], operation.path)
        last = parts[-1]
        if operation.op == "set":
            value = _block(operation.value) if ruamel else copy.deepcopy(operation.value)
            if isinstance(last, Named):
                if not (isinstance(value, dict) and value.get("name") == str(last)):
                    raise PathError(f"'{operation.path}' 는 이름이 {last} 인 항목이에요 — 새 값에도 name: {last} 가 있어야 해요")
                parent[_index(parent, last, operation.path)] = value
            else:
                if not isinstance(parent, dict):
                    raise PathError(f"'{operation.path}' 의 위 칸이 맵(키: 값)이 아니에요")
                parent[str(last)] = value
        else:
            if isinstance(last, Named):
                index = _index(parent, last, operation.path)
                if ruamel:
                    _remove_seq_item(obj, parts[:-1], index)
                else:
                    del parent[index]
            else:
                if not isinstance(parent, dict) or str(last) not in parent:
                    raise PathError(f"'{operation.path}' 칸이 지금 없어요 — 지울 게 없어요")
                if ruamel:
                    remove_key(parent, str(last))
                else:
                    del parent[str(last)]


def _walk(obj: Any, parts: tuple[Key | Named, ...], path: str) -> Any:
    here = obj
    for depth, part in enumerate(parts):
        if isinstance(part, Named):
            if not isinstance(here, list):
                raise PathError(f"'{path}' 의 {show(field_path(parts[:depth + 1]))} 는 목록이 아니에요")
            here = here[_index(here, part, path)]
        else:
            if not isinstance(here, dict) or str(part) not in here:
                where = show(field_path(parts[:depth + 1]))
                raise PathError(f"'{path}' 의 위 칸 {where} 가 지금 없어요 — 오타가 아니라면 있는 위 칸부터 통째로 set 하세요"
                                f" (예: {where} 를 새 값으로)")
            here = here[str(part)]
    return here


def _index(items: Any, name: Named, path: str) -> int:
    if not isinstance(items, list):
        raise PathError(f"'{path}' 의 [{name}] 앞 칸이 목록이 아니에요")
    found = [i for i, item in enumerate(items) if isinstance(item, dict) and item.get("name") == str(name)]
    if len(found) != 1:
        names = [str(item.get("name")) for item in items if isinstance(item, dict) and "name" in item]
        if not names:
            raise PathError(f"'{path}' 의 목록 항목에 이름(name)이 없어요 — 그 목록을 통째로 set 하세요")
        raise PathError(f"'{path}' 에서 이름이 {name} 인 항목이 {len(found)}개예요. 있는 이름: {', '.join(names)}")
    return found[0]


def _remove_seq_item(doc: Any, list_parts: tuple[Key | Named, ...], index: int) -> None:
    """ruamel 목록 항목 지우기 — 주석은 그 항목 위의 것만 같이 지운다 (yamlfile.remove_item)."""
    holder = _walk(doc, list_parts[:-1], "")
    last = list_parts[-1]
    if isinstance(last, Key) and isinstance(holder, dict):
        remove_item(holder, str(last), index)
    else:
        del _walk(doc, list_parts, "")[index]


def _block(value: Any) -> Any:
    """새 값을 레포 모양(block)으로 쓰게 ruamel 자료형으로 바꾼다."""
    if isinstance(value, dict):
        return CommentedMap((_scalar(str(k)), _block(v)) for k, v in value.items())
    if isinstance(value, list):
        return CommentedSeq(_block(v) for v in value)
    if isinstance(value, str):
        return _scalar(value)
    return value


to_yaml = _block


#: go-yaml v2(ArgoCD · sigs.k8s.io/yaml)가 참거짓으로 읽는 낱말 — PyYAML 은 y·n 을 문자열로 읽어 렌더 비교로는 안 보인다
_GO_YAML_BOOLS = frozenset({"y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO", "on", "On", "ON",
                            "off", "Off", "OFF", "true", "True", "TRUE", "false", "False", "FALSE"})


def _scalar(value: str) -> str:
    """ruamel 은 YAML 1.2 라 yes·on·off·y 를 따옴표 없이 쓴다. 렌더(PyYAML)와 ArgoCD(go-yaml)는 1.1 이라 참거짓으로
    읽는다 — 다시 읽어 같은 문자열이 아니게 될 낱말은 따옴표로 쓴다 (키도 같다)."""
    if "\n" not in value and (value in _GO_YAML_BOOLS or _read_back(value) != value):
        return DoubleQuotedScalarString(value)
    return value


def _read_back(value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return None


def prepare(obj: dict, change: Patch) -> Applied:
    """경로를 읽고, 지금 렌더 결과에 패치만 적용한 기대값을 만든다. 경로가 틀리면 PathError."""
    parsed = check_paths(change)
    want = copy.deepcopy(obj)
    apply(want, parsed)
    return Applied(parsed, want)
