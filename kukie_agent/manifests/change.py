"""고칠 곳 찾기 + 렌더 비교 — DURO-133.

    plan_change   = prepare → (형식별) propose → verify
    verify_change = prepare → verify   ← 고칠 파일을 다른 데서(나중에 AI 가) 고른 경우

prepare 는 고치기 전 렌더 결과에서 요청한 리소스를 찾고 함정(HPA · git 에 원본 없음 · Secret · ignoreDifferences)을 먼저 본다.
verify 는 레포를 임시 폴더에 복사해 고친 파일을 쓰고 다시 렌더링한 뒤, 바뀐 것이 요청한 리소스의 요청한 칸뿐인지 본다.
Kustomize 는 레포의 다른 kustomization 폴더(base · 다른 overlay)도 전·후를 렌더링해 같이 바뀌지 않았는지 본다.
레포 폴더는 읽기만 한다 — 고친 내용은 결과(Ready.files)로만 나간다. git · GitHub · PR 은 이 모듈 밖이다 (DURO-131 · 134).
"""
from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kukie_agent.manifests import compare, kustomize, plain
from kukie_agent.manifests.compare import FieldPath, Key
from kukie_agent.manifests.models import (
    AppSource, Change, Env, ImageTag, Other, Ready, Refused, Replicas, Resources, Target,
)
from kukie_agent.manifests.render import (
    RenderError, kustomization_file, kustomize_roots, render, resolve_inside,
)
from kukie_agent.manifests.workload import POD_SPEC, SCALABLE, container, containers, dig, split_image

# 값이 비밀번호·키처럼 보이는 환경변수 이름 — git 에 평문으로 남기지 않는다
_SECRETISH = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL", re.I)
_SHOW_LIMIT = 5


@dataclass
class Context:
    root: Path
    app: AppSource
    app_dir: Path
    target: Target
    change: Change
    before: dict[Key, dict]
    key: Key                   # 렌더 결과에서 찾은 요청한 리소스
    obj: dict                  # 그 리소스의 고치기 전 렌더 결과
    container: str | None      # 컨테이너 칸을 고치는 요청이면 그 이름
    allowed: FieldPath         # 바뀌어도 되는 칸 — 이 아래만 바뀌어야 한다
    warnings: list[str] = field(default_factory=list)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root.resolve()).as_posix()


# 형식별로 고칠 파일을 고르는 곳. Helm(DURO-137)은 여기와 render.render 에 한 줄씩 더한다
Proposer = Callable[[Context], "dict[str, str] | Refused"]
PROPOSERS: dict[str, Proposer] = {"Directory": plain.propose, "Kustomize": kustomize.propose}


def plan_change(repo: Path, app: AppSource, target: Target, change: Change, *,
                hpa: str | None = None) -> Ready | Refused:
    """레포(로컬에 받아 둔 폴더)에서 고칠 곳을 코드로 골라 고치고, 렌더 비교로 확인한다.

    hpa: 툴이 클러스터에서 본, 이 리소스를 맡은 HPA 이름 — git 에 없는 HPA 도 레플리카를 정한다.
    """
    ctx = prepare(repo, app, target, change, hpa=hpa)
    if isinstance(ctx, Refused):
        return ctx
    edits = PROPOSERS[app.source_type](ctx)
    if isinstance(edits, Refused):
        return edits
    return verify(ctx, edits)


def verify_change(repo: Path, app: AppSource, target: Target, change: Change,
                  edits: dict[str, str], *, hpa: str | None = None) -> Ready | Refused:
    """누가 골랐든 고친 파일(레포 루트 기준 경로 → 새 내용)이 요청한 것만 바꾸는지 렌더 비교로 확인한다."""
    ctx = prepare(repo, app, target, change, hpa=hpa)
    if isinstance(ctx, Refused):
        return ctx
    return verify(ctx, edits)


# ── 고치기 전 ─────────────────────────────────────────────


