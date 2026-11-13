"""Kustomize — Application path(환경 overlay)만 고친다. base 는 다른 환경도 같이 쓰므로 건드리지 않는다.

레플리카 수는 kustomization 의 `replicas:`, 이미지 태그는 `images:` 칸을 고친다.
resources · 환경변수는 그 폴더에 Kukie 의 patch 파일(kukie-<kind>-<name>-<요청>.yaml)을 두고 `patches:` 에 건다.
같은 요청이 다시 오면 새 파일을 만들지 않고 그 파일을 고친다. patch 는 맨 뒤에 걸려 앞의 patch 보다 나중에 적용된다.

경로 지정 변경(what=patch, DURO-251)도 같은 방식이다 — kukie-<kind>-<name>-patch.yaml 하나에 그 리소스의 경로 지정 변경을
모은다. 파일 내용은 경로를 따라 쓰지 않고 "이 patch 가 없을 때의 렌더 결과 → 기대값" 의 차이를 strategic merge patch 로 쓴다
(merge_patch). 그래서 다시 와도 파일을 통째로 다시 쓰면 되고, patch 시점에는 없는 칸(transformer 가 나중에 붙이는 레이블 등)의
부모를 따로 만들 필요가 없다. 측정(experiment/results/kustomize-patch-rejection, DURO-250)에서 나온 조건을 따른다:
  - target 이름은 이 overlay 의 namePrefix/nameSuffix 를 떼어 낸 이름 — 같은 폴더 patch 는 접두사보다 먼저 적용된다.
    target 에 namespace 는 넣지 않는다
  - 같은 overlay 의 `replicas:` · `images:` 가 정하는 칸은 patch 뒤에 덮이므로 그 항목을 고친다
  - commonLabels · labels · commonAnnotations 가 정하는 칸은 이 overlay 의 모든 리소스에 붙어 이 리소스만 못 바꾼다 — 거절
  - 이름 없는 병합 목록(ports · volumeMounts …)은 `$patch: replace` 로 통째로 — 병합 키(containerPort+protocol 등)를 맞추지 않는다
기대값과 같은지는 다른 변경과 같이 렌더 비교가 본다. `transformers:` 가 그 칸을 다시 정하면 거기서 걸리고 원인을 붙인다(explain).
"""
from __future__ import annotations

import copy
import io
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.representer import RoundTripRepresenter

from kukie_agent.manifests import compare, patch, plain
from kukie_agent.manifests.compare import FieldPath
from kukie_agent.manifests.models import Env, ImageTag, Patch, Refused, Replicas, Resources
from kukie_agent.manifests.render import RenderError, RenderTimeout, kustomization_file, render
from kukie_agent.manifests.workload import POD_SPEC, SCALABLE, container, dig, split_image
from kukie_agent.manifests.yamlfile import YamlFile, dump_new

if TYPE_CHECKING:
    from kukie_agent.manifests.change import Context

def propose(ctx: Context) -> dict[str, str] | Refused:
    file = kustomization_file(ctx.app_dir)
    assert file is not None   # prepare 가 이미 봤다
    try:
        parsed = YamlFile(file.read_text(encoding="utf-8"))
    except ValueError as exc:
        return Refused(code="render_failed", message=f"{ctx.relative(file)} 를 고칠 수 있게 읽지 못했어요. {exc}")
    kustomization = parsed.docs[0] if parsed.docs and isinstance(parsed.docs[0], dict) else None
    if kustomization is None:
        return Refused(code="render_failed", message=f"{ctx.relative(file)} 가 비어 있어요.")

    edits: dict[str, str] = {}
    change = ctx.change
    if isinstance(change, Replicas):
        _set_replicas(kustomization, ctx.target.name, change.count)
    elif isinstance(change, ImageTag):
        current = container(ctx.obj, ctx.target.kind, ctx.container or "") or {}
        _set_image_tag(kustomization, str(current.get("image", "")), change.tag)
    elif isinstance(change, Patch):
        written = _general_patch(ctx, kustomization, file)
        if isinstance(written, Refused):
            return written
        edits.update(written)
    else:
        assert isinstance(change, Resources | Env)
        name = f"kukie-{ctx.target.kind.lower()}-{ctx.target.name}-{change.what}.yaml"
        patch_file = ctx.app_dir / name
        try:
            edits[ctx.relative(patch_file)] = _patch(ctx, patch_file.read_text(encoding="utf-8") if patch_file.is_file() else None)
        except ValueError as exc:
            return Refused(code="render_failed", message=f"{ctx.relative(patch_file)} 를 고칠 수 있게 읽지 못했어요. {exc}")
        _add_patch(kustomization, name, ctx, original_name(kustomization, ctx.target.name))

    if not parsed.keeps_shape():
        ctx.warnings.append(f"{ctx.relative(file)} 는 다시 쓰면 들여쓰기·따옴표 모양이 조금 바뀌어요 — diff 에 같이 보여요.")
    edits[ctx.relative(file)] = parsed.dump()
    return edits


