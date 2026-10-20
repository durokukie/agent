"""Kukie API 서버 내부 API 클라이언트 (kukie_agent/server_api.py) — 헤더·오류 옮기기·fail-closed (DURO-113).

다른 테스트는 server_api 의 함수를 메모리 가짜로 바꿔 끼우므로, 진짜 HTTP 층은 여기서만 본다.
httpx.MockTransport 로 요청을 받아 contract 3절의 경로·헤더·JSON 모양을 확인한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from kukie_agent import server_api
from kukie_agent.auth import User
from kukie_agent.guardrail.action_plan import ActionPlan
from kukie_agent.server_api import ServerError

URL = "http://server.test"
TOKEN = "internal-secret"
USER = User(id="u-1", token="user-access-token")

RUN = {"id": "r-1", "conversationId": "c-1", "requestId": "req-1", "seq": 1, "kind": "chat", "mode": "Private",
       "status": "running", "inputText": "파드", "responsePayload": None, "agentMessages": None,
       "usageSummary": None, "startedAt": "2026-10-14T09:00:00", "finishedAt": None}
PLAN = {"id": "0199f0a0-0000-7000-8000-000000000001", "code": "ap-261014-0900-scale_resource", "runId": "r-1",
        "toolCallId": "call-1", "toolName": "scale_resource", "status": "WAITING_APPROVAL", "risk": "caution",
        "payload": {"created_at": "2026-10-14T09:00:00+00:00", "args": {"kind": "deployment", "name": "nginx"},
                    "skill": "Shared", "target": {"context": "kind-dev"}, "command": ["scale"], "intent": "늘린다",
                    "expected_effects": ["3개"], "side_effects": ["자원"],
                    "dry_run_result": {"status": "succeeded", "stdout": "", "stderr": "", "at": "2026-10-14T09:00:00Z"},
                    "decision_guidance": "확인한다"},
        "requestHash": "h", "decision": None, "executionResult": None, "failureReason": None, "appliedAt": None,
        "createdAt": "2026-10-14T09:00:00", "updatedAt": "2026-10-14T09:00:00"}


@pytest.fixture
def server(monkeypatch):
    """KUKIE_SERVER_URL·KUKIE_INTERNAL_TOKEN 을 켜고, 공유 http 클라이언트를 MockTransport 로 바꾼다.
    respond(request) -> httpx.Response 를 넣으면 받은 요청 list 를 돌려준다."""
    def install(respond):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return respond(request)

        monkeypatch.setenv("KUKIE_SERVER_URL", URL + "/")
        monkeypatch.setenv("KUKIE_INTERNAL_TOKEN", TOKEN)
        monkeypatch.setattr(server_api, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return seen

    return install


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


@pytest.mark.asyncio
async def test_입구_호출은_내부_토큰과_사용자_Bearer를_함께_싣는다(server):
    seen = server(lambda r: httpx.Response(200, json={
        "userId": "u-1",
        "conversation": {"id": "c-1", "clusterId": "cl-1", "teamId": None, "userId": "u-1", "title": "새 대화",
                         "shared": True, "currentMode": "Private", "context": "kind-dev", "namespace": "study",
                         "clusterFingerprint": "fp"},
        "activeRun": {**RUN, "status": "awaiting_approval"}, "activeRunPlans": [PLAN],
        "history": [{"runId": "r-0", "agentMessages": [{"kind": "request"}]}],
    }))

    context = await server_api.conversation_context("c-1", USER, purpose="approve")

    request = seen[0]
    assert request.method == "GET" and request.url.path == "/internal/conversations/c-1/context"
    assert request.url.params["purpose"] == "approve"
    assert request.headers["X-Kukie-Internal-Token"] == TOKEN
    assert request.headers["Authorization"] == "Bearer user-access-token"
    assert context.conversation.cluster_fingerprint == "fp" and context.conversation.shared is True
    assert context.active_run.status == "awaiting_approval"
    assert context.active_run_plans[0].code == PLAN["code"]
    assert context.history[0].agent_messages == [{"kind": "request"}]


@pytest.mark.asyncio
async def test_입구가_아닌_호출은_사용자_토큰을_싣지_않는다(server):
    """access token 은 30분이면 만료된다 — run 도중 기록이 사용자 토큰 때문에 실패하면 kubectl 결과를 잃는다."""
    seen = server(lambda r: httpx.Response(200, json={"run": {**RUN, "status": "completed"}, "closedPlans": [PLAN]}))

    update = await server_api.update_run("r-1", status="completed", append_agent_messages=[{"m": 1}],
                                         current_mode="Shared", title_from="첫 마디")

    request = seen[0]
    assert request.method == "PATCH" and request.url.path == "/internal/runs/r-1"
    assert request.headers["X-Kukie-Internal-Token"] == TOKEN
    assert "Authorization" not in request.headers
    # 준 칸만 보낸다 (None = 그대로). 불투명 JSON 은 손대지 않는다
    assert _body(request) == {"status": "completed", "appendAgentMessages": [{"m": 1}],
                              "currentMode": "Shared", "titleFrom": "첫 마디"}
    assert update.run.status == "completed" and update.closed_plans[0].id == PLAN["id"]


@pytest.mark.asyncio
async def test_run_시작과_request_id로_찾기(server):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(201, json={"run": RUN, "created": True})
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "없다"})

    seen = server(respond)
    run, created = await server_api.start_run("c-1", USER, request_id="req-1", kind="chat", mode="Private",
                                              input_text="파드")
    assert created is True and run.id == "r-1"
    assert _body(seen[0]) == {"requestId": "req-1", "kind": "chat", "mode": "Private", "inputText": "파드"}
    assert await server_api.find_run("c-1", "req-x", USER) is None          # 404 는 "없다"
    assert seen[1].url.path == "/internal/conversations/c-1/runs/by-request/req-x"


@pytest.mark.asyncio
async def test_경로에_끼우는_값은_이스케이프해_다른_내부_경로로_새지_않는다(server):
    """request_id 는 클라이언트가 고른 문자열이다. `?`·`/` 가 그대로 들어가면 쿼리·경로가 바뀌고,
    `..` 는 httpx 가 정규화해 다른 내부 엔드포인트(예: /internal/runs/interrupt-orphaned)로 간다."""
    seen = server(lambda r: httpx.Response(404, json={"code": "NOT_FOUND", "message": "없다"}))

    assert await server_api.find_run("c-1", "a/b?purpose=x#y", USER) is None
    assert seen[0].url.raw_path == b"/internal/conversations/c-1/runs/by-request/a%2Fb%3Fpurpose%3Dx%23y"

    for bad in ("..", "."):
        with pytest.raises(ServerError) as raised:
            await server_api.find_run("c-1", bad, USER)
        assert raised.value.status_code == 404
    with pytest.raises(ServerError):
        await server_api.conversation_context("..", USER, purpose="chat")
    assert len(seen) == 1                                          # 이상한 id 는 server 에 보내지도 않는다


@pytest.mark.asyncio
async def test_server_오류는_같은_상태와_코드로_옮긴다(server):
    server(lambda r: httpx.Response(409, json={"code": "ACTIVE_RUN_EXISTS", "message": "활성 run 이 있다"}))

    with pytest.raises(ServerError) as raised:
        await server_api.start_run("c-1", USER, request_id="r", kind="chat", mode="Private", input_text="x")

    assert raised.value.status_code == 409
    assert raised.value.detail == {"code": "ACTIVE_RUN_EXISTS", "message": "활성 run 이 있다"}
    assert raised.value.code == "ACTIVE_RUN_EXISTS"


@pytest.mark.asyncio
async def test_내부_토큰이_틀리면_사용자_401이_아니라_503이다(server):
    """앱이 401 을 받으면 사용자 토큰 문제로 읽고 로그아웃한다 — 설정 문제는 서버 쪽 장애로 알린다."""
    server(lambda r: httpx.Response(401, json={"code": "INVALID_INTERNAL_TOKEN", "message": "x"}))

    with pytest.raises(ServerError) as raised:
        await server_api.interrupt("c-1", "중단")

    assert raised.value.status_code == 503 and raised.value.code == "SERVER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_사용자_토큰이_만료되면_401이_그대로_나간다(server):
    server(lambda r: httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "만료"}))

    with pytest.raises(ServerError) as raised:
        await server_api.conversation_context("c-1", USER, purpose="chat")

    assert raised.value.status_code == 401 and raised.value.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_server에_닿지_못하면_503_SERVER_UNAVAILABLE(server):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    server(refuse)
    with pytest.raises(ServerError) as raised:
        await server_api.get_plan("p-1")

    assert raised.value.status_code == 503 and raised.value.code == "SERVER_UNAVAILABLE"
    assert TOKEN not in raised.value.message


@pytest.mark.asyncio
async def test_코드_없는_5xx도_503으로(server):
    server(lambda r: httpx.Response(502, text="bad gateway"))

    with pytest.raises(ServerError) as raised:
        await server_api.run_plans("r-1")

    assert raised.value.status_code == 503 and raised.value.code == "SERVER_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["KUKIE_SERVER_URL", "KUKIE_INTERNAL_TOKEN"])
async def test_설정이_하나라도_없으면_부르지_않고_503(server, monkeypatch, missing):
    """fail-closed — 토큰 없이 server 를 부르거나 조용히 기록을 건너뛰지 않는다."""
    seen = server(lambda r: httpx.Response(200, json={}))
    monkeypatch.delenv(missing)

    assert server_api.configured() is False
    with pytest.raises(ServerError) as raised:
        await server_api.interrupt_orphaned("x")

    assert raised.value.status_code == 503 and raised.value.code == "SERVER_NOT_CONFIGURED"
    assert seen == []


@pytest.mark.asyncio
async def test_plan_동작과_연결_정보(server):
    def respond(request):
        if request.url.path.endswith("/connection"):
            return httpx.Response(200, json={
                "clusterId": "cl-1", "name": "prod", "apiServer": "https://k8s", "caData": None, "insecure": False,
                "contextName": "kind-dev", "namespace": "study", "fingerprint": "fp",
                "credential": {"token": "secret-token"}})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"id": "cl-1", "status": "connected"})
        return httpx.Response(200, json={**PLAN, "status": "APPROVED"})

    seen = server(respond)
    plan = await server_api.plan_action(PLAN["id"], "decision", {"approved": True, "userId": "u-1"})
    conn = await server_api.cluster_connection("cl-1", USER, expected_fingerprint="fp")
    await server_api.update_cluster_status("cl-1", status="connected",
                                           checked_at=datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc))

    assert seen[0].url.path == f"/internal/plans/{PLAN['id']}/decision" and plan.status == "APPROVED"
    assert seen[1].url.params["expectedFingerprint"] == "fp" and seen[1].headers["Authorization"].startswith("Bearer ")
    assert conn.credential == {"token": "secret-token"}
    assert "secret-token" not in repr(conn)                       # 로그에 찍혀도 자격증명은 안 나온다
    assert _body(seen[2]) == {"status": "connected", "checkedAt": "2026-10-14T18:00:00"}
    with pytest.raises(ValueError, match="unknown plan action"):
        await server_api.plan_action("p", "approve")


def test_server_계획의_Markdown은_UUID가_아니라_code를_보여준다():
    """승인 카드의 plan_id 는 UUID, 사람이 읽는 id 는 code. 판단 가이드 LLM 입력도 같은 Markdown 이다."""
    plan = ActionPlan.from_response(server_api.Plan.from_json(PLAN))

    assert plan.id == PLAN["id"] and plan.code == PLAN["code"] and plan.path is None and plan.remote
    markdown = plan.render_markdown(include_decision_guidance=False)
    assert f"id: {PLAN['code']}" in markdown and PLAN["id"] not in markdown
    assert "decision_guidance" not in markdown and "run_id: r-1" in markdown
