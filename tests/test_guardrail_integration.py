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

    def dry_run(command, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        assert context == "kind-dev"
        assert dry_run is True
        return KubectlResult(
            command="kubectl --context kind-dev dry-run",
            stdout="server dry-run succeeded\n",
            stderr="",
            success=True,
            exit_code=0,
        )

    async def guidance(plan, manifest_preview=None):
        return "대상과 복구 기준을 확인한다."

    def execute(command, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
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
    requested = False

    def model_call(messages, info):
        nonlocal requested
        if requested:
            return ModelResponse(parts=[ToolCallPart(
                tool_name=info.output_tools[0].name,
                args={"narration": "변경을 실행하지 못했습니다."},
            )])
        requested = True
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


@pytest.mark.parametrize("approved", [False, True])
def test_Electron결정은_Hook과_Plan까지_한번만_반영한다(
    client,
    guarded_runtime,
    approved,
):
    tool_name, args, risk = "delete_resource", DELETE_ARGS, "destructive"
    expected_command = ["delete", "deployment", "nginx", "-n", "study"]
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
    assert ActionPlan.find_by_call_id(call_id).status == "WAITING_APPROVAL"

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
        assert plan.status == "APPLIED"
        assert plan.execution_result["success"] is True
    else:
        assert guarded_runtime == []
        assert plan.status == "REJECTED"
        assert plan.execution_result is None

    repeated = client.post(
        "/approve",
        json={"call_id": call_id, "approved": approved},
    )
    assert repeated.status_code == 409
    assert len(guarded_runtime) == int(approved)


@pytest.mark.parametrize("failure", ["dry_run", "intent", "risk"])
def test_검토실패는_HTTP승인없이_종료하고_mutation을_실행하지않는다(
    client, monkeypatch, tmp_path, guarded_runtime, failure,
):
    args = dict(SCALE_ARGS)
    if failure == "dry_run":
        monkeypatch.setattr(hook, "run_kubectl", lambda *a, **kw: KubectlResult(
            command="kubectl dry-run", stdout="", stderr="Forbidden", success=False,
            exit_code=1,
        ))
    elif failure == "intent":
        args["intent"] = " "
    else:
        monkeypatch.delitem(hook.RISK_STICKERS, "scale_resource")

    with agent.override(model=_tool_model("scale_resource", args, "blocked")):
        response = client.post("/chat", json={"text": "변경해줘"})

    assert response.status_code == 200
    assert response.json()["kind"] == "answer"
    assert response.json()["response"]["steps"] == []
    assert client.get("/session").json()["pending"] == []
    assert client.post("/approve", json={"call_id": "blocked", "approved": True}).status_code == 409
    assert guarded_runtime == []
    if failure == "dry_run":
        plan = ActionPlan.find_by_call_id("blocked")
        assert plan.status == "FAILED"
        assert plan.dry_run_result["stderr"] == "Forbidden"
        assert plan.decision is None and plan.execution_result is None
    else:
        assert list(tmp_path.glob("*.md")) == []


@pytest.mark.parametrize("failure", ["unsupported", "guidance"])
def test_판단보조실패는_승인DTO에_표시하고_사용자결정을_기다린다(
    client, monkeypatch, guarded_runtime, failure,
):
    if failure == "unsupported":
        monkeypatch.setattr(hook, "run_kubectl", lambda *a, **kw: KubectlResult(
            command="kubectl dry-run", stdout="", stderr="unknown flag: --dry-run",
            success=False, exit_code=1,
        ))

    async def unavailable(plan, manifest_preview=None):
        if failure == "unsupported":
            pytest.fail("dry-run 미지원이면 guidance를 생성하면 안 된다")
        raise RuntimeError("guidance service unavailable")

    monkeypatch.setattr(hook, "generate_decision_guidance", unavailable)
    # 보호 namespace에서도 CAUTION 유지 및 정상 승인 계약을 함께 검증한다.
    args = {**SCALE_ARGS, "namespace": "kube-system"}
    with agent.override(model=_tool_model("scale_resource", args, "fallback")):
        response = client.post("/chat", json={"text": "변경해줘"})

    assert response.status_code == 200
    card, = response.json()["approvals"]
    assert card["risk"] == "caution"
    assert card["target"]["namespace"] == "kube-system"
    assert card["decision_guidance"] == "guidance unavailable"
    assert card["dry_run_result"]["status"] == (
        "unsupported" if failure == "unsupported" else "succeeded"
    )
    if failure == "unsupported":
        assert card["dry_run_result"]["stderr"] == "unknown flag: --dry-run"
    assert guarded_runtime == []
    with agent.override(model=_answer_model()):
        approved = client.post("/approve", json={"call_id": "fallback", "approved": True})
    assert approved.status_code == 200
    assert len(guarded_runtime) == 1
    assert ActionPlan.find_by_call_id("fallback").status == "APPLIED"


@pytest.mark.parametrize("success", [True, False])
def test_실행후_응답실패를_resume해도_변경은_한번이고_저장결과를_반환한다(
    client, monkeypatch, guarded_runtime, success,
):
    executions = []
    command = "kubectl --context kind-dev scale deployment nginx --replicas=3 -n study"
    stdout, stderr = ("scaled\n", "") if success else ("", "Forbidden\n")

    def execute(args, **kwargs):
        executions.append(args)
        return KubectlResult(
            command=command, stdout=stdout, stderr=stderr,
            success=success, exit_code=0 if success else 1,
        )

    monkeypatch.setattr(mutate, "run_kubectl", execute)
    with agent.override(model=_tool_model("scale_resource", SCALE_ARGS, "resume")):
        pending = client.post("/chat", json={"text": "변경해줘"})
    assert pending.status_code == 200
    assert executions == []

    def response_failure(messages, info):
        raise RuntimeError("model unavailable after kubectl execution")

    with agent.override(model=FunctionModel(response_failure)):
        failed = client.post("/approve", json={"call_id": "resume", "approved": True})
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "RESUME_RETRYABLE"
    plan = ActionPlan.find_by_call_id("resume")
    assert plan.status == ("APPLIED" if success else "FAILED")
    assert plan.execution_result["stdout"] == stdout
    assert plan.execution_result["stderr"] == stderr
    assert plan.execution_result["exit_code"] == (0 if success else 1)
    assert len(executions) == 1
    recorded = plan.path.read_text()

    with agent.override(model=_answer_model()):
        resumed = client.post("/resume")
    assert resumed.status_code == 200
    assert resumed.json()["kind"] == "answer"
    step, = resumed.json()["response"]["steps"]
    assert step["command"] == command
    assert step["output"] == (stdout if success else stderr)
    assert step["access"] == "mutating"
    assert len(executions) == 1
    assert plan.path.read_text() == recorded
    assert client.post("/resume").status_code == 409
    assert client.post("/approve", json={"call_id": "resume", "approved": True}).status_code == 409
