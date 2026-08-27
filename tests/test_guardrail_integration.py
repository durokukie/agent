"""Agent부터 Electron 승인 endpoint와 Action Plan까지의 통합 계약."""

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from kukie import server
from kukie.agent import agent
from kukie.guardrail import action_plan, hook
from kukie.guardrail.action_plan import ActionPlan
from kukie.kubectl import KubectlResult
from kukie.tools import mutate


SCALE_ARGS = {
    "kind": "deployment",
    "name": "nginx",
    "replicas": 3,
    "namespace": "study",
    "intent": "nginx 레플리카를 늘린다.",
    "expected_effects": ["레플리카가 3개가 된다."],
    "side_effects": ["추가 노드 자원을 사용한다."],
}
DELETE_ARGS = {
    "kind": "deployment",
    "name": "nginx",
    "namespace": "study",
    "intent": "nginx 배포를 삭제한다.",
    "expected_effects": ["Deployment가 삭제된다."],
    "side_effects": ["서비스가 중단된다."],
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    server._session = None
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    test_client = TestClient(server.app)
    assert test_client.post("/session").status_code == 200
    assert test_client.post("/chat", json={"text": "/mode 실습"}).status_code == 200
    yield test_client
    server._session = None


@pytest.fixture
def guarded_runtime(monkeypatch):
    executions = []

    def dry_run(command, *, context, dry_run=False, stdin=None, timeout=30):
        assert context == "kind-dev"
        assert dry_run is True
        return KubectlResult(
            command="kubectl --context kind-dev dry-run",
            stdout="server dry-run succeeded\n",
            stderr="",
            success=True,
            exit_code=0,
        )

    async def guidance(plan):
        return "대상과 복구 기준을 확인한다."

    def execute(command, *, context, dry_run=False, stdin=None, timeout=30):
        executions.append((command, context, stdin))
        return KubectlResult(
            command=f"kubectl --context {context} {' '.join(command)}",
            stdout="mutation succeeded\n",
            stderr="",
            success=True,
            exit_code=0,
        )

    monkeypatch.setattr(hook, "run_kubectl", dry_run)
    monkeypatch.setattr(hook, "generate_decision_guidance", guidance)
    monkeypatch.setattr(mutate, "run_kubectl", execute)
    return executions


def _tool_model(tool_name, args, call_id):
    def model_call(messages, info):
        return ModelResponse(parts=[ToolCallPart(
            tool_name=tool_name,
            args=dict(args),
            tool_call_id=call_id,
        )])

    return FunctionModel(model_call)


def _answer_model():
    return TestModel(
        call_tools=[],
        custom_output_args={"narration": "결정을 반영했습니다."},
    )


def test_TestModel요청도_실제Hook을_거쳐_backend승인DTO가된다(
    client, monkeypatch, guarded_runtime
):
    def forbidden_handler(*args, **kwargs):
        pytest.fail("승인 전에 mutation handler가 실행되면 안 된다")

    monkeypatch.setattr(mutate, "run_kubectl", forbidden_handler)

    with agent.override(model=TestModel(
        call_tools=["scale_resource"],
        custom_output_args={"narration": "승인을 기다립니다."},
    )):
        response = client.post("/chat", json={"text": "nginx를 늘려줘"})

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "approval"
    assert len(body["approvals"]) == 1
    assert body["approvals"][0]["tool"] == "scale_resource"
    assert body["approvals"][0]["risk"] == "caution"
    assert body["approvals"][0]["dry_run_result"]["status"] == "succeeded"
    assert guarded_runtime == []


@pytest.mark.parametrize(
    ("tool_name", "args", "risk", "expected_command"),
    [
        (
            "scale_resource",
            SCALE_ARGS,
            "caution",
            ["scale", "deployment", "nginx", "--replicas=3", "-n", "study"],
        ),
        (
            "delete_resource",
            DELETE_ARGS,
            "destructive",
            ["delete", "deployment", "nginx", "-n", "study"],
        ),
    ],
)
@pytest.mark.parametrize("approved", [False, True])
def test_Electron결정은_Hook과_Plan까지_한번만_반영한다(
    client,
    guarded_runtime,
    tool_name,
    args,
    risk,
    expected_command,
    approved,
):
    call_id = f"call-{tool_name}-{approved}"
    with agent.override(model=_tool_model(tool_name, args, call_id)):
        pending = client.post("/chat", json={"text": "변경해줘"})

    assert pending.status_code == 200
    card = pending.json()["approvals"][0]
    assert card["tool_call_id"] == call_id
    assert card["tool"] == tool_name
    assert card["risk"] == risk
    assert card["command"] == expected_command
    assert card["dry_run_result"]["status"] == "succeeded"
    assert guarded_runtime == []
    assert ActionPlan.find_by_call_id(call_id).status == "draft"

    with agent.override(model=_answer_model()):
        decided = client.post(
            "/approve",
            json={"call_id": call_id, "approved": approved},
        )

    assert decided.status_code == 200
    assert decided.json()["kind"] == "answer"
    plan = ActionPlan.find_by_call_id(call_id)
    if approved:
        assert guarded_runtime == [(expected_command, "kind-dev", None)]
        assert plan.status == "executed"
        assert plan.execution_result["success"] is True
    else:
        assert guarded_runtime == []
        assert plan.status == "rejected"
        assert plan.execution_result is None

    repeated = client.post(
        "/approve",
        json={"call_id": call_id, "approved": approved},
    )
    assert repeated.status_code == 409
    assert len(guarded_runtime) == int(approved)
