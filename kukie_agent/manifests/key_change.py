"""키가 바뀌는 변경 — 리소스 삭제 · 네임스페이스 변경 (DURO-252).

렌더 결과의 객체 키(apiVersion, kind, namespace, name)가 사라지거나 바뀌는 변경이다. 다른 변경의 렌더 비교(change._compare_app)는
"대상 키가 그대로 있고 칸만 바뀐다" 를 요구해 이런 변경을 반드시 거절한다. 그래서 규칙을 따로 둔다.

삭제
  렌더 비교: 대상 키 하나만 사라지고 나머지 객체는 한 칸도 안 바뀐다
  plain: 한 파일에 문서가 여럿이면 그 문서만 뺀다(ruamel, 주석·모양 유지 — 문서 위 주석은 그 문서와 같이 빠진다), 하나뿐이면 파일을 지운다
  Kustomize: 이 overlay 폴더의 파일에 있으면 그 문서를 빼고, 파일에 그것만 있으면 `resources:` 줄과 파일을 지운다.
    base(다른 폴더)에서 오면 거절 — 거기서 빼면 같은 base 를 쓰는 다른 환경에서도 사라진다
    overlay 의 patch 가 그 리소스를 가리키면 거절 — 리소스만 빼면 patch 가 갈 곳이 없어 렌더가 깨진다
  승인 카드: 실제로 지워지는지(앱 prune · 리소스 Prune=false 등)와 되돌려도 데이터가 안 돌아오는 종류를 경고로 올린다

네임스페이스 변경 (삭제 위에 얹는다 — 쿠버네티스 입장에서는 옛 것 삭제 + 새 것 생성)
  렌더 비교: 옛 키는 사라지고 새 키가 생기며, metadata.namespace 말고는 내용이 같다
  옮겨 갈 곳을 앱의 AppProject 가 허락해야 한다 (DURO-240 project_denied 와 같은 규칙)
  옮겨 갈 곳이 앱 기본 네임스페이스(destination)면 metadata.namespace 줄을 지운다 — 그 줄이 없으면 앱 기본으로 간다
  Kustomize 의 `namespace:` 가 네임스페이스를 정하면 거절 — 그걸 바꾸면 앱 전체가 옮겨진다
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kukie_agent import argocd
from kukie_agent.manifests import compare, risks
from kukie_agent.manifests.compare import Key
from kukie_agent.manifests.models import Delete, MoveNamespace, Refused, RiskWarning
from kukie_agent.manifests.render import kustomization_file, plain_files, resolve_inside
from kukie_agent.manifests.workload import dig
from kukie_agent.manifests.yamlfile import YamlFile, remove_item, remove_key

if TYPE_CHECKING:
    from kukie_agent.manifests.change import Context

KEY_CHANGES = (Delete, MoveNamespace)
_SHOW_LIMIT = 5
SYNC_OPTIONS = "argocd.argoproj.io/sync-options"
#: 지우면 되돌려도(revert) 데이터가 돌아오지 않는 종류 — 새로 만들어질 뿐이다
_DATA_KINDS = {
    "PersistentVolumeClaim": "볼륨이 같이 지워질 수 있어요(StorageClass reclaimPolicy 가 Delete 면 디스크까지).",
    "PersistentVolume": "reclaimPolicy 가 Delete 면 디스크까지 지워져요.",
    "Namespace": "그 안의 리소스(PVC·Secret 포함)가 모두 지워져요.",
    "CustomResourceDefinition": "이 종류의 커스텀 리소스가 클러스터에서 모두 지워져요.",
}


def is_key_change(change: Any) -> bool:
    return isinstance(change, KEY_CHANGES)


# ── 고치기 전 ─────────────────────────────────────────────


def prepare(ctx: Context) -> Context | Refused:
    """change.prepare 가 렌더 결과에서 대상을 찾은 뒤 부른다."""
    if isinstance(ctx.change, Delete):
        return ctx
    assert isinstance(ctx.change, MoveNamespace)
    label = compare.label(ctx.key)
    kind = ctx.key[1]
    if kind in risks.CLUSTER_SCOPED:
        return Refused(code="key_change_blocked", message=f"{label} 는 네임스페이스가 없는 종류라 옮길 수 없어요.")
    to = ctx.change.to
    if current_namespace(ctx) == to:
        return Refused(code="no_change", message=f"{label} 는 이미 {to} 네임스페이스에 있어요. 바꿀 게 없어요.")
    denied = _project_denied(ctx, to)
    if denied:
        return denied
    moved = new_key(ctx)
    if moved in ctx.before:
        return Refused(code="key_change_blocked",
                       message=f"{_label_in(ctx, moved)} 가 이미 이 앱에 있어요 — 옮기면 같은 리소스가 둘이 돼요.")
    return ctx


def current_namespace(ctx: Context) -> str:
    """지금 들어가 있는 네임스페이스 — 문서에 없으면 앱 기본(destination)이다."""
    return ctx.key[2] or ctx.app.destination_namespace


def new_key(ctx: Context) -> Key:
    """옮긴 뒤 렌더 결과의 키. 앱 기본으로 옮기면 metadata.namespace 줄을 지우므로 렌더 결과의 namespace 도 비어 있다."""
    assert isinstance(ctx.change, MoveNamespace)
    api_version, kind, _, name = ctx.key
    return (api_version, kind, "" if _to_default(ctx) else ctx.change.to, name)


def _to_default(ctx: Context) -> bool:
    assert isinstance(ctx.change, MoveNamespace)
    return bool(ctx.app.destination_namespace) and ctx.change.to == ctx.app.destination_namespace


def _label_in(ctx: Context, key: Key) -> str:
    """namespace 가 빈 키는 앱 기본 네임스페이스로 보여 준다."""
    api_version, kind, namespace, name = key
    return compare.label((api_version, kind, namespace or ctx.app.destination_namespace, name))


def _project_denied(ctx: Context, to: str) -> Refused | None:
    """옮겨 갈 곳을 앱의 ArgoCD 프로젝트가 막으면 거절 — 머지해도 sync 가 거부된다 (DURO-240 과 같은 규칙).
    프로젝트를 못 읽었거나 판단하지 못하면 막지 않고 카드에 경고한다 (risk_warnings)."""
    app = ctx.app
    if app.project_destinations is None:
        return None
    permitted = argocd.destination_permitted(app.project_destinations, server=app.destination_server,
                                             name=app.destination_name, namespace=to)
    if permitted is not False:
        return None
    allowed, excluded = argocd.allowed_namespaces(app.project_destinations, server=app.destination_server,
                                                  name=app.destination_name)
    where = ", ".join(allowed[:_SHOW_LIMIT]) or "없음"
    if excluded:
        where += f"(제외: {', '.join(excluded[:_SHOW_LIMIT])})"
    return Refused(code="project_denied", retry=True, message=(
        f"이 앱의 ArgoCD 프로젝트 '{app.project}' 는 이 클러스터에서 {where} 네임스페이스에만 배포를 허락해요. "
        f"{to} 로 옮기면 머지해도 ArgoCD sync 가 거부돼요. 허락된 네임스페이스"
        + (f"(앱 기본은 {app.destination_namespace})" if app.destination_namespace else "")
        + " 로 옮기거나, ArgoCD 프로젝트 설정을 먼저 바꿔 주세요."))


# ── 고칠 곳 찾기 + 고치기 ─────────────────────────────────


def propose(ctx: Context) -> dict[str, str | None] | Refused:
    if ctx.app.source_type == "Kustomize":
        return _kustomize(ctx)
    return _plain(ctx)


def _plain(ctx: Context) -> dict[str, str | None] | Refused:
    found: list[tuple[str, YamlFile, int]] = []
    for file in plain_files(ctx.app_dir, recurse=ctx.app.directory_recurse):
        try:
            parsed = YamlFile(file.read_text(encoding="utf-8"))
        except ValueError as exc:
            return Refused(code="render_failed", message=f"{ctx.relative(file)} 를 고칠 수 있게 읽지 못했어요. {exc}")
        for index, doc in enumerate(parsed.docs):
            if isinstance(doc, dict) and compare.key_of(doc) == ctx.key:
                found.append((ctx.relative(file), parsed, index))
    if len(found) != 1:
        where = ", ".join(path for path, _, _ in found) or "없음"
        return Refused(code="ambiguous", message=f"{compare.label(ctx.key)} 문서가 {len(found)}개예요({where}).")
    path, parsed, index = found[0]
    return _edit_file(ctx, path, parsed, index)


def _edit_file(ctx: Context, path: str, parsed: YamlFile, index: int) -> dict[str, str | None]:
    """그 파일의 index 번째 문서를 빼거나(삭제) 네임스페이스를 고친다. 빼고 나서 문서가 없으면 파일을 지운다(None)."""
    if isinstance(ctx.change, Delete):
        if sum(isinstance(doc, dict) for doc in parsed.docs) == 1:
            return {path: None}
        del parsed.docs[index]
    else:
        _set_namespace(parsed.docs[index], ctx)
    if not parsed.keeps_shape():
        ctx.warnings.append(f"{path} 는 다시 쓰면 들여쓰기·따옴표 모양이 조금 바뀌어요 — diff 에 같이 보여요.")
    return {path: parsed.dump()}


def _set_namespace(doc: Any, ctx: Context) -> None:
    assert isinstance(ctx.change, MoveNamespace)
    meta = doc["metadata"]
    if _to_default(ctx):
        if "namespace" in meta:
            remove_key(meta, "namespace")
        return
    if "namespace" in meta:
        meta["namespace"] = ctx.change.to
        return
    keys = list(meta)
    meta.insert(keys.index("name") + 1 if "name" in keys else len(keys), "namespace", ctx.change.to)


def _kustomize(ctx: Context) -> dict[str, str | None] | Refused:
    file = kustomization_file(ctx.app_dir)
    assert file is not None   # prepare 가 이미 봤다
    try:
        parsed = YamlFile(file.read_text(encoding="utf-8"))
    except ValueError as exc:
        return Refused(code="render_failed", message=f"{ctx.relative(file)} 를 고칠 수 있게 읽지 못했어요. {exc}")
    kustomization = parsed.docs[0] if parsed.docs and isinstance(parsed.docs[0], dict) else None
    if kustomization is None:
        return Refused(code="render_failed", message=f"{ctx.relative(file)} 가 비어 있어요.")
    label = compare.label(ctx.key)
    where = ctx.relative(ctx.app_dir)
    if isinstance(ctx.change, MoveNamespace) and kustomization.get("namespace"):
        return Refused(code="key_change_blocked", message=(
            f"'{where}' 의 kustomization 이 `namespace: {kustomization['namespace']}` 로 모든 리소스의 네임스페이스를 정해요 — "
            f"{label} 만 옮길 수 없고, 그 줄을 바꾸면 앱 전체가 옮겨져요. 그렇게 하려면 GitHub 에서 직접 고쳐 주세요."))

    found = _local_docs(ctx, kustomization)
    if isinstance(found, Refused):
        return found
    if not found:
        return Refused(code="key_change_blocked", message=(
            f"{label} 는 이 overlay('{where}')의 파일이 아니라 base(다른 폴더)에서 와요. base 에서 고치면 같은 base 를 쓰는 "
            "다른 환경도 같이 바뀌어요 — 이 환경에서만 하려면 GitHub 에서 overlay 를 직접 고쳐 주세요(예: `$patch: delete` patch)."))
    if len(found) > 1:
        return Refused(code="ambiguous", message=f"{label} 문서가 overlay 에 {len(found)}개예요"
                                                 f"({', '.join(path for _, path, _, _ in found)}).")
    entry, path, doc_file, index = found[0]
    raw = doc_file.docs[index]
    patched = _patched_by(ctx, kustomization, raw)
    if isinstance(patched, Refused):
        return patched
    if patched:
        what = "빼면" if isinstance(ctx.change, Delete) else "옮기면"
        return Refused(code="key_change_blocked", message=(
            f"overlay 의 patch({patched})가 {label} 를 가리켜요 — 리소스만 {what} patch 가 갈 곳이 없어 렌더가 깨져요. "
            "그 patch 를 먼저 정리해야 해요(GitHub 에서 직접 고쳐 주세요)."))

    edits = _edit_file(ctx, path, doc_file, index)
    if edits.get(path, "") is None:
        # 파일째 지운다 — kustomization 의 resources 줄도 같이 뺀다
        remove_item(kustomization, "resources", entry)
        if not kustomization["resources"]:
            remove_key(kustomization, "resources")
        if not parsed.keeps_shape():
            ctx.warnings.append(f"{ctx.relative(file)} 는 다시 쓰면 들여쓰기·따옴표 모양이 조금 바뀌어요 — diff 에 같이 보여요.")
        edits[ctx.relative(file)] = parsed.dump()
    return edits


def _local_docs(ctx: Context, kustomization: Any) -> list[tuple[int, str, YamlFile, int]] | Refused:
    """overlay `resources:` 가운데 이 폴더 안 파일에서 대상 문서 — (resources 의 몇 번째, 파일, 읽은 파일, 몇 번째 문서)."""
    found = []
    entries = kustomization.get("resources")
    for position, entry in enumerate(entries if isinstance(entries, list) else []):
        if not isinstance(entry, str):
            continue
        file = resolve_inside(ctx.root, (Path(ctx.relative(ctx.app_dir)) / entry).as_posix())
        if file is None or not file.is_file() or not file.is_relative_to(ctx.app_dir):
            continue
        try:
            parsed = YamlFile(file.read_text(encoding="utf-8"))
        except ValueError as exc:
            return Refused(code="render_failed", message=f"{ctx.relative(file)} 를 고칠 수 있게 읽지 못했어요. {exc}")
        for index, doc in enumerate(parsed.docs):
            if isinstance(doc, dict) and _renders_as(doc, kustomization, ctx.key):
                found.append((position, ctx.relative(file), parsed, index))
    return found


def _renders_as(doc: dict, kustomization: Any, key: Key) -> bool:
    """overlay 파일의 문서가 렌더 결과의 그 키가 되는지 — overlay 의 이름 앞·뒤 붙이기와 namespace 만 본다.
    틀리게 골랐으면 렌더 비교가 거절한다."""
    api_version, kind, namespace, name = key
    meta = doc.get("metadata") or {}
    rendered = f"{kustomization.get('namePrefix') or ''}{meta.get('name') or ''}{kustomization.get('nameSuffix') or ''}"
    return (str(doc.get("apiVersion")) == api_version and str(doc.get("kind")) == kind and rendered == name
            and str(kustomization.get("namespace") or meta.get("namespace") or "") == namespace)


def _patched_by(ctx: Context, kustomization: Any, raw: dict) -> str | Refused | None:
    """overlay 의 patch 가운데 이 리소스(파일의 원래 이름, 또는 렌더된 이름)를 가리키는 것 — 문구에 쓸 이름."""
    kind = str(raw.get("kind"))
    names = {str((raw.get("metadata") or {}).get("name") or ""), ctx.key[3]}
    for number, item in enumerate(kustomization.get("patches") or [], 1):
        if not isinstance(item, dict):
            continue
        what = item.get("path") or f"patches {number}번째"
        if isinstance(item.get("target"), dict):
            if _target_hits(item["target"], kind, names):
                return str(what)
            continue
        docs = _patch_docs(ctx, item.get("path"), item.get("patch"))
        if isinstance(docs, Refused):
            return docs
        if _docs_hit(docs, kind, names):
            return str(what)
    for number, item in enumerate(kustomization.get("patchesStrategicMerge") or [], 1):
        if not isinstance(item, str):
            continue
        inline = "\n" in item or ":" in item
        docs = _patch_docs(ctx, None if inline else item, item if inline else None)
        if isinstance(docs, Refused):
            return docs
        if _docs_hit(docs, kind, names):
            return item if not inline else f"patchesStrategicMerge {number}번째"
    for number, item in enumerate(kustomization.get("patchesJson6902") or [], 1):
        if isinstance(item, dict) and isinstance(item.get("target"), dict) and _target_hits(item["target"], kind, names):
            return str(item.get("path") or f"patchesJson6902 {number}번째")
    return None


def _target_hits(target: dict, kind: str, names: set[str]) -> bool:
    """이름으로 이 리소스를 고르는 target 인가. 이름 없이 종류·레이블로 고르는 patch 는 다른 리소스에도 걸리니 남아도 렌더가 깨지지 않는다."""
    if target.get("kind") and target["kind"] != kind:
        return False
    pattern = target.get("name")
    if not isinstance(pattern, str) or not pattern:
        return False
    try:
        return any(re.fullmatch(pattern, name) for name in names)
    except re.error:
        return pattern in names


def _patch_docs(ctx: Context, path: Any, inline: Any) -> list[Any] | Refused:
    if isinstance(inline, str):
        text = inline
    elif isinstance(path, str):
        file = resolve_inside(ctx.root, (Path(ctx.relative(ctx.app_dir)) / path).as_posix())
        if file is None or not file.is_file():
            return []
        text = file.read_text(encoding="utf-8")
    else:
        return []
    try:
        return YamlFile(text).docs
    except ValueError as exc:
        return Refused(code="render_failed", message=f"overlay 의 patch 를 읽지 못했어요. {exc}")


def _docs_hit(docs: list[Any], kind: str, names: set[str]) -> bool:
    return any(isinstance(doc, dict) and doc.get("kind") == kind and (doc.get("metadata") or {}).get("name") in names
               for doc in docs)


# ── 고친 뒤 ───────────────────────────────────────────────


def compare_app(ctx: Context, after: dict[Key, dict]) -> Refused | None:
    """렌더 비교 — 삭제는 대상 키 하나만 사라지고, 네임스페이스 변경은 옛 키가 새 키로 바뀌며 namespace 말고는 같아야 한다.
    나머지 객체는 한 칸도 바뀌면 안 된다."""
    changed = [k for k in sorted(set(ctx.before) | set(after)) if ctx.before.get(k) != after.get(k)]
    if any(k[1] == "Secret" for k in changed):
        return Refused(code="secret", message="Secret 이 바뀌어요 — Secret 은 PR 로 바꾸지 않아요.")
    label = compare.label(ctx.key)
    expected_keys = {ctx.key} if isinstance(ctx.change, Delete) else {ctx.key, new_key(ctx)}
    others = [k for k in changed if k not in expected_keys]
    if others:
        return Refused(code="render_mismatch", retry=True,
                       message="요청하지 않은 리소스도 바뀌어요: "
                               + ", ".join(_describe(k, ctx.before, after) for k in others[:_SHOW_LIMIT])
                               + f". {label} 만 바뀌게 고쳐 주세요.")
    if ctx.key in after:
        return Refused(code="render_mismatch", retry=True, message=f"고친 뒤에도 {label} 가 그대로 남아요.")
    if isinstance(ctx.change, Delete):
        return None
    moved = new_key(ctx)
    if moved not in after:
        return Refused(code="render_mismatch", retry=True,
                       message=f"고친 뒤 {_label_in(ctx, moved)} 가 생기지 않아요.")
    stray = compare.diff_paths(expected_after(ctx), after[moved])
    if stray:
        return Refused(code="render_mismatch", retry=True,
                       message="옮긴 뒤 네임스페이스 말고도 바뀌는 칸이 있어요: "
                               + ", ".join(compare.show(p) for p in stray[:_SHOW_LIMIT]) + ".")
    return None


def _describe(key: Key, before: dict[Key, dict], after: dict[Key, dict]) -> str:
    if key not in before:
        return f"{compare.label(key)}(새로 생김)"
    if key not in after:
        return f"{compare.label(key)}(사라짐)"
    return compare.label(key)


def expected_after(ctx: Context) -> dict:
    """옮긴 뒤 렌더 결과가 같아야 할 것 — 지금 렌더 결과에서 metadata.namespace 만 바꾼(앱 기본이면 지운) 것."""
    obj = copy.deepcopy(ctx.obj)
    meta = obj.setdefault("metadata", {})
    if _to_default(ctx):
        meta.pop("namespace", None)
    else:
        assert isinstance(ctx.change, MoveNamespace)
        meta["namespace"] = ctx.change.to
    return obj


def render_diff(ctx: Context, after: dict[Key, dict]) -> str:
    label = _label_in(ctx, ctx.key)
    if isinstance(ctx.change, Delete):
        return compare.unified(compare.dump(ctx.obj), "", f"{label} (지금)", f"{label} (머지 후 — 없어짐)")
    moved = new_key(ctx)
    return compare.unified(compare.dump(ctx.obj), compare.dump(after[moved]),
                           f"{label} (지금)", f"{_label_in(ctx, moved)} (머지 후 — 새로 생김)")


# ── 승인 카드 경고 ───────────────────────────────────────


def risk_warnings(ctx: Context, file: str) -> list[RiskWarning]:
    """승인 카드·PR 본문에 올릴 것 — 클러스터에서 실제로 지워지는지, 그리고 종류별로 되돌릴 수 없는 것."""
    found = [_normal(_removal(ctx, file))]
    if isinstance(ctx.change, Delete):
        found += _delete_kind(ctx, file)
    else:
        found += _move_kind(ctx, file)
    return found


def sync_options(obj: dict) -> set[str]:
    """리소스의 `argocd.argoproj.io/sync-options` 어노테이션 — `Prune=false,Delete=false` 처럼 쉼표로 잇는다."""
    annotations = dig(obj, ("metadata", "annotations")) or {}
    text = annotations.get(SYNC_OPTIONS) if isinstance(annotations, dict) else None
    if not isinstance(text, str):
        return set()
    return {part.strip().replace(" ", "") for part in text.split(",") if part.strip()}


def _removal(ctx: Context, file: str) -> RiskWarning:
    """옛 리소스가 클러스터에서 지워지나. ArgoCD 는 git 에서 빠진 리소스를 prune 할 때만 지운다:
    자동 sync + prune 이면 바로, 수동 sync 면 sync 할 때 Prune 을 켜야, Prune=false 리소스는 끝까지 안 지운다.
    Delete=false 는 앱을 지울 때만 남기는 표시라 git 에서 빼는 것과는 상관없다."""
    label = _label_in(ctx, ctx.key)
    move = isinstance(ctx.change, MoveNamespace)
    options = sync_options(ctx.obj)
    app = ctx.app
    head = "옛 리소스는 " if move else ""
    leftover = (" 새 네임스페이스에 새로 만들어지는 것과 옛 것이 함께 돌게 돼요 — 옛 것은 kubectl 로 따로 지워야 해요."
                if move else " 클러스터에서도 지우려면 kubectl 로 따로 지워야 해요.")
    note = " (Delete=false 는 앱을 지울 때만 남기는 표시라 이 PR 과는 상관없어요.)" if "Delete=false" in options else ""
    if ctx.target.in_cluster is False:
        return risks.risk("git_only", file, label, None, None,
                          f"{head}클러스터에 없어요(sync 가 안 됐어요) — git 에서만 빠지고 클러스터에서 지울 것은 없어요.",
                          "")
    if "Prune=false" in options:
        code, text, tail = "prune_disabled", f"{head}Git 에서만 빠지고 클러스터엔 남아요 — 리소스에 {SYNC_OPTIONS}: " \
                                             "Prune=false 가 있어 ArgoCD 가 지우지 않아요.", leftover
    elif "Prune=confirm" in options:
        code, text, tail = "prune_confirm", f"{head}ArgoCD 에서 지우기를 확인(Prune=confirm)해야 클러스터에서 지워져요.", \
            " 확인 전까지는 남아 OutOfSync 로 보여요."
    elif not app.auto_sync:
        code, text, tail = "prune_manual", f"{head}자동 sync 가 꺼져 있어 머지만으로는 클러스터에서 지워지지 않아요.", \
            " ArgoCD 에서 sync 할 때 Prune 을 켜야 지워져요 — 그 전엔 남아 OutOfSync 로 보여요."
    elif not app.prune:
        code, text, tail = "prune_off", f"{head}Git 에서만 빠지고 클러스터엔 남아요 — 앱의 자동 sync 에 prune 이 꺼져 있어요.", \
            leftover
    else:
        return risks.risk("prune_on", file, label, None, None,
                          f"{head}머지하면 ArgoCD 가 클러스터에서도 지워요(자동 sync · prune 켜짐).{note}", "")
    return risks.risk(code, file, label, None, None, text + note, tail.strip())


def _high(warning: RiskWarning) -> RiskWarning:
    return warning.model_copy(update={"level": "high"})


def _normal(warning: RiskWarning) -> RiskWarning:
    return warning.model_copy(update={"level": "normal"})


def _volume_templates(obj: dict) -> bool:
    return obj.get("kind") == "StatefulSet" and bool(dig(obj, ("spec", "volumeClaimTemplates")))


def _delete_kind(ctx: Context, file: str) -> list[RiskWarning]:
    label = _label_in(ctx, ctx.key)
    kind = ctx.key[1]
    revert = "PR 을 revert 하면 다시 만들어지지만 새 것일 뿐 데이터는 돌아오지 않아요."
    if kind in _DATA_KINDS:
        return [_high(risks.risk("delete_data", file, label, None, None,
                                 f"지워지면 {_DATA_KINDS[kind]}", f"{revert} 먼저 백업하세요."))]
    if _volume_templates(ctx.obj):
        policy = dig(ctx.obj, ("spec", "persistentVolumeClaimRetentionPolicy", "whenDeleted")) or "Retain"
        if policy == "Delete":
            return [_high(risks.risk("delete_data", file, label, "spec.persistentVolumeClaimRetentionPolicy.whenDeleted",
                                     "Delete", "지워지면 이 StatefulSet 이 만든 PVC 와 데이터도 같이 지워져요.",
                                     f"{revert} 먼저 백업하세요."))]
        return [_high(risks.risk("delete_data", file, label, "spec.volumeClaimTemplates", None,
                                 "지워지면 파드가 모두 멈춰요. 만든 PVC 는 남지만(whenDeleted: Retain) 따로 정리해야 해요.",
                                 "revert 하면 남은 PVC 에 다시 붙어요."))]
    if kind == "Service":
        return [_normal(risks.risk("delete_address", file, label, None, None,
                                   "지워지면 이 주소로 붙던 곳이 끊겨요.",
                                   "revert 하면 다시 생기지만 ClusterIP 는 새로 받아요."))]
    return [_normal(risks.risk("delete", file, label, None, None, "지워지면 이 리소스가 하던 일이 멈춰요.",
                               "revert 하면 같은 내용으로 다시 만들어져요."))]


def _move_kind(ctx: Context, file: str) -> list[RiskWarning]:
    assert isinstance(ctx.change, MoveNamespace)
    old, new = _label_in(ctx, ctx.key), _label_in(ctx, new_key(ctx))
    kind, name = ctx.key[1], ctx.key[3]
    before, to = current_namespace(ctx), ctx.change.to
    found = [_normal(risks.risk("recreate", file, old, "metadata.namespace", to,
                                f"네임스페이스를 바꾸면 옛 것을 지우고 {new} 를 새로 만들어요(삭제 후 생성) — 이어서 옮겨지는 게 "
                                "아니라 다른 리소스가 돼요.", "그 사이 잠깐 멈출 수 있어요."))]
    if kind == "PersistentVolumeClaim" or _volume_templates(ctx.obj):
        found.append(_high(risks.risk("move_data", file, old, None, None,
                                      f"{to} 에는 빈 볼륨이 새로 생기고, 데이터는 옮겨지지 않아요.",
                                      "데이터를 먼저 옮기거나 백업하세요 — revert 해도 새 볼륨의 내용은 돌아오지 않아요.")))
    if kind == "Service":
        found.append(_normal(risks.risk("move_address", file, old, None, None,
                                        f"주소가 {name}.{before}.svc 에서 {name}.{to}.svc 로 바뀌고 ClusterIP 도 새로 받아요.",
                                        "옛 주소로 붙던 곳(다른 앱 설정·Ingress)을 같이 고쳐야 해요.")))
    if dig(ctx.obj, ("spec", "template", "spec")) is not None or kind in ("Pod", "CronJob"):
        found.append(_normal(risks.risk("move_refs", file, old, None, None,
                                        f"이 리소스가 쓰는 ConfigMap·Secret·ServiceAccount·PVC 가 {to} 에도 있어야 해요.",
                                        "없으면 파드가 뜨지 않아요.")))
    if to != ctx.app.destination_namespace:
        found.append(_normal(risks.risk("move_namespace_exists", file, old, None, None,
                                        f"{to} 는 앱 기본 네임스페이스가 아니에요 — 클러스터에 {to} 가 있어야 sync 가 돼요.",
                                        "")))
    if ctx.app.project_destinations is None:
        found.append(_normal(risks.risk("project_unknown", file, old, None, None,
                                        f"ArgoCD 프로젝트 규칙을 읽지 못해 {to} 가 허락되는지 확인하지 못했어요.",
                                        "막혀 있으면 머지 뒤 sync 가 거부돼요.")))
    return found