def add_resource(text: str, entry: str) -> tuple[str, bool]:
    """kustomization 글자에 `resources:` 한 줄을 더한다 (DURO-154) → (새 글자, 모양이 그대로인가). 못 읽으면 ValueError."""
    parsed = YamlFile(text)
    kustomization = parsed.docs[0] if parsed.docs and isinstance(parsed.docs[0], dict) else None
    if kustomization is None:
        raise ValueError("비어 있어요.")
    _list(kustomization, "resources").append(entry)
    return parsed.dump(), parsed.keeps_shape()


def _list(kustomization: Any, key: str) -> Any:
    if not isinstance(kustomization.get(key), list):
        kustomization[key] = CommentedSeq()
    return kustomization[key]


def _set_replicas(kustomization: Any, name: str, count: int) -> None:
    entries = _list(kustomization, "replicas")
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None:
        entries.append(CommentedMap([("name", name), ("count", count)]))
    else:
        entry["count"] = count


def _set_image_tag(kustomization: Any, image: str, tag: str) -> None:
    """지금 렌더 결과의 이미지 이름으로 `images:` 칸을 찾는다 — newName 이 있으면 그것, 없으면 name 이 그 이름이다.

    digest 가 있으면 뗀다. digest 가 태그보다 먼저라 남겨 두면 새 태그가 쓰이지 않는다.
    """
    repo, _, _ = split_image(image)
    entries = _list(kustomization, "images")
    entry = next((e for e in entries if isinstance(e, dict) and (e.get("newName") or e.get("name")) == repo), None)
    if entry is None:
        entries.append(CommentedMap([("name", repo), ("newTag", tag)]))
        return
    entry.pop("digest", None)
    entry["newTag"] = tag


def _patch(ctx: Context, existing: str | None) -> str:
    """Kukie 의 strategic merge patch — 요청한 컨테이너의 resources 나 env 만 담는다."""
    parsed = YamlFile(existing) if existing is not None else None
    # 파일이 비었거나 주석만 남았으면 새로 쓴다
    keep = parsed is not None and bool(parsed.docs) and isinstance(parsed.docs[0], dict)
    doc = parsed.docs[0] if keep else CommentedMap([
        ("apiVersion", ctx.target.api_version), ("kind", ctx.target.kind),
        ("metadata", CommentedMap([("name", ctx.target.name)]))])
    patch_container = _ensure_container(doc, ctx.target.kind, ctx.container or "")
    if isinstance(ctx.change, Env):
        # 예전에 지우라고 적어 둔 이름을 다시 넣는 요청이면 그 표시부터 뗀다
        env = patch_container.get("env") or []
        for item in [e for e in env if e.get("name") in ctx.change.values and "$patch" in e]:
            env.remove(item)
    plain.edit(doc, ctx)
    if isinstance(ctx.change, Env):
        _mark_deletes(patch_container, ctx.change.remove)
    if keep:
        return parsed.dump()
    what = "CPU·메모리" if isinstance(ctx.change, Resources) else "환경변수"
    header = (f"# Kukie 가 더한 patch — {ctx.target.kind} {ctx.target.name} 의 {what}.\n"
              "# 같은 요청이 다시 오면 Kukie 가 이 파일을 고친다.\n")
    return dump_new(doc, header)


