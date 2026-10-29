"""plain YAML(ArgoCD Directory) — Application path 아래 문서에서 apiVersion·kind·name(·namespace)이 같은 것을 코드로 찾아 고친다."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from kukie_agent.manifests import compare
from kukie_agent.manifests.models import Env, ImageTag, Refused, Replicas, Resources
from kukie_agent.manifests.render import plain_files
from kukie_agent.manifests.workload import POD_SPEC, dig, with_tag
from kukie_agent.manifests.yamlfile import YamlFile, remove_item

if TYPE_CHECKING:
    from kukie_agent.manifests.change import Context


def propose(ctx: Context) -> dict[str, str] | Refused:
    found: list[tuple[str, YamlFile, Any]] = []
    for file in plain_files(ctx.app_dir, recurse=ctx.app.directory_recurse):
        try:
            parsed = YamlFile(file.read_text(encoding="utf-8"))
        except ValueError as exc:
            return Refused(code="render_failed", message=f"{ctx.relative(file)} 를 고칠 수 있게 읽지 못했어요. {exc}")
        for doc in parsed.docs:
            if isinstance(doc, dict) and compare.key_of(doc) == ctx.key:
                found.append((ctx.relative(file), parsed, doc))
    if len(found) != 1:
        where = ", ".join(path for path, _, _ in found) or "없음"
        return Refused(code="ambiguous", message=f"{compare.label(ctx.key)} 문서가 {len(found)}개예요({where}).")
    path, parsed, doc = found[0]
    edit(doc, ctx)
    if not parsed.keeps_shape():
        ctx.warnings.append(f"{path} 는 다시 쓰면 들여쓰기·따옴표 모양이 조금 바뀌어요 — diff 에 같이 보여요.")
    return {path: parsed.dump()}


def edit(doc: Any, ctx: Context) -> None:
    """ruamel 문서 하나를 요청대로 고친다 (Kustomize patch 파일에도 쓴다)."""
    change = ctx.change
    if isinstance(change, Replicas):
        _child(doc, "spec")["replicas"] = change.count
        return
    container = _container(doc, ctx)
    if isinstance(change, ImageTag):
        container["image"] = with_tag(str(container["image"]), change.tag)
    elif isinstance(change, Resources):
        resources = _child(container, "resources")
        for section, values in (("requests", change.requests), ("limits", change.limits)):
            if values:
                target = _child(resources, section)
                for key, value in values.items():
                    target[key] = value
    elif isinstance(change, Env):
        env = container.get("env")
        if not isinstance(env, list):
            env = container["env"] = CommentedSeq()
        for name, value in change.values.items():
            item = next((e for e in env if e.get("name") == name), None)
            if item is None:
                env.append(CommentedMap([("name", name), ("value", value)]))
            else:
                item.pop("valueFrom", None)
                item["value"] = value
        for name in change.remove:
            while (index := next((i for i, e in enumerate(env) if e.get("name") == name), None)) is not None:
                remove_item(container, "env", index)


def _child(parent: Any, key: str) -> Any:
    if not isinstance(parent.get(key), dict):
        parent[key] = CommentedMap()
    return parent[key]


def _container(doc: Any, ctx: Context) -> Any:
    found = dig(doc, (*POD_SPEC[ctx.target.kind], "containers"))
    return next(c for c in found if c.get("name") == ctx.container)
