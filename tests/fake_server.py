"""메모리 가짜 Kukie API 서버 — kukie_agent.server_api 와 같은 함수들을 같은 모양으로 갖는다 (DURO-113).

agent 는 DB 가 없고 저장·권한 판단을 kukie-server 내부 API 에 맡긴다. agent 로직 테스트(재전송, 승인 뒤 재개,
exactly-once, UNKNOWN, 재시작 복원, 권한 거절)가 server 없이 돌도록, conftest 의 fake_server 픽스처가
server_api 의 함수들을 이 객체의 메서드로 바꿔 끼운다.

계약(kukie-server 와 맞춘 scratchpad contract.md 3절)의 규칙 중 agent 가 기대는 것만 옮겼다.
  - run: 같은 requestId 재전송·REQUEST_MISMATCH·ACTIVE_RUN_EXISTS, 종료 시 열린 plan 마감, 첫 마디 제목
  - plan: 상태 머신 (dry-run·offer·decision·start·result·fail·unknown), 허용 안 되면 409 PLAN_STATE_CONFLICT,
    승인한 호출과 다르면 409 PLAN_CALL_MISMATCH, 같은 toolCallId 생성은 기존 plan
  - 권한: 대화 읽기·승인, 클러스터 읽기 (contract 2절 권한 규칙)
응답은 camelCase JSON 을 만들어 server_api 의 from_json 으로 바꿔 준다 — 진짜 server 의 응답을 읽는 길도 같이 탄다.
server 자체의 정확성(트랜잭션·동시성·DB 제약)은 kukie-server 테스트 몫이다.
"""
from __future__ import annotations

import copy
import itertools
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from kukie_agent import server_api
from kukie_agent.auth import User
from kukie_agent.server_api import ServerError