def _ensure_container(doc: Any, kind: str, name: str) -> Any:
    here = doc
    for part in (*POD_SPEC[kind], "containers"):
        wanted = list if part == "containers" else dict
        if not isinstance(here.get(part), wanted):
            here[part] = CommentedSeq() if part == "containers" else CommentedMap()
        here = here[part]
    found = next((c for c in here if c.get("name") == name), None)
    if found is None:
        found = CommentedMap([("name", name)])
        here.append(found)
    return found


def _mark_deletes(patch_container: Any, names: list[str]) -> None:
    """patch 에서 환경변수를 지우는 표시 — strategic merge 의 `$patch: delete`."""
    if not names:
        return
    env = patch_container.get("env")
    if not isinstance(env, list):
        env = patch_container["env"] = CommentedSeq()
    for name in names:
        env.append(CommentedMap([("name", name), ("$patch", "delete")]))


def _add_patch(kustomization: Any, name: str, ctx: Context, target_name: str) -> None:
    patches = _list(kustomization, "patches")
    if any(isinstance(p, dict) and p.get("path") == name for p in patches):
        return
    # target 으로 고르면 patch 안의 이름·namespace 와 상관없이 그 리소스에 걸린다 (overlay 가 namespace 를 바꿔도)
    target = CommentedMap()
    if ctx.target.group:
        target["group"] = ctx.target.group
    target["version"] = ctx.target.api_version.split("/")[-1]
    target["kind"] = ctx.target.kind
    target["name"] = target_name
    patches.append(CommentedMap([("path", name), ("target", target)]))


def original_name(kustomization: Any, rendered: str) -> str:
    """patch target 에 쓸 이름 — 렌더 이름에서 이 overlay 의 namePrefix/nameSuffix 를 뗀다.

    같은 폴더의 patch 는 접두사·접미사보다 먼저 적용돼 렌더 이름으로는 걸리지 않는다 (DURO-250 측정, guestbook).
    base 가 붙인 접두사는 남겨도 된다 — kustomize 가 base 의 원래 이름과 접두사 붙은 이름 둘 다로 찾는다."""
    name = rendered
    prefix, suffix = kustomization.get("namePrefix"), kustomization.get("nameSuffix")
    if isinstance(prefix, str) and prefix and name.startswith(prefix) and len(name) > len(prefix):
        name = name[len(prefix):]
    if isinstance(suffix, str) and suffix and name.endswith(suffix) and len(name) > len(suffix):
        name = name[:-len(suffix)]
    return name


# ── 경로 지정 변경 (DURO-251) ─────────────────────────────

#: strategic merge 에서 name 으로 항목을 맞추는 목록 (쿠버네티스 기본 종류). 항목 하나만 고치거나 `$patch: delete` 로 뺀다
_NAME_MERGED = frozenset({"containers", "initContainers", "ephemeralContainers", "env", "volumes",
                          "imagePullSecrets", "resourceClaims"})
#: name 이 아닌 키(containerPort+protocol · mountPath · port …)로 맞추는 병합 목록 — 키를 맞추기 어려워 `$patch: replace` 로
#: 통째로 바꾼다 (측정에서 ports 를 지울 때 containerPort 만으로는 안 지워졌다)
_KEY_MERGED = frozenset({"ports", "volumeMounts", "volumeDevices", "hostAliases", "topologySpreadConstraints",
                         "readinessGates"})
_HEADER = ("# Kukie 가 더한 patch — {kind} {name} 의 경로 지정 변경.\n"
           "# 같은 리소스를 다시 바꾸면 Kukie 가 이 파일을 다시 쓴다 (이 patch 가 없을 때의 렌더 결과와 요청한 결과의 차이).\n")


