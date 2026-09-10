"""SQLAlchemy 모델 — Linear "Kukie 채팅 DB 구조" 문서의 tbl_chat_session / tbl_chat_run 을 그대로 옮긴다.

문서와 다른 점은 셋뿐이고 issue #53 에 적었다 (product-spec: 팀 → 클러스터 → 대화):
  tbl_chat_session.team_id, cluster_id, shared  — 없으면 "팀 공유 대화" 를 고를 수 없다.

tbl_user 는 Spring 회원 서버 것이라 여기 없다 (user_id 는 문자열로만 들고 소유권만 검사한다).
tbl_action_plan 은 이 표가 원본이다 (#58). .md 파일은 사람이 읽는 사본으로만 남는다.
plan 상태 이름은 DB 문서 5절의 소문자가 아니라 DURO-83 결정(기획 10, 대문자)을 따른다 — 대응표는 issue #58 댓글.
실패 정보는 별도 컬럼이 아니라 response_payload 에 {"kind": "error", ...} 로 남긴다 (문서에 error 컬럼이 없다).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# run 상태 — DB 문서 4절. 앞의 셋은 활성(finished_at 없음), 뒤의 셋은 종료(finished_at 있음).
# "채팅방 하나에 활성 run 은 최대 하나" — 활성 run 이 있으면 새 요청·모드 변경을 막는다.
RUN_ACTIVE = ("running", "awaiting_approval", "recovery_required")
RUN_FINAL = ("completed", "failed", "interrupted")
RUN_STATUSES = RUN_ACTIVE + RUN_FINAL

# 문서 4절: 일반 요청 chat, 모드 변경 mode_change. 승인·재개는 새 run 이 아니라 기존 run 을 이어서 끝낸다 (7절).
RUN_KINDS = ("chat", "mode_change")

MODES = ("학습", "진단", "실습")

#: 방을 만들 때의 기본 제목. 첫 마디가 오면 이 값일 때만 바꾼다 (#59).
DEFAULT_TITLE = "새 대화"

# 클러스터 (기획 04 §8). MVP 는 Generic Kubernetes 만 — EKS/GKE/AKS 의 Cloud Identity 는 후속(§6).
CLUSTER_PROVIDERS = ("GENERIC",)
# 마지막 Connection Test 결과. auth_expired 는 붙었지만 권한이 없거나 만료된 상태.
CLUSTER_STATUSES = ("connected", "disconnected", "auth_expired")

# plan 상태 — DURO-83 결정 (기획 10 이름 + REJECTED). DB 문서 5절의 소문자 목록을 대체한다 (issue #58 댓글에 대응표).
# 정상 흐름: DRAFT → WAITING_APPROVAL → APPROVED → EXECUTING → APPLIED → EFFECT_VERIFIED
PLAN_OPEN = ("DRAFT", "WAITING_APPROVAL", "APPROVED", "EXECUTING")   # 아직 끝나지 않은 계획
PLAN_CLOSED = ("APPLIED", "EFFECT_VERIFIED", "REJECTED", "EXPIRED", "STALE", "FAILED", "UNKNOWN")
PLAN_STATUSES = PLAN_OPEN + PLAN_CLOSED

# APPLIED = kubectl 이 성공했다. EFFECT_VERIFIED = 그 효과까지 확인했다 (기획 09, 아직 아무도 안 채운다).
# UNKNOWN = 실행 여부를 모른다 (run 의 recovery_required 짝).
#
# EXPIRED 와 STALE 은 **다른 상황**이다 (DURO-83 의 "어긋나는 지점" 이 둘을 갈라 적었다).
#   EXPIRED = 중단·재시작으로 **승인 카드가 못 쓰게 됐다.** 클러스터는 그대로다
#   STALE   = **클러스터 상태가 바뀌어 전제가 깨졌다** (기획 08). 아직 판정하는 코드가 없다
# 사용자 안내가 갈린다 — "카드가 만료됐으니 다시 요청하세요" vs "상황이 바뀌었으니 다시 계획합니다".
# 예전에는 앞엣것에 STALE 을 썼는데, DURO-83 이 그 이름을 08 용으로 정해 둔 것을 어긴 것이었다.
PLAN_FAILURE_REASONS = (
    "PERMISSION_DENIED",           # RBAC 거부
    "CONCURRENT_MODIFICATION",     # 다른 사람이 먼저 바꿨다 (기획 08, 아직 판정하지 않는다)
    "DRY_RUN_FAILED",              # 승인 전 예행에서 떨어졌다
    "EXECUTION_FAILED",            # kubectl 이 실패했다
    "VERIFICATION_FAILED",         # 적용은 됐지만 효과 확인이 실패했다 (기획 09)
    "VERIFICATION_TIMEOUT",
)

PLAN_RISKS = ("caution", "destructive")



class ChatSession(Base):
    """채팅방 하나. session 은 로그인 세션이 아니라 대화방이다 (문서 3절)."""

    __tablename__ = "tbl_chat_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)                 # tbl_user.id (Spring)
    title: Mapped[str] = mapped_column(String(200), default=DEFAULT_TITLE)
    current_mode: Mapped[str] = mapped_column(String(20))                        # MODES — 다음 요청에 적용
    installation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)   # kubectl 을 실행하는 agent 설치 환경
    context_name: Mapped[str] = mapped_column(String(200))                       # kubeconfig context
    namespace: Mapped[str] = mapped_column(String(63))
    cluster_fingerprint: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 실제 대상 클러스터 확인값
    version: Mapped[int] = mapped_column(Integer, default=1)                     # 오래된 화면의 변경 요청 감지
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # ── 문서에 없는 셋 (issue #53) ──
    team_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)

    runs: Mapped[list["ChatRun"]] = relationship(back_populates="session", order_by="ChatRun.turn_no")


class ChatRun(Base):
    """사용자 요청 한 건 (문서 4절). LLM 호출 한 번이 아니라 요청 하나 처리 전체."""

    __tablename__ = "tbl_chat_run"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id", name="uq_run_request"),
        UniqueConstraint("session_id", "turn_no", name="uq_run_turn"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tbl_chat_session.id", ondelete="RESTRICT"), index=True,
    )
    request_id: Mapped[str] = mapped_column(String(64))                          # 재전송 식별
    turn_no: Mapped[int] = mapped_column(Integer)                                # 방 안의 순서, 1부터
    kind: Mapped[str] = mapped_column(String(20))                                # RUN_KINDS
    mode: Mapped[str] = mapped_column(String(20))                                # 이 run 에 적용된 모드 (과거 값 유지)
    status: Mapped[str] = mapped_column(String(20))                              # RUN_STATUSES
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 화면에 그릴 ChatPayload. 실패면 kind=error
    agent_messages: Mapped[list | None] = mapped_column(JSON, nullable=True)     # 이 run 의 ModelMessage 들
    history_format_version: Mapped[int] = mapped_column(Integer, default=1)
    usage_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)      # 토큰 사용량. 모르면 null
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[ChatSession] = relationship(back_populates="runs")
    plans: Mapped[list["ActionPlan"]] = relationship(back_populates="run", order_by="ActionPlan.created_at")


class ActionPlan(Base):
    """Kubernetes 변경 1건의 계획·결정·실행 결과 (DB 문서 5절).

    읽기 도구 호출은 여기 오지 않는다 — 변경 툴 4종만. plan 은 반드시 run 에 속하므로
    run 이 없는 flat 엔드포인트(/chat, /approve)의 계획은 이 표에 들어오지 않고 .md 로만 남는다.
    소유자는 run_id → session_id → user_id 로 거슬러 찾는다 (문서 6절, 회원 id 를 여기 중복 저장하지 않는다).
    """

    __tablename__ = "tbl_action_plan"
    __table_args__ = (
        # 문서 5절 "tool_call_id 는 실행 안에서 중복 불가"
        UniqueConstraint("run_id", "tool_call_id", name="uq_plan_tool_call"),
        # 문서 5절: 승인·실행 상태에는 그에 맞는 결정·결과가 있어야 한다. 저장된 값의 앞뒤가 맞는지만 보는 검사이고,
        # 클러스터의 현재 상태를 증명하지는 않는다.
        CheckConstraint(
            "status NOT IN ('APPROVED', 'EXECUTING', 'APPLIED', 'EFFECT_VERIFIED') OR decision IS NOT NULL",
            name="ck_plan_decision_present",
        ),
        CheckConstraint(
            "status NOT IN ('APPLIED', 'EFFECT_VERIFIED') OR (execution_result IS NOT NULL AND applied_at IS NOT NULL)",
            name="ck_plan_applied_has_result",
        ),
        CheckConstraint("status <> 'FAILED' OR failure_reason IS NOT NULL", name="ck_plan_failed_has_reason"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)                 # ap-260910-1530-scale-resource (.md 파일 이름과 같다)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tbl_chat_run.id", ondelete="RESTRICT"), index=True,
    )
    tool_call_id: Mapped[str] = mapped_column(String(64), index=True)             # 모델의 도구 호출 id
    tool_name: Mapped[str] = mapped_column(String(50))                            # MUTATING_TOOLS
    status: Mapped[str] = mapped_column(String(30))                               # PLAN_STATUSES
    risk: Mapped[str] = mapped_column(String(20))                                 # PLAN_RISKS
    plan_payload: Mapped[dict] = mapped_column(JSON)                              # 명령·대상·인자·의도·예상 효과·부작용·예행·판단 가이드
    request_hash: Mapped[str] = mapped_column(String(64))                         # 승인한 내용과 실행 요청이 같은지 확인
    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)            # {approved, user_id, at}. 결정 전 null
    execution_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)    # {success, exit_code, stdout, stderr, at}
    failure_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)  # PLAN_FAILURE_REASONS
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ↑ 검증이 실패해도(FAILED + VERIFICATION_FAILED) "적용은 됐다" 는 사실이 남게 별도 칸 (DURO-83, 강효승 요청)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    run: Mapped[ChatRun] = relationship(back_populates="plans")


class Cluster(Base):
    """등록된 클러스터 하나 (기획 04 §8).

    지금까지 agent 는 자기가 도는 컴퓨터의 kubeconfig 를 읽었다. 서버가 사용자 노트북 밖으로 나가면
    그 파일이 없으므로 접속 정보를 여기 보관한다. 자격증명은 평문으로 두지 않는다 —
    credential_encrypted 에 서버 키로 암호화해 넣고, 실행 직전에만 풀어 임시 파일로 쓴다.

    팀·권한은 Spring 이 원본이라 team_id 는 문자열로만 들고 FK 를 걸지 않는다 (tbl_user 와 같은 이유).
    """

    __tablename__ = "tbl_cluster"
    __table_args__ = (
        # 이름 유일성은 조건에 따라 기준이 다르다. 하나의 UNIQUE 로는 못 한다 — SQL 은 NULL 을 서로
        # 다른 값으로 보므로 (team_id, registered_by, name) 은 team_id 가 NULL 인 개인 클러스터를
        # 전혀 막지 못하고, 팀 클러스터에서는 registered_by 가 섞여 같은 팀에 같은 이름이 둘 생긴다
        # (자동 리뷰 지적). 조건부 인덱스 둘로 나눈다.
        # 조건은 읽는 쪽(_visible_sessions·list_clusters)과 같아야 한다 — `''` 를 "팀 없음" 으로
        # 보면서 인덱스만 IS NULL 로 두면, 정규화 전에 들어간 `''` 행이 개인 이름 유일성 밖에
        # 남아 한 사람의 목록에 같은 이름이 둘 뜬다 (자동 리뷰 지적).
        Index("uq_cluster_team_name", "team_id", "name",
              unique=True, sqlite_where=text("team_id IS NOT NULL AND team_id <> ''"),
              postgresql_where=text("team_id IS NOT NULL AND team_id <> ''")),
        Index("uq_cluster_personal_name", "registered_by", "name",
              unique=True, sqlite_where=text("team_id IS NULL OR team_id = ''"),
              postgresql_where=text("team_id IS NULL OR team_id = ''")),
        CheckConstraint("api_server LIKE 'https://%'", name="ck_cluster_https"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)   # Spring tbl_team.id. 팀 API 붙기 전 null
    registered_by: Mapped[str] = mapped_column(String(64), index=True)                    # 등록한 회원 id
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(20), default="GENERIC")                  # CLUSTER_PROVIDERS
    api_server: Mapped[str] = mapped_column(String(300))
    ca_data: Mapped[str | None] = mapped_column(Text, nullable=True)                      # base64. insecure 면 없다
    insecure: Mapped[bool] = mapped_column(Boolean, default=False)                        # TLS 검증 건너뜀 (문서에 없는 칸 — issue #61)
    credential_encrypted: Mapped[str] = mapped_column(Text)                               # 토큰 또는 client cert/key
    context_name: Mapped[str] = mapped_column(String(200))
    default_namespace: Mapped[str] = mapped_column(String(63), default="default")
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)                      # api_server + CA 해시
    status: Mapped[str] = mapped_column(String(20), default="disconnected")               # CLUSTER_STATUSES
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