def prepare(repo: Path, app: AppSource, target: Target, change: Change, *,
            hpa: str | None = None) -> Context | Refused:
    if app.source_type == "Helm":
        return Refused(code="unsupported_source",
                       message="Helm 으로 배포한 앱은 아직 PR 로 못 고쳐요 (values 지원은 다음 단계예요).")
    if app.source_type not in PROPOSERS:
        return Refused(code="unsupported_source",
                       message=f"ArgoCD 형식이 '{app.source_type or '알 수 없음'}' 이라 PR 로 못 고쳐요. "
                               "plain YAML(Directory) 과 Kustomize 만 돼요.")
    if isinstance(change, Other):
        return Refused(code="unsupported_change",
                       message=f"'{change.description}' 은 아직 PR 로 못 해요. "
                               "PR 로는 레플리카 수 · 이미지 태그 · CPU·메모리 · 환경변수만 바꿔요.")
    if target.kind == "Secret":
        return _secret()

    app_dir = resolve_inside(repo, app.path or ".")
    if app_dir is None or not app_dir.is_dir():
        return Refused(code="invalid_path", message=f"레포에 Application path '{app.path}' 폴더가 없어요.")
    if app.source_type == "Kustomize" and kustomization_file(app_dir) is None:
        return Refused(code="render_failed",
                       message=f"'{app.path}' 에 kustomization.yaml 이 없어요 — Kustomize 로 렌더링할 수 없어요.")
    try:
        before = compare.index(render(app.source_type, app_dir, root=repo, recurse=app.directory_recurse))
    except RenderError as exc:
        return Refused(code="render_failed", message=f"지금 레포를 렌더링하지 못했어요. {exc}")

    keys = compare.find(before, target)
    if not keys:
        return Refused(code="no_source",
                       message=f"{target.label()} 의 원본이 git('{app.path}')에 없어요. "
                               "오퍼레이터 등이 만든 리소스라 PR 로 고칠 곳이 없어요.")
    if len(keys) > 1:
        return Refused(code="ambiguous", retry=True,
                       message=f"{target.label()} 이 여러 namespace 에 있어요 "
                               f"({', '.join(compare.label(k) for k in keys)}) — namespace 를 알려 주세요.")
    key = keys[0]
    obj = before[key]

    if isinstance(change, Replicas):
        if target.kind not in SCALABLE:
            return Refused(code="unsupported_change", message=f"{target.kind} 는 레플리카 수가 없어요.")
        scaler = hpa or _scaler_in_git(before, key)
        if scaler:
            return Refused(code="hpa",
                           message=f"{target.label()} 의 레플리카 수는 HPA({scaler})가 정해요. "
                                   "git 의 replicas 를 바꿔도 HPA 가 되돌려요 — HPA 의 최소·최대값을 바꿔야 해요.")
        current = dig(obj, ("spec", "replicas"))
        if (1 if current is None else current) == change.count:   # 비어 있으면 쿠버네티스 기본값 1
            return _no_change(target, f"레플리카가 이미 {change.count} 예요.")
        ctx = Context(repo, app, app_dir, target, change, before, key, obj, None, ("spec", "replicas"))
        return _check_ignored(ctx)

    if target.kind not in POD_SPEC:
        return Refused(code="unsupported_change", message=f"{target.kind} 에는 컨테이너가 없어요.")
    name = _pick_container(obj, target.kind, change.container)
    if isinstance(name, Refused):
        return name
    current = container(obj, target.kind, name) or {}
    base: FieldPath = (*POD_SPEC[target.kind], "containers", compare.item(name))

    if isinstance(change, ImageTag):
        allowed = (*base, "image")
        _, tag, digest = split_image(str(current.get("image", "")))
        if tag == change.tag and not digest:
            return _no_change(target, f"{name} 이미지 태그가 이미 {change.tag} 예요.")
    elif isinstance(change, Resources):
        allowed = (*base, "resources")
        if all(str(dig(current, ("resources", section, k))) == v
               for section, values in (("requests", change.requests), ("limits", change.limits))
               for k, v in values.items()):
            return _no_change(target, f"{name} 의 resources 가 이미 그 값이에요.")
    else:
        allowed = (*base, "env")
        refused = _check_env(current, change, name)
        if refused:
            return refused
        change = change.model_copy(update={"remove": [n for n in change.remove if _env_item(current, n)]})
        if all((_env_item(current, n) or {}).get("value") == v for n, v in change.values.items()) \
                and not change.remove:
            return _no_change(target, f"{name} 의 환경변수가 이미 그 값이에요.")

    ctx = Context(repo, app, app_dir, target, change, before, key, obj, name, allowed)
    return _check_ignored(ctx)


def _secret() -> Refused:
    return Refused(code="secret",
                   message="Secret 값은 PR 에 넣지 않아요 — git 에 평문으로 남아요. "
                           "Sealed Secrets 나 External Secrets 로 바꿔 주세요.")


def _no_change(target: Target, why: str) -> Refused:
    return Refused(code="no_change", message=f"{target.label()}: {why} 바꿀 게 없어요.")


def _scaler_in_git(objects: dict[Key, dict], key: Key) -> str | None:
    """같은 렌더 결과 안에서 이 리소스를 맡은 HPA(또는 KEDA ScaledObject)."""
    _, kind, namespace, name = key
    for other, obj in objects.items():
        if other[1] not in ("HorizontalPodAutoscaler", "ScaledObject") or other[2] != namespace:
            continue
        ref = dig(obj, ("spec", "scaleTargetRef")) or {}
        if ref.get("name") == name and ref.get("kind", "Deployment") == kind:
            return other[3]
    return None