def _general_patch(ctx: Context, kustomization: Any, file: Path) -> dict[str, str] | Refused:
    assert ctx.patched is not None and isinstance(ctx.change, Patch)
    want = ctx.patched.want
    why = owned_by_overlay(kustomization, ctx.target.kind, ctx.obj, want)
    if why:
        return Refused(code="patch_mismatch", message=f"{compare.label(ctx.key)}: {why}")
    target_name = original_name(kustomization, ctx.target.name)
    for_patch = copy.deepcopy(want)
    routed = _route_transformers(kustomization, ctx, for_patch, target_name)
    if routed:
        return Refused(code="patch_mismatch", message=f"{compare.label(ctx.key)}: {routed}")

    name = f"kukie-{ctx.target.kind.lower()}-{ctx.target.name}-patch.yaml"
    path = ctx.app_dir / name
    listed = any(isinstance(p, dict) and p.get("path") == name for p in kustomization.get("patches") or [])
    base = ctx.obj
    if listed:
        # 지금 렌더 결과에는 이 patch 가 이미 들어 있다 — 빼고 렌더한 것과 기대값의 차이로 파일을 다시 쓴다
        found = _render_without(ctx, file, name)
        if isinstance(found, Refused):
            return found
        base = found
    diff = merge_patch(base, for_patch, builtin=_builtin(ctx.target.group))
    if not diff and not listed:
        return {}                  # replicas: · images: 항목만 고치면 된다
    metadata = CommentedMap([("name", target_name)])
    for key, value in (diff.pop("metadata", None) or {}).items():
        metadata[key] = value
    doc = CommentedMap([("apiVersion", ctx.target.api_version), ("kind", ctx.target.kind), ("metadata", metadata)])
    for key, value in diff.items():
        doc[key] = value
    patches = kustomization.get("patches") or []
    mine = [p for p in patches if isinstance(p, dict) and p.get("path") == name]
    if mine and patches[-1] is not mine[-1]:
        # 그 뒤에 다른 patch(넷의 env · resources 등)가 걸렸으면 맨 뒤로 옮긴다 — 파일은 "이 patch 를 뺀 렌더" 기준이라
        # 뒤에 남은 patch 가 같은 칸을 다시 덮으면 기대값과 달라진다 (agent#159 리뷰)
        for entry in mine:
            patches.remove(entry)
        patches.append(mine[-1])
    _add_patch(kustomization, name, ctx, target_name)
    return {ctx.relative(path): _dump(doc, _HEADER.format(kind=ctx.target.kind, name=ctx.target.name))}


def merge_patch(current: Any, want: Any, *, builtin: bool, field: str | None = None) -> Any:
    """current 에 적용하면 want 가 되는 strategic merge patch.

    맵은 바뀐 키만 담고 없어진 키는 null(지우기). 목록은 쿠버네티스 기본 종류의 병합 목록만 따로 다룬다 — 나머지(args ·
    tolerations · 커스텀 리소스의 목록)는 kustomize 도 통째로 바꾸므로 기대값 목록을 그대로 쓴다."""
    if isinstance(current, dict) and isinstance(want, dict):
        out: dict[str, Any] = {}
        for key, value in want.items():
            if key not in current:
                out[key] = copy.deepcopy(value)
            elif current[key] != value:
                out[key] = merge_patch(current[key], value, builtin=builtin, field=key)
        for key in current:
            if key not in want:
                out[key] = None
        return out
    if isinstance(want, list) and builtin and field in _NAME_MERGED | _KEY_MERGED:
        if field in _NAME_MERGED and isinstance(current, list):
            now, wanted = compare.named(current), compare.named(want)
            if now is not None and wanted is not None:
                changed = {name: (copy.deepcopy(item) if name not in now
                                  else {"name": name, **merge_patch(now[name], item, builtin=builtin)})
                           for name, item in wanted.items() if now.get(name) != item}
                # kustomize 는 patch 에 적힌 항목을 그 순서로 앞에 두고 나머지를 뒤에 붙인다 — 그대로 두면 순서가 바뀌는
                # 경우(env 는 $(VAR) 가 앞 항목만 본다) 바뀌지 않은 항목도 이름만 적어 기대값 순서를 맞춘다 (agent#159 리뷰)
                merged = [*changed, *(name for name in now if name in wanted and name not in changed)]
                order = list(changed) if merged == list(wanted) else list(wanted)
                items: list[Any] = [changed.get(name, {"name": name}) for name in order]
                items += [{"name": name, "$patch": "delete"} for name in now if name not in wanted]
                return items
        return [*copy.deepcopy(want), {"$patch": "replace"}]
    return copy.deepcopy(want)


def _builtin(group: str) -> bool:
    """쿠버네티스 기본 종류인가 — kustomize 가 스키마로 병합 목록을 아는 종류. 커스텀 리소스는 목록을 통째로 바꾼다."""
    return "." not in group or group.endswith(".k8s.io")


