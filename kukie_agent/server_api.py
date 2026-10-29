"""Kukie API 서버(kukie-server) 내부 API 클라이언트 — agent 의 저장·조회는 전부 여기를 거친다 (DURO-113).

agent 는 DB 를 갖지 않는다. 대화·run·Action Plan·클러스터의 원본은 kukie-server 하나이고, agent 는
`/internal/**` 로 읽고 쓴다. 권한 판단("이 사용자가 이 대화·클러스터에 이 일을 해도 되나")도 server 가 한다.

인증은 두 겹이다.
  - 모든 요청에 `X-Kukie-Internal-Token` (KUKIE_INTERNAL_TOKEN). server 와 같은 값이어야 한다
  - **입구** 엔드포인트(대화 컨텍스트, request_id 로 run 찾기, run 시작, 클러스터 연결 정보)만 사용자 Bearer 를 함께 싣는다.
    나머지(run 종료·중단, plan 동작, 클러스터 상태)는 runId·planId 로 대상을 찾으므로 사용자 토큰을 보지 않는다 —
    access token 은 30분이면 만료되는데, run 도중 기록이 401 로 실패해 kubectl 결과를 잃으면 안 된다

fail-closed: KUKIE_SERVER_URL·KUKIE_INTERNAL_TOKEN 중 하나라도 없으면 503 SERVER_NOT_CONFIGURED.
server 에 못 닿으면 503 SERVER_UNAVAILABLE. server 가 준 오류 {code, message} 는 같은 HTTP 상태로 옮긴다
(ServerError) — 앱의 errors.ts 가 코드를 그대로 읽는다. 내부 토큰이 틀린 401 만 예외로 503 이다
(사용자 토큰 문제로 읽혀 앱이 로그아웃하면 안 된다).

JSON 키는 camelCase 다. 단 저장한 그대로 돌려주는 불투명 JSON(responsePayload·agentMessages·usageSummary,
plan 의 payload·decision·executionResult)은 안의 키를 바꾸지 않는다 (snake_case 그대로).

http 클라이언트 하나를 프로세스가 같이 쓴다 — server.py 의 lifespan 이 start()/close() 를 부른다.
테스트는 이 모듈의 함수들을 메모리 가짜(tests/fake_server.py)로 바꿔 끼운다. 그래서 부르는 쪽은
`from kukie_agent.server_api import x` 가 아니라 `server_api.x(...)` 로 부른다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from kukie_agent.auth import User, server_url

logger = logging.getLogger(__name__)

TOKEN_ENV = "KUKIE_INTERNAL_TOKEN"
TOKEN_HEADER = "X-Kukie-Internal-Token"
TIMEOUT = 10.0

# run 상태 — server 의 wire 값 그대로. 앞의 셋은 활성(방마다 최대 하나), 뒤의 셋은 종료.
RUN_ACTIVE = ("running", "awaiting_approval", "recovery_required")
RUN_FINAL = ("completed", "failed", "interrupted")

_client: httpx.AsyncClient | None = None


# ── 오류 ───────────────────────────────────────────────────

class ServerError(HTTPException):
    """kukie-server 가 거절했거나 닿지 못했다. detail 은 {code, message} — 그대로 클라이언트에 나가도 되는 모양이다."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code, {"code": code, "message": message})

    @property
    def code(self) -> str:
        return self.detail["code"]

    @property
    def message(self) -> str:
        return self.detail["message"]


def _token() -> str | None:
    return os.environ.get(TOKEN_ENV, "").strip() or None


def configured() -> bool:
    """내부 API 를 부를 수 있게 설정됐나. 개발 모드(server 없음)면 False."""
    return server_url() is not None and _token() is not None


def _config() -> tuple[str, str]:
    url, token = server_url(), _token()
    if url is None or token is None:
        raise ServerError(503, "SERVER_NOT_CONFIGURED",
                          "Kukie API 서버 설정(KUKIE_SERVER_URL, KUKIE_INTERNAL_TOKEN)이 없어 대화를 저장할 수 없다")
    return url, token


