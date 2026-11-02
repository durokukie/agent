"""매니페스트 레포를 고칠 때 주고받는 모양 — DURO-133.

들어오는 것: 고칠 리소스(Target) · ArgoCD Application 에서 읽은 것(AppSource) · 요청(Change).
나가는 것: 고친 파일과 렌더 diff(Ready), 또는 왜 못 하는지(Refused).
요청은 나중에 툴 인자로 그대로 쓰므로 값 검사를 여기서 한다 — LLM 이 채운 값이 YAML 에 그대로 들어간다.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Kubernetes quantity (1, 0.5, 100m, 256Mi, 1e3 …) — 이 모양이 아니면 YAML 에 넣지 않는다
_QUANTITY = re.compile(r"^[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+|m|k|Ki|Mi|Gi|Ti|Pi|Ei|M|G|T|P|E)?$")
_RESOURCE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?(/[a-z0-9]([-a-z0-9.]*[a-z0-9])?)?$")
_ENV_NAME = re.compile(r"^[-._a-zA-Z][-._a-zA-Z0-9]*$")
# 컨테이너 이미지 태그 규칙 (OCI distribution spec)
_TAG = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
#: 리소스 kind·이름 모양 — kubectl 인자로도 쓰이므로(`-` 로 시작하면 옵션으로 읽힌다) 이 모양만 받는다
KIND_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]*$")


class Target(BaseModel):
    """고칠 리소스 — 클러스터에서 본 이름 그대로. namespace 가 없으면 이름만으로 찾는다."""
    api_version: str
    kind: str
    name: str
    namespace: str | None = None

    @property
    def group(self) -> str:
        return self.api_version.split("/")[0] if "/" in self.api_version else ""

    def label(self) -> str:
        where = f"{self.namespace}/" if self.namespace else ""
        return f"{self.kind} {where}{self.name}"


class AppSource(BaseModel):
    """ArgoCD Application 에서 읽은 것. 형식은 `status.sourceType` 그대로 받는다 — 추측하지 않는다."""
    source_type: str                     # status.sourceType: Directory · Kustomize · Helm · Plugin …
    path: str                            # spec.source.path — 레포 루트 기준
    directory_recurse: bool = False      # spec.source.directory.recurse (Directory 일 때만)
    directory_filtered: bool = False     # spec.source.directory.include·exclude 가 있나 — 새 파일을 ArgoCD 가 안 읽을 수 있다
    ignore_differences: list[dict] = []  # spec.ignoreDifferences 그대로
    respect_ignore_differences: bool = False  # spec.syncPolicy.syncOptions 에 RespectIgnoreDifferences=true 가 있나


class Replicas(BaseModel):
    what: Literal["replicas"] = "replicas"
    count: int = Field(ge=0, le=1000)


class ImageTag(BaseModel):
    what: Literal["image_tag"] = "image_tag"
    tag: str = Field(pattern=_TAG)
    container: str | None = None         # 컨테이너가 하나면 비워도 된다


class Resources(BaseModel):
    what: Literal["resources"] = "resources"
    container: str | None = None
    requests: dict[str, str] = {}
    limits: dict[str, str] = {}

    @field_validator("requests", "limits")
    @classmethod
    def _quantities(cls, value: dict[str, str]) -> dict[str, str]:
        for key, quantity in value.items():
            if not _RESOURCE_NAME.match(key):
                raise ValueError(f"자원 이름이 아니에요: {key}")
            if not _QUANTITY.match(quantity):
                raise ValueError(f"{key} 값이 Kubernetes 수량 모양이 아니에요: {quantity}")
        return value

    @model_validator(mode="after")
    def _something(self) -> Resources:
        if not self.requests and not self.limits:
            raise ValueError("requests 나 limits 중 하나는 있어야 해요")
        return self


class Env(BaseModel):
    what: Literal["env"] = "env"
    container: str | None = None
    values: dict[str, str] = {}          # 이름 → 값 (없으면 더하고, 있으면 바꾼다)
    remove: list[str] = []

    @field_validator("values", "remove")
    @classmethod
    def _names(cls, value: dict[str, str] | list[str]) -> dict[str, str] | list[str]:
        for name in value:
            if not _ENV_NAME.match(name):
                raise ValueError(f"환경변수 이름이 아니에요: {name}")
        return value

    @model_validator(mode="after")
    def _something(self) -> Env:
        if not self.values and not self.remove:
            raise ValueError("바꿀 환경변수가 없어요")
        if overlap := set(self.values) & set(self.remove):
            raise ValueError(f"같은 이름을 바꾸면서 지울 수는 없어요: {', '.join(sorted(overlap))}")
        return self


class Other(BaseModel):
    """처음 지원하는 넷(레플리카·이미지 태그·resources·환경변수) 밖의 요청."""
    what: Literal["other"] = "other"
    description: str


class NewResource(BaseModel):
    """이미 있는 ArgoCD 앱의 폴더에 새 매니페스트 파일을 더하는 요청 (DURO-154). 기존 리소스는 고치지 않는다."""
    what: Literal["new_resource"] = "new_resource"
    application: str = Field(min_length=1)      # 대상 ArgoCD Application 이름
    application_namespace: str | None = None    # 같은 이름의 앱이 여러 네임스페이스에 있을 때만
    manifest_yaml: str = Field(min_length=1)    # 더할 리소스 전체 (여러 문서 가능)
    file_name: str | None = None                # 앱 폴더 기준 새 파일 이름. 비우면 첫 리소스 이름으로 짓는다


Change = Annotated[Replicas | ImageTag | Resources | Env | Other | NewResource, Field(discriminator="what")]

# 못 하는 이유. retry 가 True 면 고칠 곳을 다시 골라 볼 만하다(툴이 ModelRetry 로 이어 붙인다),
# False 면 PR 로는 못 하는 일이라 kubectl + 경고 길로 간다 (설계 문서 3절).
RefuseCode = Literal[
    "unsupported_source",   # Helm(DURO-137) · Plugin 등 아직 못 고치는 형식
    "unsupported_change",   # 처음 지원하는 넷 밖의 요청, 또는 그 리소스에 없는 칸
    "hpa",                  # HPA 가 레플리카를 정한다 — HPA 최소·최대를 바꿔야 한다
    "no_source",            # git 에 원본이 없다 (오퍼레이터가 만든 것 등)
    "ambiguous",            # 같은 리소스가 여럿, 또는 컨테이너를 골라야 한다
    "secret",               # Secret 값은 PR 에 넣지 않는다
    "ignored_field",        # RespectIgnoreDifferences 로 sync 때 빼는 칸 — git 을 고쳐도 반영되지 않는다
    "no_change",            # 이미 그 값이다
    "invalid_path",         # 레포 밖 경로, 또는 Application path 가 없다
    "outside_app",          # Application path 밖(base 등)을 고쳤다 — 다른 환경도 같이 바뀐다
    "render_failed",        # 렌더링이 안 된다
    "render_mismatch",      # 렌더 비교에서 요청 밖 변화가 보였다
    "busy",                 # 레포 검사 시간이 넘었다 — 잠시 뒤 다시 (kubectl 길·다시 고르기 둘 다 아니다)
    "invalid_manifest",     # 새 리소스 YAML 을 리소스로 읽지 못했다
    "overwrite",            # 새 파일 자리에 이미 파일이 있다
    "resource_exists",      # 새로 만들려는 리소스가 이미 앱에 있다
    "kustomize_config",     # 레포 밖을 끌어올 수 있는 Kustomize 설정을 새로 더하려 했다
]


class Refused(BaseModel):
    ok: Literal[False] = False
    code: RefuseCode
    message: str            # 사람과 AI 가 읽는 이유 (한국어)
    retry: bool = False


class WarningLocation(BaseModel):
    """경고가 가리키는 곳. value 에 비밀 값을 싣지 않는다."""
    file: str               # 레포 루트 기준 파일
    resource: str           # `Deployment shop/caddy`
    field: str | None = None      # `spec.template.spec.containers[app].image`
    value: str | None = None      # 그 칸의 값


class RiskWarning(BaseModel):
    """막지는 않지만 승인 전에 봐야 할 것 (DURO-154) — 어디가, 무슨 일이 날 수 있고, 어떻게 하면 되는지."""
    code: str
    location: WarningLocation
    risk: str
    suggestion: str
    text: str               # 한 줄 문구 — 옛 앱이 gitops.warnings 로 같은 경고를 보여 준다


class Ready(BaseModel):
    ok: Literal[True] = True
    files: dict[str, str]   # 레포 루트 기준 경로 → 고친 뒤 전체 내용 (새 파일 포함, 안 바뀐 파일은 없다)
    file_diff: str          # git diff 모양 — PR·승인 카드에 싣는다
    render_diff: str        # 클러스터에서 실제로 바뀔 것 — 요청한 리소스 하나의 렌더 결과 전·후
    warnings: list[str] = []
    risk_warnings: list[RiskWarning] = []   # 새 리소스 요청에만 (DURO-154)
    new_resources: list[dict[str, str]] = []  # 새 리소스 요청에만 — {api_version, kind, name, namespace, file}


Result = Ready | Refused