def _pod_template(kind: str) -> FieldPath | None:
    return POD_SPEC[kind][:-1] if kind in POD_SPEC else None


def owned_by_overlay(kustomization: Any, kind: str, current: dict, want: dict) -> str | None:
    """이 overlay 의 commonLabels · labels · commonAnnotations 가 정하는 칸을 바꾸는지 — patch 뒤에 다시 덮인다.
    그 항목은 이 overlay 의 모든 리소스에 붙어 이 리소스만 따로 바꿀 수 없다."""
    template = _pod_template(kind)
    owned: list[tuple[FieldPath, str]] = []

    def add(section: str, keys: Any, where: str, *, selectors: bool, templates: bool) -> None:
        for key in keys if isinstance(keys, dict) else {}:
            owned.append((("metadata", section, str(key)), where))
            if templates and template:
                owned.append(((*template, "metadata", section, str(key)), where))
            if selectors and section == "labels":
                if template:
                    owned.append((("spec", "selector", "matchLabels", str(key)), where))
                if kind == "Service":
                    owned.append((("spec", "selector", str(key)), where))

    add("labels", kustomization.get("commonLabels"), "commonLabels", selectors=True, templates=True)
    for entry in kustomization.get("labels") or []:
        if isinstance(entry, dict):
            add("labels", entry.get("pairs"), "labels", selectors=bool(entry.get("includeSelectors")),
                templates=bool(entry.get("includeSelectors") or entry.get("includeTemplates")))
    add("annotations", kustomization.get("commonAnnotations"), "commonAnnotations", selectors=False, templates=True)
    for path, where in owned:
        if compare.value_at(current, path) != compare.value_at(want, path):
            return (f"그 칸({compare.show(path)})은 이 overlay kustomization 의 {where}: 가 정해요 — 거기를 고쳐야 해요. "
                    f"{where}: 는 이 overlay 의 모든 리소스에 붙어서 patch 로 이 리소스만 바꿔도 다시 덮여요")
    return None


def _route_transformers(kustomization: Any, ctx: Context, for_patch: dict, target_name: str) -> str | None:
    """같은 overlay 의 `replicas:` · `images:` 가 정하는 칸은 patch 뒤에 덮인다 — 그 항목을 고치고, patch 에는 지금 값을 둔다.
    고칠 수 없으면(칸을 지우는 등) 그 이유."""
    current, want, kind = ctx.obj, for_patch, ctx.target.kind
    replicas = next((e for e in kustomization.get("replicas") or []
                     if isinstance(e, dict) and e.get("name") in (target_name, ctx.target.name)), None)
    if replicas is not None and kind in SCALABLE:
        old, new = dig(current, ("spec", "replicas")), dig(want, ("spec", "replicas"))
        if old != new:
            if not isinstance(new, int) or isinstance(new, bool):
                return ("그 칸(spec.replicas)은 이 overlay kustomization 의 replicas: 가 정해요 — 거기를 고쳐야 해요. "
                        "칸을 지우거나 숫자가 아닌 값으로는 바꿀 수 없어요")
            replicas["count"] = new
            want["spec"]["replicas"] = old
    template = _pod_template(kind)
    if template is None:
        return None
    for section in ("containers", "initContainers"):
        for old_item in dig(current, (*template, "spec", section)) or []:
            if not isinstance(old_item, dict) or not isinstance(old_item.get("image"), str):
                continue
            entry = _image_entry(kustomization, old_item["image"])
            new_item = compare.value_at(want, (*template, "spec", section, compare.item(str(old_item.get("name")))))
            if entry is None or not isinstance(new_item, dict) or new_item.get("image") == old_item["image"]:
                continue
            path = compare.show((*template, "spec", section, compare.item(str(old_item.get("name"))), "image"))
            if not isinstance(new_item.get("image"), str) or not new_item["image"]:
                return (f"그 칸({path})은 이 overlay kustomization 의 images: 가 정해요 — 거기를 고쳐야 해요. "
                        "이미지를 지울 수는 없어요")
            _set_image(entry, new_item["image"])
            new_item["image"] = old_item["image"]
    return None


