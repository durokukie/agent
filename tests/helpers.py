"""여러 테스트 파일이 같이 쓰는 값과 만들기 함수. 상태를 바꾸는 준비(monkeypatch)는 conftest.py 의 픽스처에 둔다."""
from __future__ import annotations

from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from kukie.guardrail.action_plan import ActionPlan

MEMBER_URL = "http://member.test"
USER = {"X-User": "u-1"}
OTHER = {"X-User": "u-2"}
FAKE_COMMAND = "kubectl --context kind-dev get pods -n study -o wide"
GUIDANCE = "대상과 롤백 기준을 확인한다."


def chat_model(narration="답", call_tools=("list_resources",)) -> TestModel:
    return TestModel(call_tools=list(call_tools), custom_output_args={"narration": narration})


def answer_model(narration="결정을 반영했습니다.") -> TestModel:
    """승인 뒤 재개용 — 툴을 더 부르지 않고 답만 낸다."""
    return chat_model(narration, call_tools=())


def new_room(client, headers=USER, **body) -> str:
    r = client.post("/conversations", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["conversation"]["id"]


def ready_plan(call_id: str) -> ActionPlan:
    plan = ActionPlan.create_draft(
        call_id=call_id,
        tool="scale_resource",
        args={
            "kind": "deployment",
            "name": "nginx",
            "replicas": 3,
            "namespace": "study",
        },
        command=["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        risk="caution",
        skill="실습",
        target={
            "context": "kind-dev",
            "namespace": "study",
            "kind": "deployment",
            "name": "nginx",
        },
        intent="nginx 레플리카를 늘린다.",
        expected_effects=["레플리카가 3개가 된다."],
        side_effects=["추가 노드 자원을 사용한다."],
    )
    plan.record_dry_run("succeeded", "deployment.apps/nginx configured\n", "")
    plan.record_decision_guidance("현재 replica와 가용 자원을 확인한다.")
    plan.offer_for_approval()
    return plan


def pending_ticket(call_id: str) -> tuple[ActionPlan, DeferredToolRequests]:
    """승인을 기다리는 scale 한 건 — 계획서와 에이전트가 낸 승인 요청."""
    plan = ready_plan(call_id)
    call = ToolCallPart(
        tool_name="scale_resource",
        args={
            "kind": "deployment",
            "name": "nginx",
            "replicas": 3,
            "namespace": "study",
            "intent": plan.intent,
            "expected_effects": plan.expected_effects,
            "side_effects": plan.side_effects,
        },
        tool_call_id=call_id,
    )
    return plan, DeferredToolRequests(
        approvals=[call],
        metadata={call_id: {"plan_id": plan.id}},
    )
