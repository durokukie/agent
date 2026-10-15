"""여러 테스트 파일이 같이 쓰는 값과 만들기 함수. 상태를 바꾸는 준비(monkeypatch)는 conftest.py 의 픽스처에 둔다."""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from kukie_agent.guardrail.action_plan import ActionPlan

SERVER_URL = "http://server.test"
USER = {"X-User": "u-1"}
OTHER = {"X-User": "u-2"}
FAKE_COMMAND = "kubectl --context kind-dev get pods -n study -o wide"
GUIDANCE = "대상과 롤백 기준을 확인한다."

# scale 한 건 — 모델이 부르는 인자와, 가드레일이 Plan 에 남기는 정규화된 값
SCALE_PLAN_ARGS = {"kind": "deployment", "name": "nginx", "replicas": 3, "namespace": "study"}
SCALE_COMMAND = ["scale", "deployment", "nginx", "--replicas=3", "-n", "study"]
SCALE_TARGET = {"context": "kind-dev", "namespace": "study", "kind": "deployment", "name": "nginx"}
SCALE_INTENT = "nginx 레플리카를 늘린다."
SCALE_EXPECTED = ["레플리카가 3개가 된다."]
SCALE_SIDE = ["추가 노드 자원을 사용한다."]


def sync(coroutine):
    """동기 테스트에서 async 함수 하나를 끝까지 돌린다 (파일 계획은 실제로 기다리는 일이 없다)."""
    return asyncio.run(coroutine)


def chat_model(narration="답", call_tools=("list_resources",)) -> TestModel:
    return TestModel(call_tools=list(call_tools), custom_output_args={"narration": narration})


def answer_model(narration="결정을 반영했습니다.") -> TestModel:
    """승인 뒤 재개용 — 툴을 더 부르지 않고 답만 낸다."""
    return chat_model(narration, call_tools=())


def new_room(fake, user: str = "u-1", **fields: Any) -> str:
    """Kukie API 서버에 채팅방 하나 (POST /member/conversations 와 같은 결과). 만들기는 이제 server 몫이다."""
    return fake.add_conversation(owner=user, **fields)


def scale_call(call_id: str) -> ToolCallPart:
    """모델이 낸 scale 호출 — 승인 대기 티켓에 실리는 원본."""
    return ToolCallPart(
        tool_name="scale_resource",
        args={
            **SCALE_PLAN_ARGS,
            "intent": SCALE_INTENT,
            "expected_effects": SCALE_EXPECTED,
            "side_effects": SCALE_SIDE,
        },
        tool_call_id=call_id,
    )


def ready_plan(call_id: str) -> ActionPlan:
    """승인을 기다리는 파일 계획 (flat 엔드포인트, run 없음)."""
    async def make() -> ActionPlan:
        plan = await ActionPlan.create_draft(
            call_id=call_id,
            tool="scale_resource",
            args=dict(SCALE_PLAN_ARGS),
            command=list(SCALE_COMMAND),
            risk="caution",
            skill="실습",
            target=dict(SCALE_TARGET),
            intent=SCALE_INTENT,
            expected_effects=list(SCALE_EXPECTED),
            side_effects=list(SCALE_SIDE),
        )
        await plan.record_dry_run("succeeded", "deployment.apps/nginx configured\n", "")
        await plan.offer("현재 replica와 가용 자원을 확인한다.")
        return plan

    return sync(make())


def pending_ticket(call_id: str) -> tuple[ActionPlan, DeferredToolRequests]:
    """승인을 기다리는 scale 한 건 — 파일 계획서와 에이전트가 낸 승인 요청 (flat 엔드포인트)."""
    plan = ready_plan(call_id)
    return plan, DeferredToolRequests(
        approvals=[scale_call(call_id)],
        metadata={call_id: {"plan_id": plan.id}},
    )


def waiting_plan(fake, run_id: str, call_id: str) -> str:
    """대화 경로의 승인 대기 계획 — Kukie API 서버에 WAITING_APPROVAL 로 놓는다. 반환은 server 의 plan id."""
    return fake.add_plan(
        run_id,
        tool_call_id=call_id,
        tool_name="scale_resource",
        risk="caution",
        status="WAITING_APPROVAL",
        payload={
            "created_at": "2026-10-14T00:00:00+00:00",
            "args": dict(SCALE_PLAN_ARGS),
            "skill": "실습",
            "target": dict(SCALE_TARGET),
            "command": list(SCALE_COMMAND),
            "intent": SCALE_INTENT,
            "expected_effects": list(SCALE_EXPECTED),
            "side_effects": list(SCALE_SIDE),
            "dry_run_result": {"status": "succeeded", "stdout": "ok\n", "stderr": "", "at": "2026-10-14T00:00:00Z"},
            "decision_guidance": GUIDANCE,
        },
    )