def _image_entry(kustomization: Any, image: str) -> Any:
    repo, _, _ = split_image(image)
    return next((e for e in kustomization.get("images") or []
                 if isinstance(e, dict) and (e.get("newName") or e.get("name")) == repo), None)


def _set_image(entry: Any, image: str) -> None:
    """`images:` 항목이 이 이미지를 내게 고친다 — 저장소가 다르면 newName, digest 가 있으면 digest(태그보다 먼저다)."""
    repo, tag, digest = split_image(image)
    if repo != (entry.get("newName") or entry.get("name")):
        entry["newName"] = repo
    if digest:
        entry.pop("newTag", None)
        entry["digest"] = digest
    else:
        entry.pop("digest", None)
        if tag:
            entry["newTag"] = tag
        else:
            entry.pop("newTag", None)


def _render_without(ctx: Context, file: Path, name: str) -> dict | Refused:
    """Kukie 의 경로 지정 patch 를 뺀 렌더 결과에서 이 리소스 — 레포는 읽기만 하고 임시 폴더에서 돌린다."""
    relative = ctx.relative(file)
    try:
        parsed = YamlFile(file.read_text(encoding="utf-8"))
    except ValueError as exc:
        return Refused(code="render_failed", message=f"{relative} 를 고칠 수 있게 읽지 못했어요. {exc}")
    kustomization = parsed.docs[0]
    kustomization["patches"] = [p for p in kustomization["patches"] if not (isinstance(p, dict) and p.get("path") == name)]
    with tempfile.TemporaryDirectory(prefix="kukie-manifests-") as tmp:
        workdir = Path(tmp) / "repo"
        shutil.copytree(ctx.root, workdir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        (workdir / relative).write_text(parsed.dump(), encoding="utf-8")
        try:
            objects = compare.index(render("Kustomize", workdir / ctx.relative(ctx.app_dir), root=workdir))
        except RenderTimeout as exc:
            return Refused(code="busy", message=f"{exc} 잠시 뒤 다시 해 주세요.")
        except RenderError as exc:
            return Refused(code="render_failed", message=f"Kukie patch({name})를 뺀 렌더가 안 돼요. {exc}")
    found = objects.get(ctx.key)
    if found is None:
        return Refused(code="render_failed", message=f"Kukie patch({name})를 빼면 {compare.label(ctx.key)} 가 안 보여요.")
    return found


def explain(ctx: Context, refused: Refused) -> Refused:
    """경로 지정 patch 가 렌더 비교에서 걸렸을 때, 이 overlay 의 `transformers:` 가 patch 뒤에 그 칸을 다시 정했을 수 있다고 덧붙인다.
    patch 는 코드가 만들어 경로를 바꿔 다시 해도 같다 — 툴 실패(patch_mismatch)로 끝낸다."""
    if refused.code != "render_mismatch" or not isinstance(ctx.change, Patch):
        return refused
    file = kustomization_file(ctx.app_dir)
    try:
        parsed = YamlFile(file.read_text(encoding="utf-8")) if file else None
    except ValueError:
        return refused
    kustomization = parsed.docs[0] if parsed and parsed.docs and isinstance(parsed.docs[0], dict) else {}
    transformers = [str(t) for t in kustomization.get("transformers") or []]
    if not transformers or "다른 환경" in refused.message:
        return refused
    return Refused(code="patch_mismatch",
                   message=f"{refused.message} 그 칸은 이 overlay 의 transformers:({', '.join(transformers[:3])})가 "
                           "patch 뒤에 다시 정해요 — 거기를 고쳐야 해요.")


class _NullRepresenter(RoundTripRepresenter):
    """None 을 빈칸 대신 `null` 로 — 이 클래스에만 건다 (다른 YAML 쓰기는 그대로)."""


_NullRepresenter.add_representer(type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"))


def _dump(doc: Any, header: str) -> str:
    """새 patch 파일 — 값은 레포 모양으로(yes·on 같은 문자열은 따옴표), 지우는 칸은 `null` 로 보이게."""
    yaml_ = YAML()
    yaml_.Representer = _NullRepresenter
    yaml_.width = 4096
    yaml_.indent(mapping=2, sequence=4, offset=2)
    out = io.StringIO()
    yaml_.dump(patch.to_yaml(doc), out)
    return header + out.getvalue()