DEFAULT_TITLE = "새 대화"
PLAN_OPEN = ("DRAFT", "WAITING_APPROVAL", "APPROVED", "EXECUTING")


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def _at() -> str:
    """불투명 JSON 안의 시각 (contract 0절: Z 붙은 문자열)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _not_found(what: str) -> ServerError:
    return ServerError(404, "NOT_FOUND", f"없다: {what}")


def _conflict(code: str = "PLAN_STATE_CONFLICT", message: str = "지금 상태에서는 할 수 없다") -> ServerError:
    return ServerError(409, code, message)


def _titleable(text: str | None) -> bool:
    trimmed = " ".join((text or "").split())
    return bool(trimmed) and not trimmed.startswith("/mode")


def _same(a: Any, b: Any) -> bool:
    """JSON 값 비교 — 키 순서 무관."""
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


class FakeServer:
    def __init__(self) -> None:
        self.clusters: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.teams: dict[str, dict[str, str]] = {}     # team_id → {user_id: "ADMIN" | "MEMBER"}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._codes: dict[str, itertools.count] = {}

    # ── 테스트가 상태를 만들고 읽는 도구 (server_api 에는 없다) ─────────────

    def add_member(self, team_id: str, user_id: str, role: str = "MEMBER") -> None:
        self.teams.setdefault(team_id, {})[user_id] = role

    def add_cluster(
        self, *, owner: str = "u-1", team_id: str | None = None, name: str | None = None,
        context: str = "kind-dev", namespace: str = "study", fingerprint: str | None = None,
        credential: dict[str, str] | None = None, api_server: str = "https://cluster.example",
        ca_data: str | None = "QQ==", insecure: bool = False, has_gpu: bool | None = None,
    ) -> str:
        cluster_id = str(uuid.uuid4())
        self.clusters[cluster_id] = {
            "id": cluster_id, "teamId": team_id, "registeredBy": owner, "name": name or cluster_id[:8],
            "provider": "GENERIC", "apiServer": api_server, "caData": ca_data, "insecure": insecure,
            "contextName": context, "namespace": namespace, "fingerprint": fingerprint or f"fp-{cluster_id}",
            "credential": credential or {"token": "secret-token"}, "status": "disconnected", "lastCheckedAt": None,
            "hasGpu": has_gpu,
        }
        return cluster_id

    def add_conversation(
        self, *, owner: str = "u-1", shared: bool = False, cluster_id: str | None = None,
        team_id: str | None = None, title: str = DEFAULT_TITLE, namespace: str | None = None,
    ) -> str:
        """POST /member/conversations 와 같은 결과 — 클러스터의 context·지문을 방에 복사한다."""
        if cluster_id is None:
            cluster_id = self.add_cluster(owner=owner, team_id=team_id)
        cluster = self.clusters[cluster_id]
        conversation_id = str(uuid.uuid4())
        self.conversations[conversation_id] = {
            "id": conversation_id, "clusterId": cluster_id, "userId": owner, "title": title or DEFAULT_TITLE,
            "shared": shared, "context": cluster["contextName"],
            "namespace": namespace or cluster["namespace"], "clusterFingerprint": cluster["fingerprint"],
            "version": 1, "updatedAt": _now(),
        }
        return conversation_id

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.conversations[conversation_id]

    def runs_of(self, conversation_id: str) -> list[dict[str, Any]]:
        return sorted((r for r in self.runs.values() if r["conversationId"] == conversation_id),
                      key=lambda r: r["seq"])

    def turns(self, conversation_id: str) -> list[dict[str, Any]]:
        """공개 API 의 TurnResponse 모양 (GET /member/conversations/{id} 의 turns) — 앱이 보는 기록."""
        out = []
        for run in self.runs_of(conversation_id):
            payload, error = run["responsePayload"], None
            if payload is not None and payload.get("kind") == "error":
                error = {k: v for k, v in payload.items() if k != "kind"}
                payload = None
            out.append({
                "id": run["id"], "seq": run["seq"], "kind": run["kind"], "mode": run["mode"], "status": run["status"],
                "input_text": run["inputText"], "payload": payload, "error": error,
                "started_at": run["startedAt"], "finished_at": run["finishedAt"],
            })
        return out

    def active_run_of(self, conversation_id: str) -> dict[str, Any] | None:
        return next((r for r in self.runs_of(conversation_id) if r["status"] in server_api.RUN_ACTIVE), None)

    def running(self, conversation_id: str) -> bool:
        active = self.active_run_of(conversation_id)
        return active is not None and active["status"] == "running"

    def pending(self, conversation_id: str) -> list[str]:
        """SessionResponse.pending — 대기·확인 필요 run 의 미결 plan 의 toolCallId."""
        active = self.active_run_of(conversation_id)
        if active is None or active["status"] == "running":
            return []
        return [p["toolCallId"] for p in self.plans_of_run(active["id"])
                if p["status"] in ("WAITING_APPROVAL", "APPROVED", "REJECTED", "EXECUTING")]

    def plans_of_run(self, run_id: str) -> list[dict[str, Any]]:
        return sorted((p for p in self.plans.values() if p["runId"] == run_id), key=lambda p: p["_order"])

    def plans_of(self, conversation_id: str) -> list[dict[str, Any]]:
        return [p for run in self.runs_of(conversation_id) for p in self.plans_of_run(run["id"])]

    def add_plan(
        self, run_id: str, *, tool_call_id: str, tool_name: str, risk: str, payload: dict[str, Any],
        status: str = "DRAFT", decision: dict[str, Any] | None = None,
        execution_result: dict[str, Any] | None = None,
    ) -> str:
        """상태 머신을 거치지 않고 plan 을 놓는다 — 가짜 _run_agent 가 낸 승인 카드처럼 훅을 안 거친 경우."""
        plan = {
            "id": str(uuid.uuid4()), "code": self._code(tool_name), "runId": run_id, "toolCallId": tool_call_id,
            "toolName": tool_name, "status": status, "risk": risk, "payload": copy.deepcopy(payload),
            "requestHash": "h", "decision": decision, "executionResult": execution_result, "failureReason": None,
            "appliedAt": None, "createdAt": _now(), "updatedAt": _now(), "_order": len(self.plans),
        }
        self.plans[plan["id"]] = plan
        return plan["id"]

    def plan_by_call(self, tool_call_id: str) -> dict[str, Any]:
        return next(p for p in self.plans.values() if p["toolCallId"] == tool_call_id)

    def put_run(self, conversation_id: str, *, request_id: str, status: str = "running", kind: str = "chat",
                input_text: str | None = "x", mode: str = "Private") -> dict[str, Any]:
        """저장에 실패해 활성으로 남은 run 같은, 정상 흐름으로는 만들기 어려운 상태를 직접 놓는다."""
        run = self._new_run(conversation_id, request_id=request_id, kind=kind, mode=mode, input_text=input_text)
        run["status"] = status
        return run

    # ── 권한 (contract 2절) ────────────────────────────────

    def _role(self, team_id: str, user_id: str) -> str | None:
        return self.teams.get(team_id, {}).get(user_id)

    def _readable_cluster(self, cluster: dict[str, Any], user_id: str) -> bool:
        if cluster["teamId"]:
            return self._role(cluster["teamId"], user_id) is not None
        return cluster["registeredBy"] == user_id

    def _conversation_for(self, conversation_id: str, user: User) -> dict[str, Any]:
        room = self.conversations.get(conversation_id)
        if room is None:
            raise _not_found(conversation_id)
        team_id = self.clusters[room["clusterId"]]["teamId"]
        if not room["shared"] or not team_id:
            if room["userId"] != user.id:
                raise _not_found(conversation_id)
        elif self._role(team_id, user.id) is None:
            raise _not_found(conversation_id)
        return room

    def _require_approver(self, room: dict[str, Any], user: User) -> None:
        if not room["shared"]:
            raise ServerError(403, "PRIVATE_SESSION", "Private 대화에서는 승인할 수 없다")
        team_id = self.clusters[room["clusterId"]]["teamId"]
        if team_id:
            if self._role(team_id, user.id) == "ADMIN" or room["userId"] == user.id:
                return
            raise ServerError(403, "NOT_TEAM_ADMIN", "승인·재개는 팀 Admin 이나 대화를 만든 사람만")
        if room["userId"] != user.id:
            raise ServerError(403, "FORBIDDEN", "승인·재개는 대화를 만든 사람만")

    # ── 대화 · run ─────────────────────────────────────────

    async def conversation_context(self, conversation_id: str, user: User, *, purpose: str):
        self.calls.append(("conversation_context", {"id": conversation_id, "user": user.id, "purpose": purpose}))
        room = self._conversation_for(conversation_id, user)
        if purpose == "approve":
            self._require_approver(room, user)
        active = self.active_run_of(conversation_id)
        completed = [r for r in self.runs_of(conversation_id) if r["status"] == "completed" and r["agentMessages"]]
        data = {
            "userId": user.id,
            "conversation": {
                **{k: room[k] for k in ("id", "clusterId", "userId", "title", "shared", "context",
                                        "namespace", "clusterFingerprint")},
                "teamId": self.clusters[room["clusterId"]]["teamId"],
            },
            "activeRun": self._run_json(active) if active else None,
            "activeRunPlans": [self._plan_json(p) for p in self.plans_of_run(active["id"])] if active else [],
            "history": [{"runId": r["id"], "agentMessages": copy.deepcopy(r["agentMessages"])}
                        for r in completed[-100:]],
        }
        return server_api.ConversationContext.from_json(data)

    async def find_run(self, conversation_id: str, request_id: str, user: User):
        self.calls.append(("find_run", {"id": conversation_id, "requestId": request_id}))
        self._conversation_for(conversation_id, user)
        found = next((r for r in self.runs_of(conversation_id) if r["requestId"] == request_id), None)
        return server_api.Run.from_json(self._run_json(found)) if found else None

    def _new_run(self, conversation_id: str, *, request_id: str, kind: str, mode: str,
                 input_text: str | None) -> dict[str, Any]:
        seq = max((r["seq"] for r in self.runs_of(conversation_id)), default=0) + 1
        run = {
            "id": str(uuid.uuid4()), "conversationId": conversation_id, "requestId": request_id, "seq": seq,
            "kind": kind, "mode": mode, "status": "running", "inputText": input_text, "responsePayload": None,
            "agentMessages": None, "usageSummary": None, "startedAt": _now(), "finishedAt": None,
        }
        self.runs[run["id"]] = run
        self.conversations[conversation_id]["updatedAt"] = _now()
        return run

    async def start_run(self, conversation_id: str, user: User, *, request_id: str, kind: str, mode: str,
                        input_text: str | None):
        self.calls.append(("start_run", {"id": conversation_id, "requestId": request_id, "kind": kind}))
        self._conversation_for(conversation_id, user)
        existing = next((r for r in self.runs_of(conversation_id) if r["requestId"] == request_id), None)
        if existing is not None:
            if existing["kind"] != kind or existing["inputText"] != input_text:
                raise ServerError(409, "REQUEST_MISMATCH", f"같은 requestId 에 다른 입력: {request_id}")
            return server_api.Run.from_json(self._run_json(existing)), False
        if self.active_run_of(conversation_id) is not None:
            raise ServerError(409, "ACTIVE_RUN_EXISTS", "이 대화에 끝나지 않은 run 이 있다")
        run = self._new_run(conversation_id, request_id=request_id, kind=kind, mode=mode, input_text=input_text)
        return server_api.Run.from_json(self._run_json(run)), True

    def _close_plans(self, run_id: str) -> list[dict[str, Any]]:
        closed = []
        for plan in self.plans_of_run(run_id):
            if plan["status"] in PLAN_OPEN:
                approved = bool(plan["decision"] and plan["decision"].get("approved"))
                plan["status"] = "UNKNOWN" if approved and plan["executionResult"] is None else "EXPIRED"
                plan["updatedAt"] = _now()
                closed.append(plan)
        return closed

    async def update_run(self, run_id: str, *, status=None, response_payload=None, append_agent_messages=None,
                         usage_summary=None, title_from=None):
        fields = {k: v for k, v in dict(status=status, response_payload=response_payload,
                                        append_agent_messages=append_agent_messages, usage_summary=usage_summary,
                                        title_from=title_from).items() if v is not None}
        self.calls.append(("update_run", {"runId": run_id, **fields}))
        run = self.runs.get(run_id)
        if run is None:
            raise _not_found(run_id)
        room = self.conversations[run["conversationId"]]
        closed: list[dict[str, Any]] = []
        if response_payload is not None:
            run["responsePayload"] = copy.deepcopy(response_payload)
        if append_agent_messages is not None:
            run["agentMessages"] = (run["agentMessages"] or []) + copy.deepcopy(append_agent_messages)
        if usage_summary is not None:
            run["usageSummary"] = usage_summary
        if status is not None:
            run["status"] = status
            if status in server_api.RUN_FINAL:
                run["finishedAt"] = _now()
                closed = self._close_plans(run_id)
            else:
                run["finishedAt"] = None
        if title_from is not None and _titleable(title_from) and room["title"] == DEFAULT_TITLE:
            earlier = [r for r in self.runs_of(room["id"])
                       if r["kind"] == "chat" and r["seq"] < run["seq"] and _titleable(r["inputText"])]
            if run["kind"] == "chat" and not earlier:
                room["title"] = " ".join(title_from.split())[:30].strip()
                room["version"] += 1
        return server_api.RunUpdate(server_api.Run.from_json(self._run_json(run)),
                                    [server_api.Plan.from_json(self._plan_json(p)) for p in closed])

    def _interrupt_run(self, run: dict[str, Any], message: str) -> list[dict[str, Any]]:
        payload = run["responsePayload"] or {}
        if payload.get("kind") != "error":
            run["responsePayload"] = {"kind": "error", "code": "INTERRUPTED", "message": message, "http_status": 409}
        run["status"] = "interrupted"
        run["finishedAt"] = _now()
        return self._close_plans(run["id"])

    async def interrupt(self, conversation_id: str, message: str):
        self.calls.append(("interrupt", {"id": conversation_id, "message": message}))
        runs = [r for r in self.runs_of(conversation_id) if r["status"] in server_api.RUN_ACTIVE]
        closed = [p for run in runs for p in self._interrupt_run(run, message)]
        return server_api.Interrupted([server_api.Run.from_json(self._run_json(r)) for r in runs],
                                      [server_api.Plan.from_json(self._plan_json(p)) for p in closed])

    async def interrupt_orphaned(self, message: str) -> int:
        self.calls.append(("interrupt_orphaned", {"message": message}))
        runs = [r for r in self.runs.values() if r["status"] == "running"]
        for run in runs:
            self._interrupt_run(run, message)
        return len(runs)

    # ── 클러스터 ───────────────────────────────────────────

    async def cluster_connection(self, cluster_id: str, user: User, *, expected_fingerprint: str | None = None):
        self.calls.append(("cluster_connection", {"id": cluster_id, "user": user.id,
                                                  "expectedFingerprint": expected_fingerprint}))
        cluster = self.clusters.get(cluster_id)
        if cluster is None or not self._readable_cluster(cluster, user.id):
            raise _not_found(cluster_id)
        if expected_fingerprint and cluster["fingerprint"] != expected_fingerprint:
            raise ServerError(409, "CLUSTER_CHANGED", "이 대화가 쓰던 클러스터의 접속 대상이 바뀌었다")
        return server_api.ClusterConnection.from_json({
            "clusterId": cluster_id, **{k: cluster[k] for k in (
                "name", "apiServer", "caData", "insecure", "contextName", "namespace", "fingerprint", "credential",
                "hasGpu")},
        })

    async def update_cluster_status(self, cluster_id: str, *, status: str, checked_at: datetime):
        self.calls.append(("update_cluster_status", {"id": cluster_id, "status": status}))
        cluster = self.clusters.get(cluster_id)
        if cluster is None:
            raise _not_found(cluster_id)
        cluster["status"] = status
        cluster["lastCheckedAt"] = checked_at.isoformat()
        return {k: v for k, v in cluster.items() if k != "credential"}

    async def update_cluster_gpu(self, cluster_id: str, *, has_gpu: bool, fingerprint: str):
        self.calls.append(("update_cluster_gpu", {"id": cluster_id, "hasGpu": has_gpu, "fingerprint": fingerprint}))
        cluster = self.clusters.get(cluster_id)
        if cluster is None:
            raise _not_found(cluster_id)
        if cluster["fingerprint"] != fingerprint:
            raise ServerError(409, "CLUSTER_CHANGED", "이 대화가 쓰던 클러스터의 접속 대상이 바뀌었다")
        cluster["hasGpu"] = has_gpu
        return {k: v for k, v in cluster.items() if k != "credential"}

    # ── Action Plan (contract 3절 상태 머신) ─────────────────

    def _code(self, tool: str) -> str:
        base = f"ap-{datetime.now(timezone.utc):%y%m%d-%H%M}-{tool}"
        n = next(self._codes.setdefault(base, itertools.count(1)))
        return base if n == 1 else f"{base}-{n}"

    async def create_plan(self, run_id: str, *, tool_call_id: str, tool_name: str, risk: str, request_hash: str,
                          payload: dict[str, Any]):
        self.calls.append(("create_plan", {"runId": run_id, "toolCallId": tool_call_id}))
        if run_id not in self.runs:
            raise _not_found(run_id)
        existing = next((p for p in self.plans_of_run(run_id) if p["toolCallId"] == tool_call_id), None)
        if existing is not None:
            return server_api.Plan.from_json(self._plan_json(existing))
        plan = {
            "id": str(uuid.uuid4()), "code": self._code(tool_name), "runId": run_id, "toolCallId": tool_call_id,
            "toolName": tool_name, "status": "DRAFT", "risk": risk, "payload": copy.deepcopy(payload),
            "requestHash": request_hash, "decision": None, "executionResult": None, "failureReason": None,
            "appliedAt": None, "createdAt": _now(), "updatedAt": _now(), "_order": len(self.plans),
        }
        self.plans[plan["id"]] = plan
        return server_api.Plan.from_json(self._plan_json(plan))

    async def run_plans(self, run_id: str, *, tool_call_id: str | None = None):
        self.calls.append(("run_plans", {"runId": run_id, "toolCallId": tool_call_id}))
        plans = self.plans_of_run(run_id)
        if tool_call_id is not None:
            plans = [p for p in plans if p["toolCallId"] == tool_call_id]
        return [server_api.Plan.from_json(self._plan_json(p)) for p in plans]

    async def get_plan(self, plan_id: str):
        plan = self.plans.get(plan_id)
        if plan is None:
            raise _not_found(plan_id)
        return server_api.Plan.from_json(self._plan_json(plan))

    async def plan_action(self, plan_id: str, action: str, body: dict[str, Any] | None = None):
        body = body or {}
        self.calls.append(("plan_action", {"id": plan_id, "action": action, **body}))
        plan = self.plans.get(plan_id)
        if plan is None:
            raise _not_found(plan_id)
        getattr(self, "_" + action.replace("-", "_"))(plan, body)
        plan["updatedAt"] = _now()
        return server_api.Plan.from_json(self._plan_json(plan))

    def _dry_run(self, plan: dict[str, Any], body: dict[str, Any]) -> None:
        if plan["status"] != "DRAFT" or plan["payload"].get("dry_run_result") is not None:
            raise _conflict()
        plan["payload"]["dry_run_result"] = {"status": body["status"], "stdout": body["stdout"],
                                             "stderr": body["stderr"], "at": _at()}
        if body["status"] == "failed":
            plan["status"], plan["failureReason"] = "FAILED", "DRY_RUN_FAILED"

    def _offer(self, plan: dict[str, Any], body: dict[str, Any]) -> None:
        guidance = (body.get("decisionGuidance") or "").strip()
        if not guidance:
            raise ServerError(400, "INVALID_REQUEST", "decisionGuidance 가 비었다")
        dry = plan["payload"].get("dry_run_result") or {}
        if (plan["status"] != "DRAFT" or dry.get("status") not in ("succeeded", "unsupported")
                or plan["payload"].get("decision_guidance") is not None):
            raise _conflict()
        plan["payload"]["decision_guidance"] = guidance
        plan["status"] = "WAITING_APPROVAL"

    def _decision(self, plan: dict[str, Any], body: dict[str, Any]) -> None:
        approved = bool(body["approved"])
        if not approved and plan["status"] == "REJECTED" and plan["decision"] and not plan["decision"]["approved"]:
            return                                           # 거절 재시도는 그대로 성공
        if (plan["status"] not in ("DRAFT", "WAITING_APPROVAL") or plan["decision"] is not None
                or plan["executionResult"] is not None):
            raise _conflict()
        if approved:
            dry = plan["payload"].get("dry_run_result") or {}
            if dry.get("status") not in ("succeeded", "unsupported") or not plan["payload"].get("decision_guidance"):
                raise _conflict()
            call = body.get("call") or {}
            expected = {"tool": plan["toolName"], "args": plan["payload"].get("args"),
                        "command": plan["payload"].get("command"), "risk": plan["risk"],
                        "target": plan["payload"].get("target")}
            for name, value in expected.items():
                if name not in call or not _same(call[name], value):
                    raise _conflict("PLAN_CALL_MISMATCH", f"approved request mismatch: {name}")
        plan["decision"] = {"approved": approved, "user_id": body.get("userId"), "at": _at()}
        plan["status"] = "APPROVED" if approved else "REJECTED"

    def _start(self, plan: dict[str, Any], body: dict[str, Any]) -> None:
        if plan["status"] != "APPROVED":
            raise _conflict()
        plan["status"] = "EXECUTING"

    def _result(self, plan: dict[str, Any], body: dict[str, Any]) -> None:
        if (plan["status"] not in ("APPROVED", "EXECUTING") or plan["decision"] is None
                or plan["executionResult"] is not None):
            raise _conflict()
        at = _at()
        plan["executionResult"] = {"success": body["success"], "stdout": body["stdout"], "stderr": body["stderr"],
                                   "exit_code": body.get("exitCode"), "at": at}
        if body["success"] and body.get("exitCode") == 0:
            plan["status"], plan["appliedAt"] = "APPLIED", at.removesuffix("Z")
        else:
            plan["status"], plan["failureReason"] = "FAILED", "EXECUTION_FAILED"

    def _fail(self, plan: dict[str, Any], body: dict[str, Any]) -> None:
        if plan["status"] not in PLAN_OPEN:
            raise _conflict()
        plan["status"], plan["failureReason"] = "FAILED", body["reason"]

    def _unknown(self, plan: dict[str, Any], body: dict[str, Any]) -> None:
        if plan["status"] not in ("APPROVED", "EXECUTING") or plan["executionResult"] is not None:
            raise _conflict()
        plan["status"] = "UNKNOWN"

    # ── JSON ───────────────────────────────────────────────

    @staticmethod
    def _run_json(run: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(run)

    @staticmethod
    def _plan_json(plan: dict[str, Any]) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in plan.items() if not k.startswith("_")}


def install(monkeypatch) -> FakeServer:
    """server_api 의 함수들을 가짜의 메서드로 바꿔 끼운다. 테스트가 그 위에 다시 monkeypatch 해 고장을 낼 수 있다."""
    fake = FakeServer()
    for name in server_api.API:
        monkeypatch.setattr(server_api, name, getattr(fake, name))
    return fake