def _pick_container(obj: dict, kind: str, wanted: str | None) -> str | Refused:
    names = [c.get("name") for c in containers(obj, kind)]
    if wanted:
        if wanted in names:
            return wanted
        return Refused(code="ambiguous", retry=True,
                       message=f"컨테이너 '{wanted}' 가 없어요. 있는 것: {', '.join(map(str, names)) or '없음'}.")
    if len(names) == 1:
        return names[0]
    return Refused(code="ambiguous", retry=True,
                   message=f"컨테이너가 여럿이에요({', '.join(map(str, names))}) — 어느 것인지 골라 주세요.")


def _env_item(current: dict, name: str) -> dict | None:
    return next((e for e in current.get("env") or [] if isinstance(e, dict) and e.get("name") == name), None)


def _check_env(current: dict, change: Env, name: str) -> Refused | None:
    if any(_SECRETISH.search(env_name) for env_name in change.values):
        return _secret()
    for env_name in [*change.values, *change.remove]:
        found = _env_item(current, env_name) or {}
        source = found.get("valueFrom")
        if isinstance(source, dict) and "secretKeyRef" in source:
            return _secret()
        if source:
            return Refused(code="unsupported_change",
                           message=f"{name} 의 {env_name} 은 ConfigMap 등 다른 곳에서 값을 가져와요 — "
                                   "그 원본을 바꿔야 해서 아직 PR 로 못 해요.")
    return None


def _check_ignored(ctx: Context) -> Context | Refused:
    """ArgoCD 가 ignoreDifferences 로 무시하는 칸이면 git 을 고쳐도 클러스터에 반영되지 않을 수 있다."""
    group = ctx.target.group
    _, kind, namespace, name = ctx.key
    for rule in ctx.app.ignore_differences:
        if (rule.get("group") or "") != group or rule.get("kind") != kind:
            continue
        if rule.get("name") and rule["name"] != name:
            continue
        if rule.get("namespace") and namespace and rule["namespace"] != namespace:
            continue
        for pointer in rule.get("jsonPointers") or []:
            ignored = compare.pointer_path(pointer, ctx.obj)
            if ignored is None:
                continue
            if compare.under(ctx.allowed, ignored):
                return Refused(code="ignored_field",
                               message=f"ArgoCD 가 {compare.label(ctx.key)} 의 {pointer} 를 무시해요"
                                       "(ignoreDifferences) — git 을 고쳐도 클러스터에 반영되지 않아요.")
            if compare.under(ignored, ctx.allowed):
                ctx.warnings.append(f"ArgoCD 가 {pointer} 는 무시해요(ignoreDifferences) — 그 부분은 반영되지 않을 수 있어요.")
        if rule.get("jqPathExpressions"):
            ctx.warnings.append("ignoreDifferences 에 jq 식이 있어요 — 무시하는 칸인지 코드가 확인하지 못했어요.")
        if rule.get("managedFieldsManagers"):
            ctx.warnings.append("ignoreDifferences 가 다른 관리자가 쓴 칸을 무시해요 — 반영되는지 sync 뒤에 확인해야 해요.")
    return ctx


# ── 고친 뒤 ───────────────────────────────────────────────


