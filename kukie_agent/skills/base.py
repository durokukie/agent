"""스킬 정의와 공통 응답 스키마.

스킬 = 프롬프트 + 툴 이름 목록 + 응답 스키마 (3요소).
툴 실제 구현은 tools/ 카탈로그에 있고, 스킬은 이름으로만 참조한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── 공통 응답 뼈대 (모든 스킬 공유 — UI 일관성) ──────────────────

class FieldExplanation(BaseModel):
    """명령/필드 설명 한 줄 — '배우면서' 가치의 그릇."""
    field: str      # 예: "-n study"
    meaning: str    # 예: "study 네임스페이스를 대상으로 지정"


class ToolStep(BaseModel):
    """화면의 명령 실행 블록 하나.

    네 칸 전부 코드가 채운다 (response.collect_steps) — LLM은 이 블록의 존재를 모른다.
    command/output/access 는 실제 툴 실행 기록에서, explanations 는 FLAG_GLOSSARY 사전에서.
    """
    step_label: str
    access: Literal["read-only", "mutating"]
    command: str
    output: str
    explanations: list[FieldExplanation]


# 원인 분류 — 평가표가 일치로 채점하는 값이다 (DURO-264). kukie-model 시나리오의 `diagnosis:` 도 이 목록을 쓴다.
# 값을 고치거나 더하면 그 시나리오들과 채점기도 같이 고친다. 확실하지 않으면 other, 조사했는데 고장이 없으면 healthy.
CauseCategory = Literal[
    "bad-command", "image-not-found", "oom", "probe-misconfig", "selector-mismatch", "missing-config",
    "unschedulable", "quota", "rbac", "network-policy",
    "volume-permission", "port-conflict", "admission-rejected",   # DURO-268
    "other", "healthy",
]
CAUSE_CATEGORIES: tuple[str, ...] = get_args(CauseCategory)

# 분류마다 뜻과 가까운 분류와의 경계 — 스키마의 cause_category 설명으로 모델에게 그대로 간다 (DURO-268).
# enum 값만 보내면 모델은 이름만 보고 고른다. 일반적인 증상만 적고, 평가 시나리오의 이름·정답 문구는 넣지 않는다.
CAUSE_CATEGORY_GUIDE: dict[str, str] = {
    "bad-command": "컨테이너 command·args·entrypoint 가 틀려 프로세스가 곧바로 끝난다 (CrashLoopBackOff, 로그에 실행 파일 없음·인자 오류). "
                   "명령 자체가 틀린 경우다. 참조한 ConfigMap·Secret 이나 키가 없어 컨테이너가 뜨지 못하면 missing-config",
    "image-not-found": "없는 이미지·태그·레지스트리 주소라 이미지를 받지 못한다 (ErrImagePull, ImagePullBackOff). "
                       "이미지는 받았는데 실행 명령이 틀리면 bad-command",
    "oom": "메모리 limit 을 넘어 컨테이너가 죽는다 (OOMKilled, exit code 137)",
    "probe-misconfig": "liveness·readiness·startup probe 의 포트·경로·시간이 틀려 재시작이 반복되거나 Ready 가 되지 않는다. "
                       "앱은 정상인데 probe 만 실패하는 경우다. 앱이 스스로 죽으면 bad-command",
    "selector-mismatch": "Service selector 가 파드 레이블과 달라 Endpoints 가 비어 있다",
    "missing-config": "파드가 참조하는 ConfigMap·Secret 이나 그 안의 키가 없다 (CreateContainerConfigError, 마운트 실패). "
                      "있는 설정의 값이 틀려 명령이 실패하면 bad-command, 파일은 있는데 쓰기 권한이 없으면 volume-permission",
    "unschedulable": "request 가 너무 크거나 nodeSelector·affinity·taint 에 맞는 노드가 없어 Pending 이다 (FailedScheduling). "
                     "hostPort 가 겹쳐 Pending 이면 port-conflict",
    "quota": "ResourceQuota 가 꽉 찼거나 LimitRange 범위를 벗어나 생성이 거부된다 (exceeded quota, LimitRange 위반). "
             "정책·webhook 이 내용을 보고 거부하면 admission-rejected",
    "rbac": "요청한 사용자·ServiceAccount 에 Role·RoleBinding 권한이 없어 Forbidden 이다 (... cannot <동사> resource ...). "
            "권한은 있는데 정책이 내용을 보고 거부하면 admission-rejected",
    "network-policy": "NetworkPolicy 가 트래픽을 막아 연결이 시간 초과된다. Endpoints 가 비어 있으면 selector-mismatch",
    "volume-permission": "볼륨·파일 권한이 맞지 않아 쓰거나 읽지 못한다 (fsGroup·파일 소유자·runAsUser 불일치, Permission denied). "
                         "볼륨·ConfigMap·Secret 자체가 없으면 missing-config",
    "port-conflict": "같은 파드 안 컨테이너끼리 같은 포트를 쓰거나 hostPort 가 같은 노드에서 겹친다 "
                     "(address already in use, 노드에 빈 포트 없음). probe 가 틀린 포트를 보면 probe-misconfig",
    "admission-rejected": "admission 정책·webhook(Pod Security Admission, ValidatingAdmissionPolicy, Gatekeeper·Kyverno 등)이 "
                          "생성·수정을 거부한다 (admission webhook denied, violates PodSecurity). "
                          "ResourceQuota·LimitRange 초과는 quota, 주체에 권한이 없어 Forbidden 이면 rbac",
    "other": "위 목록에 맞는 것이 없거나 확실하지 않다",
    "healthy": "조사했지만 고장이 없다",
}
if tuple(CAUSE_CATEGORY_GUIDE) != CAUSE_CATEGORIES:   # python -O 에서도 걸리게 assert 대신 쓴다
    raise RuntimeError("CauseCategory 와 CAUSE_CATEGORY_GUIDE 의 분류 목록이 어긋났다")

_CAUSE_CATEGORY_DESCRIPTION = (
    "원인 분류. 아래 목록에서 하나만 고른다. 확실하지 않으면 other, 고장이 없으면 healthy.\n"
    + "\n".join(f"- {name}: {text}" for name, text in CAUSE_CATEGORY_GUIDE.items())
)


class CauseObject(BaseModel):
    """원인이 있는 쿠버네티스 오브젝트 하나. 증상이 보인 오브젝트가 아니라 고쳐야 할 오브젝트다."""
    kind: str = Field(min_length=1, description="오브젝트 종류. 예: Deployment, Service, ConfigMap")
    namespace: str | None = Field(default=None, description="네임스페이스. Node·ClusterRole 같은 클러스터 범위 리소스면 null")
    name: str = Field(min_length=1, description="오브젝트 이름")

    @field_validator("namespace", mode="before")
    @classmethod
    def _blank_namespace_is_none(cls, value: Any) -> Any:
        """클러스터 범위에 null 대신 "" 를 보내도 같은 답이다 — 거부하면 답 전체가 형식 재시도에 걸리고, 그대로 두면 채점에서 null 과 달라진다."""
        return None if isinstance(value, str) and not value.strip() else value


def _all_required(schema: dict[str, Any]) -> None:
    """LLM 에게 보이는 스키마에서는 모든 칸이 필수다 — 새 칸을 늘 채우게 한다.
    파이썬 쪽은 기본값이 있어 새 칸이 없는 예전 응답·기록도 그대로 읽힌다."""
    schema["required"] = list(schema.get("properties", {}))


class Finding(BaseModel):
    """원인 분석 결론 — (a)원인 (b)근거 (c)권장 조치를 구조로 강제.

    cause·evidence·recommendation 은 화면이 그리는 칸이라 이름·모양을 바꾸지 않는다.
    cause_object·cause_category·fix_commands 는 일치 채점용 구조 칸이다 (DURO-264).
    """
    model_config = ConfigDict(json_schema_extra=_all_required)

    cause: str
    evidence: str
    recommendation: str
    cause_object: CauseObject | None = Field(
        default=None, description="원인이 있는(고쳐야 할) 오브젝트. 하나를 고를 수 없으면 null")
    cause_category: CauseCategory = Field(
        default="other", description=_CAUSE_CATEGORY_DESCRIPTION)
    fix_commands: list[str] = Field(
        default_factory=list,
        description="고치는 kubectl 명령. 제안만 하고 실행하지 않는다 — 사용자에게 보여 줄 뿐이다. 고칠 것이 없으면 빈 목록")


class KukieResponse(BaseModel):
    """공통 응답 뼈대. Private·Shared 가 같은 스키마를 쓴다 — 대화마다 출력 스키마가 같아야 캐시가 맞는다 (DURO-100)."""
    narration: str
    steps: list[ToolStep] = []
    suggested_next_action: str | None = None   # 답변·실행 결과에 근거한 다음 행동 안내
    finding: Finding | None = None             # "왜 안 돼요?" 에 원인을 찾았을 때만. 앱이 이 칸을 그린다


# ── 스킬 정의 ────────────────────────────────────────────────

# 공통 베이스 툴 — 읽기 5종 + 매니페스트 레포 읽기 3종(DURO-151). Private·Shared 어느 대화든 항상 포함
# (탈출구 run_readonly_kubectl은 팀 결정으로 제거 — 안내 폴백으로 대체)
# 레포 읽기 툴은 레포가 연결되지 않은 클러스터에서도 노출한다 — 툴 목록이 연결 상태로 바뀌면 프롬프트 캐시가 깨진다 (DURO-135)
COMMON_TOOLS: frozenset[str] = frozenset({
    "list_resources",
    "explain_command",
    "describe_resource",
    "get_events",
    "get_logs",
    "list_repo_dir",
    "read_repo_file",
    "render_app",
})


@dataclass(frozen=True)
class Skill:
    name: str
    prompt: str                          # 스킬 전용 프롬프트 (공통 베이스 위에 얹힘)
    extra_tools: frozenset[str] = field(default_factory=frozenset)
    output_type: type[KukieResponse] = KukieResponse

    @property
    def allowed_tools(self) -> frozenset[str]:
        return COMMON_TOOLS | self.extra_tools

    @property
    def output_fn(self):
        """run 에 꽂을 조립 함수 — output_type 클래스에서 steps 를 뺀 시그니처로 LLM 스키마를 만들고,
        steps 는 코드가 실행 기록에서 채운다 (DURO-44). 호출부: agent.run(..., output_type=skill.output_fn)."""
        from kukie_agent.response import build_response_for   # 순환 import 회피 (response → base)
        return build_response_for(self.output_type)
