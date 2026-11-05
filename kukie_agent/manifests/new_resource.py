"""새 리소스 더하기 + 렌더 비교 — DURO-154.

이미 있는 ArgoCD 앱의 폴더에 새 파일을 더한다. plain 은 파일 하나, Kustomize 는 그 폴더에 파일 + `resources:` 한 줄이다.

**기본은 허용이다.** 종류 허용 목록은 두지 않는다. 막는 것은 아래뿐이고, 막을 때는 다음 방법을 문구에 담는다.
  - 앱 폴더 밖 쓰기 · 기존 파일 덮어쓰기
  - 렌더 비교에서 기존 리소스가 바뀌거나 요청하지 않은 리소스가 생김 · 렌더 실패
  - 값이 든 Secret · 레포 밖을 끌어올 수 있는 Kustomize 설정(Kustomization 종류, 레포에 이미 있는 원격 resources 는 렌더가 거절한다)
그 밖에 봐 둘 것(클러스터 범위 · 다른 네임스페이스 · privileged · hostPath · hostNetwork · latest·태그 없는 이미지)은 경고로 올린다.
경고는 렌더 결과를 본다 — Kustomize 가 네임스페이스·이미지 태그를 바꾸면 바뀐 값이 클러스터에 올라갈 값이다.

레포 폴더는 읽기만 한다 — 고친 내용은 결과(Ready.files)로만 나간다. git · GitHub · PR 은 이 모듈 밖이다.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from kukie_agent.manifests import compare, kustomize
from kukie_agent.manifests.change import _SECRETISH, _SHOW_LIMIT, busy, other_environments
from kukie_agent.manifests.compare import FieldPath, Key
from kukie_agent.manifests.models import (
    KIND_SHAPE, NAME_SHAPE, AppSource, NewResource, Ready, Refused, RiskWarning, Target, WarningLocation,
)
from kukie_agent.manifests.render import (
    KUSTOMIZATION_FILES, RenderError, RenderTimeout, kustomization_file, render, resolve_inside,
)
from kukie_agent.manifests.workload import split_image

_FILE_SUFFIXES = (".yaml", ".yml")
#: 파일 이름 길이 — 쿠버네티스 이름은 253자까지라 그대로 쓰면 파일 시스템 한도(255바이트)를 넘는다
_FILE_NAME_LIMIT = 200
#: 폴더에 있으면 ArgoCD 가 앱 형식을 다르게 읽는 파일 이름
_RESERVED_FILES = (*KUSTOMIZATION_FILES, "Chart.yaml")
#: 네임스페이스가 없다고 아는 종류. 이름으로 짐작하지 않는다 — ClusterServiceVersion 처럼 네임스페이스가 있는 종류도 있고,
#: 틀리면 namespace 칸이 비어 같은 이름을 못 찾는다(agent#132 리뷰). 모르는 커스텀 리소스는 경고하지 않고, 막는 데도 쓰지 않는다
_CLUSTER_SCOPED = frozenset({
    "ClusterRole", "ClusterRoleBinding", "ClusterIssuer", "ClusterSecretStore", "ClusterExternalSecret",
    "CustomResourceDefinition", "Namespace", "Node", "PersistentVolume", "StorageClass", "PriorityClass",
    "IngressClass", "RuntimeClass", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration",
    "APIService", "CSIDriver", "VolumeSnapshotClass",
})
_CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")
_VALUE_LIMIT = 200
#: 쿠버네티스 이름 규칙. 렌더 비교는 YAML 만 읽어 못 거른다 — 그대로 올리면 머지한 뒤 sync 가 실패한다
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_DNS_1035_LABEL = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
_SUBDOMAIN_LIMIT = 253
_LABEL_LIMIT = 63
_RBAC_GROUP = "rbac.authorization.k8s.io"
#: 막을 때 공통으로 주는 다음 방법
_OR_GITHUB = "이대로 올려야 한다면 GitHub 에서 직접 PR 을 올려 주세요."


def plan_new_resource(repo: Path, app: AppSource, target: Target, request: NewResource) -> Ready | Refused:
    """레포(로컬에 받아 둔 폴더)에 새 파일을 더한 모양을 만들고 렌더 비교로 확인한다.

    target 의 kind·name 은 Plan 이 대표로 적는 리소스, namespace 는 앱의 destination 네임스페이스다.
    """
    if app.source_type not in ("Directory", "Kustomize"):
        return Refused(code="unsupported_source",
                       message=f"ArgoCD 형식이 '{app.source_type or '알 수 없음'}' 이라 새 파일을 PR 로 못 더해요. "
                               "plain YAML(Directory) 과 Kustomize 만 돼요.")
    docs = _parse(request.manifest_yaml, target)
    if isinstance(docs, Refused):
        return docs
    if (blocked := _blocked_kind(docs)) is not None:
        return blocked

    app_dir = resolve_inside(repo, app.path or ".")
    if app_dir is None or not app_dir.is_dir():
        return Refused(code="invalid_path", message=f"레포에 Application path '{app.path}' 폴더가 없어요.")
    if app.source_type == "Kustomize" and kustomization_file(app_dir) is None:
        return Refused(code="render_failed",
                       message=f"'{app.path}' 에 kustomization.yaml 이 없어요 — Kustomize 로 렌더링할 수 없어요.")
    place = _new_file(app, app_dir, request.file_name or _default_name(docs[0]))
    if isinstance(place, Refused):
        return place

    root = repo.resolve()
    relative = place.relative_to(root).as_posix()
    entry = place.relative_to(app_dir).as_posix()
    files = {relative: request.manifest_yaml.rstrip("\n") + "\n"}
    originals: dict[str, str | None] = {relative: None}
    warnings: list[str] = []
    if app.source_type == "Kustomize":
        file = kustomization_file(app_dir)
        assert file is not None
        name = file.relative_to(root).as_posix()
        original = file.read_text(encoding="utf-8")
        try:
            files[name], same_shape = kustomize.add_resource(original, entry)
        except ValueError as exc:
            return Refused(code="render_failed", retry=True,
                           message=f"{name} 를 고칠 수 있게 읽지 못했어요. {exc} 그 파일을 먼저 고쳐 주세요.")
        originals[name] = original
        if not same_shape:
            warnings.append(f"{name} 는 다시 쓰면 들여쓰기·따옴표 모양이 조금 바뀌어요 — diff 에 같이 보여요.")

    try:
        before = compare.index(render(app.source_type, app_dir, root=repo, recurse=app.directory_recurse))
    except RenderTimeout as exc:
        return busy(exc)
    except RenderError as exc:
        return Refused(code="render_failed", retry=True,
                       message=f"지금 레포를 렌더링하지 못했어요. {exc} 레포의 이 문제를 먼저 고치거나, 다른 앱을 골라 주세요.")
    if (exists := _already_in_app(docs, before)) is not None:
        return exists

    with tempfile.TemporaryDirectory(prefix="kukie-manifests-") as tmp:
        workdir = Path(tmp) / "repo"
        shutil.copytree(root, workdir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        for path, content in files.items():
            (workdir / path).parent.mkdir(parents=True, exist_ok=True)
            (workdir / path).write_text(content, encoding="utf-8")
        app_path = app_dir.relative_to(root).as_posix()
        try:
            after = compare.index(render(app.source_type, workdir / app_path, root=workdir,
                                         recurse=app.directory_recurse))
        except RenderTimeout as exc:
            return busy(exc)
        except RenderError as exc:
            return Refused(code="render_failed", retry=True,
                           message=f"새 파일을 더하면 렌더링이 안 돼요. {exc} 매니페스트를 고쳐 다시 제안해 주세요. {_OR_GITHUB}")
        created = _compare(docs, before, after)
        if isinstance(created, Refused):
            return created
        if app.source_type == "Kustomize":
            try:
                changed = other_environments(root, workdir, app_path)
            except RenderTimeout as exc:
                return busy(exc, "다른 환경과 비교하다 ")
            if changed:
                return Refused(code="render_mismatch", retry=True,
                               message="다른 환경도 같이 바뀌어요: " + ", ".join(changed[:_SHOW_LIMIT])
                                       + f". '{app_path}' 를 base 로 쓰는 곳이 있어요 — 그 환경의 overlay 앱을 골라 주세요.")

    resources = []
    risks = []
    if app.source_type == "Directory" and app.directory_filtered:
        risks.append(_risk("directory_filter", relative, app.path or ".", "spec.source.directory", None,
                           "이 앱은 읽을 파일을 include·exclude 로 골라요. 새 파일이 거기서 빠지면 머지해도 클러스터에 안 생겨요.",
                           "앱의 include·exclude 설정이 이 파일 이름을 받는지 확인하세요."))
    for key, obj in created:
        # 네임스페이스를 안 적은 문서는 앱의 destination 으로 들어간다. 클러스터 범위 리소스는 네임스페이스가 없다
        namespace = "" if key[1] in _CLUSTER_SCOPED else key[2] or target.namespace or ""
        resources.append({"api_version": key[0], "kind": key[1], "name": key[3], "namespace": namespace,
                          "file": relative})
        risks += _risks(obj, (key[0], key[1], namespace, key[3]), relative, target.namespace)
    return Ready(
        files=files,
        file_diff="".join(
            compare.unified(originals[p], files[p], f"a/{p}" if originals[p] is not None else "/dev/null", f"b/{p}")
            for p in sorted(files)),
        render_diff="".join(
            compare.unified(None, compare.dump(obj), "/dev/null", f"{compare.label(key)} (머지 후)")
            for key, obj in created),
        warnings=[*warnings, *(risk.text for risk in risks)],
        risk_warnings=risks,
        new_resources=resources,
    )


# ── 입력 ──────────────────────────────────────────────────


def _again(message: str) -> Refused:
    return Refused(code="invalid_manifest", retry=True, message=message)


def _parse(text: str, target: Target) -> list[dict] | Refused:
    try:
        loaded = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    except yaml.YAMLError as exc:
        # 오류 문구에는 값 조각이 실린다 — 줄 번호만 준다 (render.parse_objects 와 같다)
        mark = getattr(exc, "problem_mark", None)
        return _again(f"manifest_yaml 을 YAML 로 읽지 못했어요{f' ({mark.line + 1}번째 줄)' if mark else ''}.")
    if not loaded:
        return _again("manifest_yaml 에 리소스가 없어요.")
    for doc in loaded:
        meta = doc.get("metadata") if isinstance(doc, dict) else None
        name = meta.get("name") if isinstance(meta, dict) else None
        if not (isinstance(doc, dict) and isinstance(doc.get("apiVersion"), str) and isinstance(doc.get("kind"), str)
                and KIND_SHAPE.match(doc["kind"]) and isinstance(name, str) and NAME_SHAPE.match(name)):
            return _again("manifest_yaml 의 문서마다 apiVersion · kind · metadata.name 이 있어야 해요.")
        if (problem := _bad_name(doc)) is not None:
            return _again(problem)
        # List 안의 항목은 다른 검사(Secret 등)를 지나가므로 풀어서 받는다
        if doc["kind"].endswith("List") and isinstance(doc.get("items"), list):
            return _again(f"{doc['kind']} 는 항목을 문서(---)로 나눠 다시 주세요.")
    if not any(_same_kind(doc["kind"], target.kind) and doc["metadata"]["name"] == target.name for doc in loaded):
        return _again(f"kind·name({target.kind} {target.name})이 manifest_yaml 에 없어요 — 그 안의 리소스 하나로 적어 주세요.")
    return loaded


def _bad_name(doc: dict) -> str | None:
    """쿠버네티스가 받지 않을 이름·네임스페이스면 고칠 방법을 담은 문구."""
    name = doc["metadata"]["name"]
    namespace = doc["metadata"].get("namespace")
    if namespace is not None and not (isinstance(namespace, str) and len(namespace) <= _LABEL_LIMIT
                                      and _DNS_LABEL.fullmatch(namespace)):
        return ("metadata.namespace 를 쿠버네티스 이름으로 못 써요. 소문자·숫자·'-' 로 63자 안에서 고치거나, "
                "앱의 네임스페이스로 들어가게 지워 주세요.")
    if doc["kind"] == "Service" and doc["apiVersion"] == "v1":
        rule, limit, how = _DNS_1035_LABEL, _LABEL_LIMIT, "소문자로 시작하고 소문자·숫자·'-' 로"
    elif doc["kind"] == "Namespace" and doc["apiVersion"] == "v1":
        rule, limit, how = _DNS_LABEL, _LABEL_LIMIT, "소문자·숫자·'-' 로"
    elif doc["apiVersion"].partition("/")[0] == _RBAC_GROUP:
        rule, limit, how = None, _SUBDOMAIN_LIMIT, "RBAC 이름 규칙에 맞게"     # system:foo 처럼 콜론·대문자를 받는다
    else:
        rule, limit, how = _DNS_SUBDOMAIN, _SUBDOMAIN_LIMIT, "소문자·숫자·'-'·'.' 로"
    if len(name) <= limit and (rule is None or rule.fullmatch(name)):
        return None
    shown = name if len(name) <= 60 else name[:60] + "…"
    return (f"{doc['kind']} 의 이름 '{shown}' 은 쿠버네티스가 받지 않아요 — 머지해도 sync 가 실패해요. "
            f"{how} {limit}자 안에서 고쳐 다시 제안해 주세요.")


def _same_kind(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _blocked_kind(docs: list[dict]) -> Refused | None:
    for doc in docs:
        kind = doc["kind"]
        group = doc["apiVersion"].partition("/")[0]
        if _same_kind(kind, "Secret") and (doc.get("data") or doc.get("stringData")):
            return Refused(code="secret", retry=True, message=(
                f"Secret {doc['metadata']['name']} 의 값이 git 에 평문으로 영원히 남아요. ExternalSecret 이나 SealedSecret 으로 "
                "바꿔 다시 제안하거나, 값은 클러스터에 따로 넣고 secretKeyRef 로 가리켜 주세요."))
        if _same_kind(kind, "Kustomization") or _same_kind(kind, "Component") or group == "kustomize.config.k8s.io":
            return Refused(code="kustomize_config", retry=True, message=(
                f"{kind} 는 레포 밖 내용을 끌어올 수 있어 Kukie 가 새로 더하지 않아요. 가져오려는 리소스의 YAML 을 직접 넣어 "
                f"다시 제안해 주세요. {_OR_GITHUB}"))
    return None


def _default_name(doc: dict) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", doc["metadata"]["name"])[:_FILE_NAME_LIMIT - len(".yaml")]
    return (name.strip(".-") or "resource") + ".yaml"


def _new_file(app: AppSource, app_dir: Path, name: str) -> Path | Refused:
    """새 파일의 실제 경로. 앱 폴더 밖이거나 이미 있으면 Refused."""
    path = PurePosixPath(name)
    outside = Refused(code="outside_app", retry=True,
                      message=f"'{name}' 는 앱 폴더('{app.path}') 밖이에요. 그 폴더 안의 파일 이름으로 다시 주세요. "
                              "다른 폴더에 더하려면 그 폴더를 보는 앱을 골라 주세요.")
    if "\\" in name or path.is_absolute() or ".." in path.parts:
        return outside
    if path.suffix not in _FILE_SUFFIXES or any(part.startswith(".") for part in path.parts):
        return _again(f"'{name}' 는 새 파일 이름으로 못 써요 — 점으로 시작하지 않는 .yaml 이나 .yml 로 주세요.")
    if any(len(part.encode("utf-8")) > _FILE_NAME_LIMIT for part in path.parts):
        return _again(f"파일 이름이 너무 길어요 — {_FILE_NAME_LIMIT}바이트 안으로 줄여 다시 주세요.")
    if path.name in _RESERVED_FILES:
        return Refused(code="kustomize_config", retry=True,
                       message=f"'{path.name}' 는 ArgoCD 가 앱 형식을 다르게 읽는 이름이에요. 다른 파일 이름으로 다시 주세요.")
    if len(path.parts) > 1 and app.source_type == "Directory" and not app.directory_recurse:
        return _again(f"이 앱은 하위 폴더를 읽지 않아요(directory.recurse 꺼짐) — '{name}' 는 ArgoCD 가 못 봐요. "
                      f"폴더 없이 '{path.name}' 로 다시 주세요.")
    candidate = app_dir / name
    if os.path.lexists(candidate):
        return Refused(code="overwrite", retry=True,
                       message=f"'{name}' 가 이미 앱 폴더에 있어요. 기존 파일은 덮어쓰지 않아요 — 다른 파일 이름으로 다시 주세요.")
    found = candidate.resolve()
    if not found.is_relative_to(app_dir):
        return outside
    for parent in found.parents:
        if parent == app_dir:
            break
        if parent.exists() and not parent.is_dir():
            return _again(f"'{parent.name}' 는 폴더가 아니라 파일이에요. 다른 파일 이름으로 다시 주세요.")
    return found


# ── 렌더 비교 ─────────────────────────────────────────────


def _group(api_version: str) -> str:
    return api_version.partition("/")[0] if "/" in api_version else ""


def _already_in_app(docs: list[dict], before: dict[Key, dict]) -> Refused | None:
    for doc in docs:
        wanted = doc["metadata"].get("namespace")
        for key in before:
            same_ns = not wanted or not key[2] or wanted == key[2]
            if (_group(key[0]), key[1], key[3]) == (_group(doc["apiVersion"]), doc["kind"], doc["metadata"]["name"]) and same_ns:
                return Refused(code="resource_exists", retry=True, message=(
                    f"{compare.label(key)} 는 이미 이 앱에 있어요. 새로 만들지 말고 change 의 replicas·image_tag·resources·env 로 "
                    f"고쳐 주세요. {_OR_GITHUB}"))
    return None


def _compare(docs: list[dict], before: dict[Key, dict], after: dict[Key, dict]) -> list[tuple[Key, dict]] | Refused:
    """기존 리소스는 그대로이고 새로 생긴 것이 요청한 것뿐인지. 맞으면 (요청 순서대로) 새로 생긴 렌더 결과."""
    changed = [key for key in before if after.get(key) != before[key]]
    if changed:
        # 기존 리소스의 내용은 싣지 않는다 — Secret 일 수 있다
        return Refused(code="render_mismatch", retry=True,
                       message="새 파일을 더하면 기존 리소스가 바뀌어요: " + ", ".join(map(compare.label, changed[:_SHOW_LIMIT]))
                               + f". 기존 리소스를 건드리지 않게 매니페스트를 고쳐 주세요. {_OR_GITHUB}")
    new = {key: obj for key, obj in after.items() if key not in before}
    created: list[tuple[Key, dict]] = []
    for doc in docs:
        key = _pick(doc, new)
        if key is None:
            return Refused(code="render_mismatch", retry=True,
                           message=f"{doc['kind']} {doc['metadata']['name']} 가 렌더 결과에 안 보여요 — 머지해도 클러스터에 안 생겨요. "
                                   "앱의 폴더 설정을 확인하거나 다른 앱을 골라 주세요.")
        created.append((key, new.pop(key)))
    if new:
        return Refused(code="render_mismatch", retry=True,
                       message="요청하지 않은 리소스도 생겨요: " + ", ".join(map(compare.label, list(new)[:_SHOW_LIMIT]))
                               + f". 요청한 리소스만 생기게 고쳐 주세요. {_OR_GITHUB}")
    return created


def _pick(doc: dict, new: dict[Key, dict]) -> Key | None:
    """그 문서가 렌더된 결과. Kustomize 의 namePrefix·nameSuffix 가 이름에 붙을 수 있어, 같은 이름이 없으면 그 이름을 품은 것을 본다."""
    name = doc["metadata"]["name"]
    same = [key for key in new if (key[0], key[1]) == (doc["apiVersion"], doc["kind"])]
    # 가장 짧은 것 — api·api-worker 를 같이 더하면 x-api-worker 도 api 를 품는다
    return (next((key for key in same if key[3] == name), None)
            or min((key for key in same if name in key[3]), key=lambda key: len(key[3]), default=None))


# ── 경고 ──────────────────────────────────────────────────


def _risk(code: str, file: str, resource: str, field: str | None, value: str | None, risk: str,
          suggestion: str) -> RiskWarning:
    if value is not None and len(value) > _VALUE_LIMIT:
        value = value[:_VALUE_LIMIT] + "…"
    where = " · ".join(part for part in (resource, field, value) if part)
    return RiskWarning(code=code, location=WarningLocation(file=file, resource=resource, field=field, value=value),
                       risk=risk, suggestion=suggestion, text=f"{where}: {risk} {suggestion}")


def representative(resources: list[dict[str, str]], kind: str, name: str) -> dict[str, str] | None:
    """요청이 대표로 적은 리소스의 렌더 결과. 종류까지 맞춘다 — Deployment·Service 는 이름이 같은 경우가 흔하다.
    이름은 _pick 처럼 본다 — namePrefix·nameSuffix 가 붙으면 그 이름을 품은 것 중 가장 짧은 것."""
    same = [r for r in resources if _same_kind(r["kind"], kind)]
    return (next((r for r in same if r["name"] == name), None)
            or min((r for r in same if name in r["name"]), key=lambda r: len(r["name"]), default=None))


def already_in_cluster(resource: dict[str, str]) -> RiskWarning:
    """git 에는 없지만 클러스터에 이미 있는 리소스 — 렌더 비교로는 안 보인다. 조회는 gitops/route.py 가 한다."""
    namespace = f"{resource['namespace']}/" if resource["namespace"] else ""
    return _risk("already_in_cluster", resource["file"], f"{resource['kind']} {namespace}{resource['name']}", None, None,
                 "클러스터에 같은 이름의 리소스가 이미 있어요. sync 하면 새로 생기는 게 아니라 지금 것을 이 내용으로 덮어써요.",
                 "지금 설정과 비교해 보고, 다른 리소스라면 이름을 바꿔 주세요.")


def _walk(node: Any, path: FieldPath = ()) -> Iterator[tuple[FieldPath, Any]]:
    """객체의 모든 칸 — 이름 있는 목록 항목은 `[이름]` 조각으로. 파드 스펙 위치를 종류로 고르지 않으니 커스텀 리소스도 같이 본다."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield (*path, str(key)), value
            yield from _walk(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            named = isinstance(value, dict) and isinstance(value.get("name"), str)
            yield from _walk(value, (*path, compare.item(value["name"]) if named else index))


def _risks(obj: dict, key: Key, file: str, destination: str | None) -> list[RiskWarning]:
    resource = compare.label(key)
    kind, namespace = key[1], key[2]
    found: list[RiskWarning] = []
    # 경고는 틀려도 막지 않으니 이름 짐작도 쓴다 (ClusterPolicy·ClusterTask …). namespace 칸은 아는 종류만 비운다 (plan_new_resource)
    if kind in _CLUSTER_SCOPED or kind.startswith("Cluster"):
        found.append(_risk("cluster_scoped", file, resource, "kind", kind,
                           "클러스터 전체에 영향을 줘요. 이 앱 밖의 리소스와 권한까지 바뀔 수 있어요.",
                           "꼭 클러스터 전체에 필요한지 확인하세요. 한 네임스페이스면 충분하면 Role·RoleBinding 같은 네임스페이스 범위로 바꾸세요."))
    if namespace and destination and namespace != destination:
        found.append(_risk("other_namespace", file, resource, "metadata.namespace", namespace,
                           f"이 앱의 네임스페이스({destination})가 아닌 곳에 만들어져요. 다른 팀의 공간일 수 있고, ArgoCD 프로젝트가 막으면 sync 가 실패해요.",
                           f"metadata.namespace 를 지우거나 {destination} 로 바꾸세요. 그 네임스페이스가 맞다면 그곳을 보는 앱을 고르는 것도 방법이에요."))
    for path, value in _walk(obj):
        field = compare.show(path)
        leaf = path[-1]
        parent = path[-2] if len(path) > 1 else None
        holder = path[-3] if len(path) > 2 else None
        if leaf == "privileged" and value is True and parent == "securityContext":
            found.append(_risk("privileged", file, resource, field, "true",
                               "이 컨테이너가 노드의 거의 모든 권한을 가져요. 뚫리면 노드 전체와 그 위의 다른 파드까지 위험해요.",
                               "privileged 를 빼고, 꼭 필요한 권한만 securityContext.capabilities.add 로 주세요."))
        elif leaf == "hostNetwork" and value is True:
            found.append(_risk("host_network", file, resource, field, "true",
                               "파드가 노드의 네트워크를 그대로 써요. 포트가 겹칠 수 있고 네트워크 격리를 받지 못해요.",
                               "hostNetwork 를 빼고 Service 로 열어 주세요."))
        elif leaf == "hostPath" and isinstance(value, dict) and holder == "volumes":
            found.append(_risk("host_path", file, resource, field, str(value.get("path") or ""),
                               "파드가 노드의 파일을 직접 읽고 써요. 노드의 설정이나 다른 파드의 데이터가 노출될 수 있어요.",
                               "PersistentVolumeClaim·emptyDir·ConfigMap 으로 바꿀 수 있는지 보세요."))
        elif leaf == "image" and isinstance(value, str) and holder in _CONTAINER_LISTS:
            _, tag, digest = split_image(value)
            if not digest and tag in (None, "latest"):
                found.append(_risk("latest_image" if tag else "untagged_image", file, resource, field, value,
                                   "어느 버전이 뜰지 정해지지 않아요. 파드가 다시 뜰 때 다른 버전이 올라올 수 있고, 되돌리기도 어려워요.",
                                   "1.29.0 처럼 버전을 고정한 태그나 digest 를 쓰세요."))
        elif leaf == "value" and holder == "env" and isinstance(parent, str) and _SECRETISH.search(parent) and value:
            # 값은 싣지 않는다 — 비밀 값일 수 있다
            found.append(_risk("plaintext_env", file, resource, field, None,
                               "비밀번호나 키처럼 보이는 환경변수 값이 git 에 평문으로 남아요.",
                               "값이 비밀이면 Secret 을 따로 두고 valueFrom.secretKeyRef 로 가리키세요."))
    return found
