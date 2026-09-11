import subprocess

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
from kukie.guardrail.decision_guidance import guidance_agent
from kukie.kubectl.runner import run_kubectl as real_run_kubectl
from kukie.tools import mutate


pytestmark = pytest.mark.e2e


def test_E2E_context는_명시적인_kind_cluster다(e2e_context):
    assert e2e_context.startswith("kind-")


def test_kind_prefix_alias는_실제_kind_cluster가_아니면_거부한다(
    monkeypatch, request
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        stdout = "kukie-e2e\n" if command == ["kind", "get", "clusters"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setenv("KUKIE_E2E_CONTEXT", "kind-production")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(pytest.fail.Exception, match="kind가 관리하는 cluster"):
        request.getfixturevalue("e2e_context")

    assert calls == [["kind", "get", "clusters"]]


def test_kind_context는_관리_cluster와_API_identity가_같아야한다(
    monkeypatch, request
):
    calls = []
    selected = """clusters:
- cluster:
    certificate-authority-data: production-ca
    server: https://production.example
"""
    expected = """clusters:
- cluster:
    certificate-authority-data: kind-ca
    server: https://127.0.0.1:51234
"""

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["kind", "get", "clusters"]:
            stdout = "kukie-e2e\n"
        elif command[:2] == ["kubectl", "--context"]:
            stdout = selected
        elif command == ["kind", "get", "kubeconfig", "--name", "kukie-e2e"]:
            stdout = expected
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setenv("KUKIE_E2E_CONTEXT", "kind-kukie-e2e")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(pytest.fail.Exception, match="API server 또는 CA가 다릅니다"):
        request.getfixturevalue("e2e_context")

    assert calls == [
        ["kind", "get", "clusters"],
        [
            "kubectl",
            "--context",
            "kind-kukie-e2e",
            "config",
            "view",
            "--raw",
            "--minify",
            "--output=yaml",
        ],
        ["kind", "get", "kubeconfig", "--name", "kukie-e2e"],
    ]


@pytest.fixture
def client(monkeypatch, tmp_path, e2e_context, e2e_session_namespace):
    server._session = None
    monkeypatch.setattr(
        server,
        "read_kubeconfig",
        lambda: (e2e_context, e2e_session_namespace),
    )
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    test_client = TestClient(server.app)
    yield test_client
    server._session = None


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


def _decide(client, tool_name, args, call_id, approved):
    assert client.post("/session").status_code == 200
    assert client.post("/chat", json={"text": "/mode 실습"}).status_code == 200

    with guidance_agent.override(model=TestModel(
        custom_output_text="대상과 복구 기준을 확인한다."
    )):
        with agent.override(model=_tool_model(tool_name, args, call_id)):
            pending = client.post("/chat", json={"text": "변경해줘"})

    assert pending.status_code == 200
    assert pending.json()["kind"] == "approval"
    card = pending.json()["approvals"][0]
    assert card["tool_call_id"] == call_id
    assert card["tool"] == tool_name
    assert card["risk"] == (
        "destructive" if tool_name == "delete_resource" else "caution"
    )
    if tool_name == "rollout_restart":
        assert card["dry_run_result"]["status"] == "unsupported"
        assert card["dry_run_result"]["stderr"].strip()
        assert card["decision_guidance"] == "guidance unavailable"
    else:
        assert card["dry_run_result"]["status"] == "succeeded"
        assert card["decision_guidance"] == "대상과 복구 기준을 확인한다."

    with agent.override(model=_answer_model()):
        decided = client.post(
            "/approve",
            json={"call_id": call_id, "approved": approved},
        )

    assert decided.status_code == 200
    assert decided.json()["kind"] == "answer"
    return card, ActionPlan.find_by_call_id(call_id)


CONFIG_MAP = """apiVersion: v1
kind: ConfigMap
metadata:
  name: guardrail-config
data:
  enabled: "true"
"""

DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: guardrail-nginx
spec:
  replicas: 1
  selector:
    matchLabels:
      app: guardrail-nginx
  template:
    metadata:
      labels:
        app: guardrail-nginx
    spec:
      containers:
        - name: nginx
          image: registry.k8s.io/pause:3.10
"""

DESCRIPTIONS = {
    "intent": "격리된 E2E 리소스를 변경한다.",
    "expected_effects": ["테스트 대상 상태가 변경된다."],
    "side_effects": ["테스트 namespace의 리소스만 영향받는다."],
}


def _seed_deployment(kubectl_cli, context, namespace):
    kubectl_cli(
        context,
        "apply",
        "-f",
        "-",
        "-n",
        namespace,
        stdin=DEPLOYMENT,
    )


@pytest.mark.parametrize("approved", [False, True])
def test_apply_manifest_결정과_cluster상태가_일치한다(
    client,
    monkeypatch,
    kubectl_cli,
    e2e_context,
    e2e_namespace,
    e2e_session_namespace,
    approved,
):
    assert e2e_namespace != e2e_session_namespace
    calls = []

    def traced_run(command, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        calls.append((list(command), context, dry_run, stdin))
        return real_run_kubectl(
            command,
            context=context,
            dry_run=dry_run,
            stdin=stdin,
            timeout=timeout,
            kubeconfig=kubeconfig,
        )

    monkeypatch.setattr(hook, "run_kubectl", traced_run)
    monkeypatch.setattr(mutate, "run_kubectl", traced_run)
    args = {
        "manifest_yaml": CONFIG_MAP,
        "namespace": e2e_namespace,
        **DESCRIPTIONS,
    }

    _, plan = _decide(client, "apply_manifest", args, "call-apply", approved)

    found = kubectl_cli(
        e2e_context,
        "get",
        "configmap",
        "guardrail-config",
        "-n",
        e2e_namespace,
        "--ignore-not-found=true",
        "-o", "name",
    )
    assert found.stdout.strip() == ("configmap/guardrail-config" if approved else "")
    assert kubectl_cli(
        e2e_context, "get", "configmap", "guardrail-config",
        "-n", e2e_session_namespace, "--ignore-not-found=true", "-o", "name",
    ).stdout.strip() == ""
    assert plan.target["namespace"] == e2e_namespace
    assert plan.status == ("APPLIED" if approved else "REJECTED")
    expected_command = ["apply", "-f", "-", "-n", e2e_namespace]
    assert calls[0] == (expected_command, e2e_context, True, CONFIG_MAP)
    if approved:
        assert calls[1] == (expected_command, e2e_context, False, CONFIG_MAP)
    else:
        assert len(calls) == 1


@pytest.mark.parametrize("approved", [False, True])
def test_scale_resource_결정과_cluster상태가_일치한다(
    client,
    kubectl_cli,
    e2e_context,
    e2e_namespace,
    approved,
):
    _seed_deployment(kubectl_cli, e2e_context, e2e_namespace)
    args = {
        "kind": "deployment",
        "name": "guardrail-nginx",
        "replicas": 2,
        "namespace": e2e_namespace,
        **DESCRIPTIONS,
    }

    _, plan = _decide(client, "scale_resource", args, "call-scale", approved)

    replicas = kubectl_cli(
        e2e_context,
        "get",
        "deployment",
        "guardrail-nginx",
        "-n",
        e2e_namespace,
        "-o",
        "jsonpath={.spec.replicas}",
    ).stdout
    assert replicas == ("2" if approved else "1")
    assert plan.status == ("APPLIED" if approved else "REJECTED")


@pytest.mark.parametrize("approved", [False, True])
def test_rollout_restart_결정과_cluster상태가_일치한다(
    client,
    kubectl_cli,
    e2e_context,
    e2e_namespace,
    approved,
):
    _seed_deployment(kubectl_cli, e2e_context, e2e_namespace)
    args = {
        "kind": "deployment",
        "name": "guardrail-nginx",
        "namespace": e2e_namespace,
        **DESCRIPTIONS,
    }

    _, plan = _decide(client, "rollout_restart", args, "call-restart", approved)

    restarted_at = kubectl_cli(
        e2e_context,
        "get",
        "deployment",
        "guardrail-nginx",
        "-n",
        e2e_namespace,
        "-o",
        "jsonpath={.spec.template.metadata.annotations.kubectl\\.kubernetes\\.io/restartedAt}",
    ).stdout
    assert bool(restarted_at) is approved
    assert plan.status == ("APPLIED" if approved else "REJECTED")


@pytest.mark.parametrize("approved", [False, True])
def test_delete_resource_결정과_cluster상태가_일치한다(
    client,
    kubectl_cli,
    e2e_context,
    e2e_namespace,
    approved,
):
    _seed_deployment(kubectl_cli, e2e_context, e2e_namespace)
    args = {
        "kind": "deployment",
        "name": "guardrail-nginx",
        "namespace": e2e_namespace,
        **DESCRIPTIONS,
    }

    _, plan = _decide(client, "delete_resource", args, "call-delete", approved)

    found = kubectl_cli(
        e2e_context,
        "get",
        "deployment",
        "guardrail-nginx",
        "-n",
        e2e_namespace,
        "--ignore-not-found=true",
        "-o", "name",
    )
    assert found.stdout.strip() == ("" if approved else "deployment.apps/guardrail-nginx")
    assert plan.status == ("APPLIED" if approved else "REJECTED")