def _path(*segments: str) -> str:
    """`/internal/...` 경로를 조각마다 이스케이프해 잇는다.

    request_id 는 클라이언트가 고른 아무 문자열이고, 대화·클러스터 id 도 요청 경로에서 온다. 그대로 끼우면
    `?`·`#`·`/` 가 쿼리나 다른 경로를 만들고, `..` 는 httpx 가 정규화해 다른 내부 엔드포인트로 간다.
    `.`·`..` 인 id 는 있을 수 없으니 부르지 않고 404 로 끝낸다.
    """
    escaped = []
    for segment in segments:
        text = str(segment)
        if text in ("", ".", ".."):
            raise ServerError(404, "NOT_FOUND", f"없다: {text!r}")
        escaped.append(quote(text, safe=""))
    return "/internal/" + "/".join(escaped)


# ── 연결 ───────────────────────────────────────────────────

def start() -> None:
    """lifespan 에서 부른다. 연결 풀을 프로세스가 같이 쓴다."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _http() -> httpx.AsyncClient:
    # lifespan 을 거치지 않은 경우(TestClient 를 with 없이 쓴 테스트 등)에도 동작하게 처음 쓸 때 만든다
    if _client is None:
        start()
    assert _client is not None
    return _client


async def _call(
    method: str, path: str, *, user: User | None = None, params: dict[str, Any] | None = None,
    body: Any = None, not_found_is_none: bool = False,
) -> Any:
    """요청 하나. user 를 주면 입구 엔드포인트다 — 사용자 Bearer 를 함께 싣는다."""
    url, token = _config()
    headers = {TOKEN_HEADER: token}
    if user is not None:
        if not user.token:
            raise ServerError(401, "UNAUTHORIZED", "사용자 토큰이 없어 Kukie API 서버에 물을 수 없다")
        headers["Authorization"] = f"Bearer {user.token}"
    try:
        response = await _http().request(
            method, f"{url}{path}", headers=headers,
            params={k: v for k, v in (params or {}).items() if v is not None} or None,
            json=body,
        )
    except httpx.HTTPError as exc:
        # 예외 문자열에 URL 이 실릴 수 있지만 토큰은 헤더라 실리지 않는다
        raise ServerError(503, "SERVER_UNAVAILABLE", f"Kukie API 서버에 연결할 수 없다: {type(exc).__name__}") from exc
    if response.status_code == 404 and not_found_is_none:
        return None
    if response.is_success:
        return response.json() if response.content else None
    raise _error_of(response)


def _error_of(response: httpx.Response) -> ServerError:
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    message = body.get("message") if isinstance(body, dict) else None
    if code == "INVALID_INTERNAL_TOKEN":
        logger.error("Kukie API 서버가 내부 토큰을 거절했다 — KUKIE_INTERNAL_TOKEN 이 server 와 다르다")
        return ServerError(503, "SERVER_UNAVAILABLE", "Kukie API 서버와의 내부 인증 설정이 맞지 않는다")
    if isinstance(code, str) and code:
        return ServerError(response.status_code, code, str(message or ""))
    if response.status_code >= 500:
        return ServerError(503, "SERVER_UNAVAILABLE", f"Kukie API 서버 응답 {response.status_code}")
    return ServerError(response.status_code, "SERVER_ERROR", f"Kukie API 서버 응답 {response.status_code}")


# ── 결과 객체 ───────────────────────────────────────────────

@dataclass(frozen=True)
class Run:
    """RunResponse. response_payload 는 저장된 그대로 — 실패면 {"kind": "error", ...} 모양이다."""

    id: str
    conversation_id: str
    request_id: str
    seq: int
    kind: str
    mode: str
    status: str
    input_text: str | None
    response_payload: dict[str, Any] | None
    agent_messages: list[Any] | None
    usage_summary: dict[str, Any] | None
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Run":
        return cls(
            id=str(data["id"]), conversation_id=str(data.get("conversationId", "")),
            request_id=data["requestId"], seq=int(data.get("seq", 0)), kind=data["kind"], mode=data["mode"],
            status=data["status"], input_text=data.get("inputText"), response_payload=data.get("responsePayload"),
            agent_messages=data.get("agentMessages"), usage_summary=data.get("usageSummary"),
            started_at=data.get("startedAt"), finished_at=data.get("finishedAt"),
        )

    @property
    def active(self) -> bool:
        return self.status in RUN_ACTIVE

    @property
    def error(self) -> dict[str, Any] | None:
        """실패로 남은 run 이면 그 내용 (kind 를 뺀 나머지). 화면 payload 와 갈라 읽는다."""
        payload = self.response_payload
        if payload is not None and payload.get("kind") == "error":
            return {k: v for k, v in payload.items() if k != "kind"}
        return None

    @property
    def payload(self) -> dict[str, Any] | None:
        return None if self.error is not None else self.response_payload


@dataclass(frozen=True)
class Plan:
    """PlanResponse. 상태 전이는 server 가 정하고, agent 는 이 값으로 메모리의 ActionPlan 을 맞춘다."""

    id: str
    code: str
    run_id: str
    tool_call_id: str
    tool_name: str
    status: str
    risk: str
    payload: dict[str, Any]
    request_hash: str | None
    decision: dict[str, Any] | None
    execution_result: dict[str, Any] | None
    failure_reason: str | None
    applied_at: str | None
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Plan":
        return cls(
            id=str(data["id"]), code=data.get("code") or str(data["id"]), run_id=str(data["runId"]),
            tool_call_id=data["toolCallId"], tool_name=data["toolName"], status=data["status"], risk=data["risk"],
            payload=dict(data.get("payload") or {}), request_hash=data.get("requestHash"),
            decision=data.get("decision"), execution_result=data.get("executionResult"),
            failure_reason=data.get("failureReason"), applied_at=data.get("appliedAt"),
            created_at=data.get("createdAt"), updated_at=data.get("updatedAt"),
        )


@dataclass(frozen=True)
class ConversationInfo:
    """대화 컨텍스트의 conversation. context·namespace·지문은 대화를 만들 때 클러스터에서 복사한 값이다."""

    id: str
    cluster_id: str
    team_id: str | None
    user_id: str
    title: str
    shared: bool
    context: str
    namespace: str
    cluster_fingerprint: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ConversationInfo":
        return cls(
            id=str(data["id"]), cluster_id=str(data["clusterId"]), team_id=data.get("teamId"),
            user_id=str(data["userId"]), title=data.get("title", ""), shared=bool(data.get("shared")),
            context=data["context"], namespace=data["namespace"],
            cluster_fingerprint=data.get("clusterFingerprint"),
        )


@dataclass(frozen=True)
class HistoryEntry:
    run_id: str
    agent_messages: list[Any]


@dataclass(frozen=True)
class ConversationContext:
    """GET /internal/conversations/{id}/context — 방 하나를 열 때 필요한 것 전부를 한 번에.

    history 는 완료된 run 중 agent_messages 가 있는 것 (server 가 최근 100개, seq 오름차순으로 준다).
    """

    user_id: str
    conversation: ConversationInfo
    active_run: Run | None
    active_run_plans: list[Plan] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ConversationContext":
        active = data.get("activeRun")
        return cls(
            user_id=str(data.get("userId", "")),
            conversation=ConversationInfo.from_json(data["conversation"]),
            active_run=Run.from_json(active) if active else None,
            active_run_plans=[Plan.from_json(p) for p in data.get("activeRunPlans") or []],
            history=[HistoryEntry(run_id=str(h.get("runId", "")), agent_messages=list(h.get("agentMessages") or []))
                     for h in data.get("history") or []],
        )

    def without_active_run(self) -> "ConversationContext":
        """활성 run 을 방금 닫았을 때 — 같은 요청 안에서 낡은 값을 다시 보지 않게."""
        return replace(self, active_run=None, active_run_plans=[])


@dataclass(frozen=True)
class RunUpdate:
    """PATCH /internal/runs/{id} — 종료 상태로 닫았으면 같은 트랜잭션에서 닫힌 plan 도 같이 온다."""

    run: Run
    closed_plans: list[Plan]


@dataclass(frozen=True)
class Interrupted:
    runs: list[Run]
    closed_plans: list[Plan]


@dataclass(frozen=True)
class ClusterConnection:
    """평문 자격증명이 든다 — 로그·LLM 입력·응답에 싣지 않는다. repr 에서도 뺀다."""

    cluster_id: str
    name: str
    api_server: str
    ca_data: str | None
    insecure: bool
    context_name: str
    namespace: str
    fingerprint: str
    credential: dict[str, str] = field(repr=False)
    #: server 에 남은 GPU 노드 유무. None 은 아직 모른다 — 개요·연결 확인이 알아내면 update_cluster_gpu 로 남긴다
    has_gpu: bool | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ClusterConnection":
        return cls(
            cluster_id=str(data["clusterId"]), name=data.get("name", ""), api_server=data["apiServer"],
            ca_data=data.get("caData"), insecure=bool(data.get("insecure")), context_name=data["contextName"],
            namespace=data["namespace"], fingerprint=data.get("fingerprint", ""),
            credential={str(k): str(v) for k, v in (data.get("credential") or {}).items()},
            has_gpu=data["hasGpu"] if isinstance(data.get("hasGpu"), bool) else None,
        )


# ── 대화 · run ─────────────────────────────────────────────

async def conversation_context(conversation_id: str, user: User, *, purpose: str) -> ConversationContext:
    """입구. purpose=chat 은 대화 읽기 권한, approve 는 승인·재개 권한까지 server 가 본다 (404 / 403)."""
    data = await _call("GET", _path("conversations", conversation_id, "context"),
                       user=user, params={"purpose": purpose})
    return ConversationContext.from_json(data)


async def find_run(conversation_id: str, request_id: str, user: User) -> Run | None:
    """입구. 같은 request_id 로 이미 만든 run — 없으면 None."""
    data = await _call("GET", _path("conversations", conversation_id, "runs", "by-request", request_id),
                       user=user, not_found_is_none=True)
    return Run.from_json(data) if data is not None else None


async def start_run(
    conversation_id: str, user: User, *, request_id: str, kind: str, mode: str, input_text: str | None,
) -> tuple[Run, bool]:
    """입구. (run, 새로 만들었나). 같은 request_id·같은 입력이면 기존 run 을 준다.

    409 REQUEST_MISMATCH(같은 id 에 다른 입력), 409 ACTIVE_RUN_EXISTS(방에 활성 run 이 있음) 는 ServerError 로.
    """
    data = await _call("POST", _path("conversations", conversation_id, "runs"), user=user,
                       body={"requestId": request_id, "kind": kind, "mode": mode, "inputText": input_text})
    return Run.from_json(data["run"]), bool(data.get("created"))


async def update_run(
    run_id: str, *, status: str | None = None, response_payload: dict[str, Any] | None = None,
    append_agent_messages: list[Any] | None = None, usage_summary: dict[str, Any] | None = None,
    title_from: str | None = None,
) -> RunUpdate:
    """run 갱신 한 번. 준 칸만 바꾼다 (None = 그대로).

    server 가 한 트랜잭션에서 한다: 종료 상태면 finishedAt + 열린 plan 마감(결정 있고 결과 없음 → UNKNOWN,
    나머지 → EXPIRED), title_from 이면 첫 마디로 제목 짓기 (기본 제목일 때만).
    agent_messages 는 기존 배열 뒤에 **이어 붙인다** — 승인·재개가 같은 run 을 이어 쓰기 때문이다.
    """
    body: dict[str, Any] = {}
    for key, value in (
        ("status", status), ("responsePayload", response_payload), ("appendAgentMessages", append_agent_messages),
        ("usageSummary", usage_summary), ("titleFrom", title_from),
    ):
        if value is not None:
            body[key] = value
    data = await _call("PATCH", _path("runs", run_id), body=body)
    return RunUpdate(Run.from_json(data["run"]), [Plan.from_json(p) for p in data.get("closedPlans") or []])


async def interrupt(conversation_id: str, message: str) -> Interrupted:
    """방의 활성 run 을 interrupted 로 닫는다. 실패 기록(error payload)이 이미 있으면 그 코드는 남긴다."""
    data = await _call("POST", _path("conversations", conversation_id, "interrupt"), body={"message": message})
    return Interrupted([Run.from_json(r) for r in data.get("runs") or []],
                       [Plan.from_json(p) for p in data.get("closedPlans") or []])


async def interrupt_orphaned(message: str) -> int:
    """기동할 때 — 전 프로세스가 돌리다 죽은 `running` run 을 전부 닫는다. 승인 대기는 그대로 둔다."""
    data = await _call("POST", _path("runs", "interrupt-orphaned"), body={"message": message})
    return int((data or {}).get("interrupted", 0))


# ── 클러스터 ───────────────────────────────────────────────

async def cluster_connection(
    cluster_id: str, user: User, *, expected_fingerprint: str | None = None,
) -> ClusterConnection:
    """입구. 이 사용자가 읽을 수 있는 클러스터면 평문 자격증명까지 준다 (못 보면 404).

    expected_fingerprint 를 주면 server 가 지금 지문과 대조해 다르면 409 CLUSTER_CHANGED — "승인한 대상 = 실행 대상".
    """
    data = await _call("GET", _path("clusters", cluster_id, "connection"), user=user,
                       params={"expectedFingerprint": expected_fingerprint})
    return ClusterConnection.from_json(data)


async def update_cluster_status(cluster_id: str, *, status: str, checked_at: datetime) -> dict[str, Any]:
    """연결 확인 결과. 시각은 server 응답과 같은 오프셋 없는 UTC ISO 로 보낸다."""
    at = checked_at.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    return await _call("PATCH", _path("clusters", cluster_id, "status"), body={"status": status, "checkedAt": at})


async def update_cluster_gpu(cluster_id: str, *, has_gpu: bool, fingerprint: str) -> dict[str, Any]:
    """노드 목록으로 알아낸 GPU 노드 유무 (DURO-118). 앱은 클러스터 목록의 이 값으로 GPU 아이콘을 띄운다.

    fingerprint 는 노드를 읽을 때 쓴 연결 정보의 지문이다 — 그 사이 kubeconfig 가 바뀌었으면 server 가 409 CLUSTER_CHANGED.
    """
    return await _call("PATCH", _path("clusters", cluster_id, "gpu"),
                       body={"hasGpu": has_gpu, "fingerprint": fingerprint})


# ── Action Plan ────────────────────────────────────────────

async def create_plan(
    run_id: str, *, tool_call_id: str, tool_name: str, risk: str, request_hash: str, payload: dict[str, Any],
) -> Plan:
    """DRAFT 로 만든다. id(UUID)와 표시용 code 는 server 가 정한다. 같은 toolCallId 면 기존 plan 을 준다."""
    data = await _call("POST", _path("runs", run_id, "plans"), body={
        "toolCallId": tool_call_id, "toolName": tool_name, "risk": risk,
        "requestHash": request_hash, "payload": payload,
    })
    return Plan.from_json(data)


async def run_plans(run_id: str, *, tool_call_id: str | None = None) -> list[Plan]:
    """run 의 plan 들 (생성 순). tool_call_id 를 주면 0~1개."""
    data = await _call("GET", _path("runs", run_id, "plans"), params={"toolCallId": tool_call_id})
    return [Plan.from_json(p) for p in data or []]


async def get_plan(plan_id: str) -> Plan:
    return Plan.from_json(await _call("GET", _path("plans", plan_id)))


#: plan 동작 이름 — `POST /internal/plans/{id}/<action>`. 허용 안 되는 상태면 409 PLAN_STATE_CONFLICT,
#: 승인한 호출과 실행하려는 호출이 다르면 409 PLAN_CALL_MISMATCH.
#: stale 은 승인 순간 전제가 깨졌다는 알림이다 (기획 08, GitOps 의 PR 길이 바뀜 — DURO-134). 결정 전 계획만 받는다
PLAN_ACTIONS = frozenset({"dry-run", "offer", "decision", "start", "result", "fail", "unknown", "stale"})


async def plan_action(plan_id: str, action: str, body: dict[str, Any] | None = None) -> Plan:
    """무슨 일이 있었는지를 알린다. 다음 상태는 server 가 정하고, 갱신된 plan 을 돌려준다."""
    if action not in PLAN_ACTIONS:
        raise ValueError(f"unknown plan action: {action}")
    return Plan.from_json(await _call("POST", _path("plans", plan_id, action), body=body or {}))


# ── GitOps 매니페스트 레포 (DURO-131) ─────────────────────────

@dataclass(frozen=True)
class ManifestRepo:
    """클러스터에 연결된 매니페스트 레포 하나 (ManifestRepoResponse). repository 는 소문자 `owner/repo`."""

    id: str
    cluster_id: str
    repository: str
    repository_id: int
    installation_id: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ManifestRepo":
        return cls(
            id=str(data["id"]), cluster_id=str(data["clusterId"]), repository=str(data["repository"]).lower(),
            repository_id=int(data.get("repositoryId") or 0), installation_id=int(data.get("installationId") or 0),
        )


@dataclass(frozen=True)
class RepoToken:
    """그 레포 하나로 범위를 좁힌 1시간 GitHub App 설치 토큰. 로그·LLM 입력·응답에 싣지 않는다 — repr 에서도 뺀다."""

    repository: str
    repository_id: int
    expires_at: str
    token: str = field(repr=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RepoToken":
        return cls(
            repository=str(data["repository"]).lower(), repository_id=int(data.get("repositoryId") or 0),
            expires_at=str(data.get("expiresAt") or ""), token=str(data["token"]),
        )


async def manifest_repos(cluster_id: str) -> list[ManifestRepo]:
    """클러스터에 지금 연결된 레포들. PR 길 판단(gitops/route.py)이 ArgoCD Application 의 repoURL 과 맞춰 본다."""
    data = await _call("GET", _path("clusters", cluster_id, "manifest-repos"))
    return [ManifestRepo.from_json(item) for item in data or []]


async def repo_token(cluster_id: str, repository: str) -> RepoToken:
    """연결된 레포의 설치 토큰. 쓸 때마다 받는다 (30분보다 남았으면 server 가 같은 토큰을 다시 준다).

    repository 는 `owner/repo` 든 ArgoCD repoURL 이든 된다. 연결 안 됐거나 끊긴 레포면 404 MANIFEST_REPO_NOT_CONNECTED.
    """
    data = await _call("POST", _path("clusters", cluster_id, "manifest-repos", "token"), body={"repository": repository})
    return RepoToken.from_json(data)


#: 테스트의 메모리 가짜가 같은 이름으로 바꿔 끼우는 함수들 (tests/fake_server.py).
API = (
    "conversation_context", "find_run", "start_run", "update_run", "interrupt", "interrupt_orphaned",
    "cluster_connection", "update_cluster_status", "update_cluster_gpu", "create_plan", "run_plans", "get_plan", "plan_action",
    "manifest_repos", "repo_token",
)