def verify(ctx: Context, edits: dict[str, str]) -> Ready | Refused:
    files: dict[str, str] = {}
    originals: dict[str, str | None] = {}
    for path, content in edits.items():
        found = resolve_inside(ctx.root, path)
        if found is None or found.is_dir():
            return Refused(code="invalid_path", retry=True, message=f"'{path}' 는 레포 안 파일이 아니에요.")
        relative = ctx.relative(found)
        original = found.read_text(encoding="utf-8") if found.is_file() else None
        if original != content:
            files[relative], originals[relative] = content, original
    if not files:
        return Refused(code="render_mismatch", retry=True, message="고친 파일이 없어요.")

    with tempfile.TemporaryDirectory(prefix="kukie-manifests-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(ctx.root, copy, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        for relative, content in files.items():
            (copy / relative).parent.mkdir(parents=True, exist_ok=True)
            (copy / relative).write_text(content, encoding="utf-8")
        app_path = ctx.relative(ctx.app_dir)
        try:
            after = compare.index(render(ctx.app.source_type, copy / app_path, root=copy,
                                         recurse=ctx.app.directory_recurse))
        except RenderError as exc:
            return Refused(code="render_failed", retry=True, message=f"고친 뒤 렌더링이 안 돼요. {exc}")

        refused = _compare_app(ctx, after)
        if refused:
            return refused
        if ctx.app.source_type == "Kustomize":
            changed = _other_environments(ctx, copy, app_path)
            if changed:
                return Refused(code="render_mismatch", retry=True,
                               message="다른 환경도 같이 바뀌어요: " + ", ".join(changed[:_SHOW_LIMIT])
                                       + f". '{app_path}' 안에서만 고쳐 주세요 — base 는 다른 환경이 같이 써요.")

    outside = [p for p in files if not (ctx.root.resolve() / p).is_relative_to(ctx.app_dir)]
    if outside:
        return Refused(code="outside_app", retry=True,
                       message=f"Application path('{app_path}') 밖 파일을 고쳤어요: {', '.join(outside)}. "
                               "그 폴더 안에서만 고쳐 주세요.")

    label = compare.label(ctx.key)
    return Ready(
        files=files,
        file_diff="".join(
            compare.unified(originals[p], files[p], f"a/{p}" if originals[p] is not None else "/dev/null", f"b/{p}")
            for p in sorted(files)),
        render_diff=compare.unified(compare.dump(ctx.obj), compare.dump(after[ctx.key]),
                                    f"{label} (지금)", f"{label} (머지 후)"),
        warnings=ctx.warnings,
    )


def _compare_app(ctx: Context, after: dict[Key, dict]) -> Refused | None:
    changed = [k for k in sorted(set(ctx.before) | set(after)) if ctx.before.get(k) != after.get(k)]
    # Secret 이 바뀌는 diff 는 만들지 않는다 — 어떤 이유로든 Secret 이 바뀌면 그 내용은 싣지 않고 멈춘다
    if any(k[1] == "Secret" for k in changed):
        return _secret()
    others = [k for k in changed if k != ctx.key]
    if others:
        return Refused(code="render_mismatch", retry=True,
                       message="요청하지 않은 리소스도 바뀌어요: "
                               + ", ".join(_describe(k, ctx.before, after) for k in others[:_SHOW_LIMIT])
                               + f". {compare.label(ctx.key)} 만 바뀌게 고쳐 주세요.")
    if ctx.key not in after:
        return Refused(code="render_mismatch", retry=True, message=f"고친 뒤 {compare.label(ctx.key)} 가 사라져요.")
    paths = compare.diff_paths(ctx.obj, after[ctx.key])
    if not paths:
        return Refused(code="render_mismatch", retry=True,
                       message="고친 뒤에도 렌더 결과가 그대로예요 — 고친 곳이 이 리소스에 반영되지 않아요.")
    stray = [p for p in paths if not compare.under(p, ctx.allowed)]
    if stray:
        return Refused(code="render_mismatch", retry=True,
                       message="요청하지 않은 칸도 바뀌어요: " + ", ".join(compare.show(p) for p in stray[:_SHOW_LIMIT])
                               + f". {compare.show(ctx.allowed)} 만 바뀌어야 해요.")
    problem = _applied(ctx, after[ctx.key])
    if problem:
        return Refused(code="render_mismatch", retry=True, message=f"고친 뒤 값이 요청과 달라요: {problem}")
    return None


def _describe(key: Key, before: dict[Key, dict], after: dict[Key, dict]) -> str:
    if key not in before:
        return f"{compare.label(key)}(새로 생김)"
    if key not in after:
        return f"{compare.label(key)}(사라짐)"
    return compare.label(key)


def _applied(ctx: Context, obj: dict) -> str | None:
    change = ctx.change
    if isinstance(change, Replicas):
        got = dig(obj, ("spec", "replicas"))
        return None if got == change.count else f"replicas 가 {got} 예요 (요청 {change.count})."
    current = container(obj, ctx.target.kind, ctx.container or "") or {}
    if isinstance(change, ImageTag):
        image = str(current.get("image", ""))
        _, tag, digest = split_image(image)
        return None if tag == change.tag and not digest else f"이미지가 {image} 예요 (요청 태그 {change.tag})."
    if isinstance(change, Resources):
        for section, values in (("requests", change.requests), ("limits", change.limits)):
            for k, v in values.items():
                got = dig(current, ("resources", section, k))
                if str(got) != v:
                    return f"resources.{section}.{k} 가 {got} 예요 (요청 {v})."
        return None
    assert isinstance(change, Env)
    for name, value in change.values.items():
        found = _env_item(current, name)
        if not found or found.get("value") != value or "valueFrom" in found:
            return f"환경변수 {name} 가 요청한 값이 아니에요."
    for name in change.remove:
        if _env_item(current, name):
            return f"환경변수 {name} 가 아직 있어요."
    return None


def _other_environments(ctx: Context, copy: Path, app_path: str) -> list[str]:
    """레포의 다른 kustomization 폴더 중 고친 뒤 렌더 결과가 달라지는 곳 (전·후 모두 렌더링이 안 되는 곳은 뺀다)."""
    changed = []
    for root in kustomize_roots(ctx.root):
        if root == app_path:
            continue
        results = []
        for base in (ctx.root, copy):
            try:
                results.append(compare.index(render("Kustomize", base / root, root=base)))
            except RenderError:
                results.append(None)
        if results[0] is None and results[1] is None:
            continue
        if results[0] != results[1]:
            changed.append(root)
    return changed
