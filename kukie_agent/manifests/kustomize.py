"""Kustomize — Application path(환경 overlay)만 고친다. base 는 다른 환경도 같이 쓰므로 건드리지 않는다.

레플리카 수는 kustomization 의 `replicas:`, 이미지 태그는 `images:` 칸을 고친다.
resources · 환경변수는 그 폴더에 Kukie 의 patch 파일(kukie-<kind>-<name>-<요청>.yaml)을 두고 `patches:` 에 건다.
같은 요청이 다시 오면 새 파일을 만들지 않고 그 파일을 고친다. patch 는 맨 뒤에 걸려 앞의 patch 보다 나중에 적용된다.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from kukie_agent.manifests import plain
from kukie_agent.manifests.models import Env, ImageTag, Refused, Replicas, Resources
from kukie_agent.manifests.render import kustomization_file
from kukie_agent.manifests.workload import POD_SPEC, container, split_image
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
    else:
        assert isinstance(change, Resources | Env)
        name = f"kukie-{ctx.target.kind.lower()}-{ctx.target.name}-{change.what}.yaml"
        patch = ctx.app_dir / name
        try:
            edits[ctx.relative(patch)] = _patch(ctx, patch.read_text(encoding="utf-8") if patch.is_file() else None)
        except ValueError as exc:
            return Refused(code="render_failed", message=f"{ctx.relative(patch)} 를 고칠 수 있게 읽지 못했어요. {exc}")
        _add_patch(kustomization, name, ctx)

    if not parsed.keeps_shape():
        ctx.warnings.append(f"{ctx.relative(file)} 는 다시 쓰면 들여쓰기·따옴표 모양이 조금 바뀌어요 — diff 에 같이 보여요.")
    edits[ctx.relative(file)] = parsed.dump()
    return edits


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


def _add_patch(kustomization: Any, name: str, ctx: Context) -> None:
    patches = _list(kustomization, "patches")
    if any(isinstance(p, dict) and p.get("path") == name for p in patches):
        return
    # target 으로 고르면 patch 안의 이름·namespace 와 상관없이 그 리소스에 걸린다 (overlay 가 namespace 를 바꿔도)
    target = CommentedMap()
    if ctx.target.group:
        target["group"] = ctx.target.group
    target["version"] = ctx.target.api_version.split("/")[-1]
    target["kind"] = ctx.target.kind
    target["name"] = ctx.target.name
    patches.append(CommentedMap([("path", name), ("target", target)]))
