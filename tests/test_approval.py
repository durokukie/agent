import hashlib

import pytest
from pydantic_ai.messages import ToolCallPart

from kukie.guardrail import action_plan
from kukie.guardrail.action_plan import ActionPlan
from kukie.guardrail.approval import ApprovalRequest, build_approval_request


CALL_ARGS = {
    "kind": "deployment",
    "name": "nginx",
    "replicas": 3,
    "namespace": "study",
    "intent": "nginx 레플리카를 늘린다.",
    "expected_effects": ["레플리카가 3개가 된다."],
    "side_effects": ["추가 노드 자원을 사용한다."],
}


def _ready_plan(
    monkeypatch,
    tmp_path,
    dry_run_status: str = "succeeded",
) -> ActionPlan:
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    plan = ActionPlan.create_draft(
        call_id="call-123",
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
        intent=CALL_ARGS["intent"],
        expected_effects=CALL_ARGS["expected_effects"],
        side_effects=CALL_ARGS["side_effects"],
    )
    stdout = "deployment.apps/nginx configured\n" if dry_run_status == "succeeded" else ""
    stderr = "server does not support dry run\n" if dry_run_status == "unsupported" else ""
    plan.record_dry_run(dry_run_status, stdout, stderr)
    plan.record_decision_guidance("현재 replica와 가용 자원을 확인한다.")
    return plan


def _ready_manifest_plan(monkeypatch, plan_dir, call_id: str, manifest: str) -> ActionPlan:
    monkeypatch.setattr(action_plan, "PLAN_DIR", plan_dir)
    plan = ActionPlan.create_draft(
        call_id=call_id,
        tool="apply_manifest",
        args={
            "namespace": "study",
            "manifest_sha256": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        },
        command=["apply", "-f", "-", "-n", "study"],
        risk="caution",
        skill="실습",
        target={
            "context": "kind-dev",
            "namespace": "study",
            "resources": [{"kind": "Pod", "name": "app"}],
        },
        intent="Pod를 배포한다.",
        expected_effects=["Pod가 적용된다."],
        side_effects=["클러스터 구성이 변경된다."],
    )
    plan.record_dry_run("unsupported", "", "server does not support dry run")
    plan.record_decision_guidance("대상과 권한을 확인한다.")
    return plan


def _manifest_dto(monkeypatch, plan_dir, call_id: str, manifest: str) -> ApprovalRequest:
    plan = _ready_manifest_plan(monkeypatch, plan_dir, call_id, manifest)
    return build_approval_request(
        ToolCallPart(
            tool_name="apply_manifest",
            args={
                "manifest_yaml": manifest,
                "namespace": "study",
                "intent": plan.intent,
                "expected_effects": plan.expected_effects,
                "side_effects": plan.side_effects,
            },
            tool_call_id=call_id,
        ),
        {"plan_id": plan.id},
        default_namespace="study",
    )


def test_apply_manifest_승인_DTO는_보안설정차이를_보존하고_원문을_노출하지않는다(
    monkeypatch, tmp_path
):
    safe = """apiVersion: v1
kind: Pod
metadata:
  name: app
spec:
  containers:
  - name: app
    image: example/app:latest
"""
    privileged = safe + "    securityContext:\n      privileged: true\n"

    safe_dto = _manifest_dto(monkeypatch, tmp_path / "safe", "safe", safe)
    privileged_dto = _manifest_dto(
        monkeypatch,
        tmp_path / "privileged",
        "privileged",
        privileged,
    )

    assert safe_dto.manifest_preview != privileged_dto.manifest_preview
    assert privileged_dto.manifest_preview[0]["spec"]["containers"][0][
        "securityContext"
    ] == {"privileged": True}
    assert privileged_dto.manifest_sha256 == hashlib.sha256(
        privileged.encode("utf-8")
    ).hexdigest()
    assert privileged not in privileged_dto.model_dump_json()


def test_apply_manifest_승인_DTO는_민감값을_가리고_구조를_유지한다(monkeypatch, tmp_path):
    manifest = """apiVersion: v1
kind: List
items:
- apiVersion: v1
  kind: Secret
  metadata:
    name: credentials
    annotations:
      kubectl.kubernetes.io/last-applied-configuration: '{"token":"annotation-secret"}'
  data:
    token: c2VjcmV0
  stringData:
    password: hunter2
  binaryData:
    certificate: YmluYXJ5
- apiVersion: v1
  kind: Pod
  metadata:
    name: app
  spec:
    automountServiceAccountToken: false
    clientSecret: inline-client-secret
    containers:
    - name: app
      image: example/app:latest
      securityContext:
        privileged: true
      env:
      - name: DB_PASSWORD
        value: env-secret
      - name: LOG_LEVEL
        value: debug
      - name: DATABASE_URL
        value: postgresql://alice:hunter2@db/app
      - name: API_TOKEN
        valueFrom:
          secretKeyRef:
            name: credentials
            key: token
    imagePullSecrets:
    - name: registry-credentials
    volumes:
    - name: credentials
      secret:
        secretName: credentials
        optional: false
        defaultMode: 0400
- apiVersion: example.com/v1
  kind: AccessPolicy
  metadata:
    name: policy
    annotations:
      example.com/dsn: postgresql://carol:letmein@db/app
      example.com/endpoint: https://api.example.test/callback?access_token=query-secret
      example.com/signed: https://s3.example.test/object?X-Amz-Signature=signed-secret
      example.com/relative: //relative-user:relative-password@api.example.test/path
      example.com/jdbc: jdbc:postgresql://dave:jdbc-password@db/app
      example.com/malformed: https://broken-user:broken-password@[invalid
  spec:
    authorization:
      mode: RBAC
      value: nested-authorization-secret
      webhook:
        endpoint: https://auth.example.test/check
    passwordPolicy:
      minLength: 16
      requireSymbols: true
      value: nested-policy-secret
    tokenFile: /var/run/secrets/token
    tokenTTL: 3600
    tokenPaths:
    - path: /var/run/secrets/material
      material: list-secret
    credentials:
      value: nested-credential-secret
      DATABASE_URL: postgresql://alice:hunter2@db/app
      AWS_ACCESS_KEY_ID: AKIAEXAMPLE
      connectionString: mongodb://user:password@mongo/app
      neutralAccessKey: AKIAIOSFODNN7EXAMPLE
      neutralBearer: Bearer bearer-secret
      embeddedSecret: |
        apiVersion: v1
        kind: Secret
        data:
          password: embedded-secret
"""

    dto = _manifest_dto(monkeypatch, tmp_path, "sensitive", manifest)
    secret, pod, policy = dto.manifest_preview[0]["items"]

    assert secret["data"] == {"token": "<redacted>"}
    assert secret["stringData"] == {"password": "<redacted>"}
    assert secret["binaryData"] == {"certificate": "<redacted>"}
    assert secret["metadata"]["annotations"][
        "kubectl.kubernetes.io/last-applied-configuration"
    ] == "<redacted>"
    assert pod["spec"]["clientSecret"] == "<redacted>"
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["containers"][0]["env"][0] == {
        "name": "DB_PASSWORD",
        "value": "<redacted>",
    }
    assert pod["spec"]["containers"][0]["env"][1]["value"] == "<redacted>"
    assert pod["spec"]["containers"][0]["env"][2]["value"] == "<redacted>"
    assert pod["spec"]["containers"][0]["env"][3]["valueFrom"] == {
        "secretKeyRef": {"name": "credentials", "key": "token"}
    }
    assert pod["spec"]["containers"][0]["securityContext"] == {
        "privileged": True
    }
    assert pod["spec"]["imagePullSecrets"] == [{"name": "registry-credentials"}]
    assert pod["spec"]["volumes"][0]["secret"] == {
        "secretName": "credentials",
        "optional": False,
        "defaultMode": 256,
    }
    assert policy["metadata"]["annotations"] == {
        "example.com/dsn": "postgresql://<redacted>@db/app",
        "example.com/endpoint": (
            "https://api.example.test/callback?access_token=<redacted>"
        ),
        "example.com/signed": (
            "https://s3.example.test/object?X-Amz-Signature=<redacted>"
        ),
        "example.com/relative": "//<redacted>@api.example.test/path",
        "example.com/jdbc": "jdbc:postgresql://<redacted>@db/app",
        "example.com/malformed": "https://<redacted>@[invalid",
    }
    assert policy["spec"]["authorization"] == {
        "mode": "RBAC",
        "value": "<redacted>",
        "webhook": {"endpoint": "https://auth.example.test/check"},
    }
    assert policy["spec"]["passwordPolicy"] == {
        "minLength": 16,
        "requireSymbols": True,
        "value": "<redacted>",
    }
    assert policy["spec"]["tokenFile"] == "/var/run/secrets/token"
    assert policy["spec"]["tokenTTL"] == 3600
    assert policy["spec"]["tokenPaths"] == [
        {"path": "/var/run/secrets/material", "material": "<redacted>"}
    ]
    assert policy["spec"]["credentials"] == {
        "value": "<redacted>",
        "DATABASE_URL": "postgresql://<redacted>@db/app",
        "AWS_ACCESS_KEY_ID": "<redacted>",
        "connectionString": "mongodb://<redacted>@mongo/app",
        "neutralAccessKey": "<redacted>",
        "neutralBearer": "<redacted>",
        "embeddedSecret": "<redacted>",
    }

    serialized = dto.model_dump_json()
    for value in (
        "annotation-secret",
        "c2VjcmV0",
        "hunter2",
        "YmluYXJ5",
        "inline-client-secret",
        "env-secret",
        "postgresql://alice:hunter2@db/app",
        "carol",
        "letmein",
        "query-secret",
        "signed-secret",
        "relative-user",
        "relative-password",
        "nested-authorization-secret",
        "nested-credential-secret",
        "list-secret",
        "nested-policy-secret",
        "jdbc-password",
        "broken-user",
        "broken-password",
        "AKIAIOSFODNN7EXAMPLE",
        "bearer-secret",
        "embedded-secret",
        "AKIAEXAMPLE",
        "mongodb://user:password@mongo/app",
    ):
        assert value not in serialized


def test_apply_manifest_승인_DTO는_재귀별칭과_binary를_JSON으로_안전하게_표시한다(
    monkeypatch, tmp_path
):
    manifest = """apiVersion: v1
kind: ConfigMap
metadata:
  name: unusual
binaryData:
  archive: !!binary aGVsbG8=
data:
  recursive: &recursive
  - *recursive
  shared: &shared
    enabled: true
  repeated: *shared
  positiveInfinity: .inf
  negativeInfinity: -.inf
  notANumber: .nan
"""

    dto = _manifest_dto(monkeypatch, tmp_path, "unusual", manifest)

    assert dto.manifest_preview[0]["binaryData"]["archive"] == "<binary: 5 bytes>"
    assert dto.manifest_preview[0]["data"]["recursive"] == ["<recursive-reference>"]
    assert dto.manifest_preview[0]["data"]["shared"] == {"enabled": True}
    assert dto.manifest_preview[0]["data"]["repeated"] == "<shared-reference>"
    assert dto.manifest_preview[0]["data"]["positiveInfinity"] == "<non-finite: inf>"
    assert dto.manifest_preview[0]["data"]["negativeInfinity"] == "<non-finite: -inf>"
    assert dto.manifest_preview[0]["data"]["notANumber"] == "<non-finite: nan>"
    dto.model_dump_json()


def test_apply_manifest_승인_DTO는_지나치게_깊은_YAML을_거부한다(monkeypatch, tmp_path):
    nested = "value: leaf"
    for _ in range(120):
        nested = "child:\n" + "\n".join(f"  {line}" for line in nested.splitlines())
    indented = "\n".join(f"  {line}" for line in nested.splitlines())
    manifest = (
        "apiVersion: example.com/v1\n"
        "kind: DeepResource\n"
        "metadata: {name: deep}\n"
        f"spec:\n{indented}\n"
    )

    with pytest.raises(ValueError, match="manifest is too deeply nested"):
        _manifest_dto(monkeypatch, tmp_path, "deep", manifest)


@pytest.mark.parametrize("dry_run_status", ["succeeded", "unsupported"])
def test_Plan과_pending_call로_승인_DTO를_만든다(
    monkeypatch, tmp_path, dry_run_status
):
    plan = _ready_plan(monkeypatch, tmp_path, dry_run_status)
    call = ToolCallPart(
        tool_name="scale_resource",
        args=CALL_ARGS,
        tool_call_id="call-123",
    )

    dto = build_approval_request(
        call,
        {"plan_id": plan.id},
        default_namespace="study",
    )

    assert dto.tool_call_id == "call-123"
    assert dto.plan_id == plan.id
    assert dto.tool == "scale_resource"
    assert dto.risk == "caution"
    assert dto.command == plan.command
    assert dto.target == plan.target
    assert dto.dry_run_result["status"] == dry_run_status
    assert dto.decision_guidance == "현재 replica와 가용 자원을 확인한다."
    assert dto.manifest_preview is None
    assert dto.manifest_sha256 is None
    assert set(dto.model_dump()) == {
        "tool_call_id",
        "plan_id",
        "tool",
        "target",
        "command",
        "risk",
        "intent",
        "expected_effects",
        "side_effects",
        "dry_run_result",
        "decision_guidance",
    }
    assert ApprovalRequest.model_validate_json(dto.model_dump_json()) == dto


@pytest.mark.parametrize(
    ("tool", "args", "metadata"),
    [
        ("rollout_restart", CALL_ARGS, {"plan_id": "ap-unused"}),
        ("scale_resource", {**CALL_ARGS, "replicas": 4}, {"plan_id": "ap-unused"}),
        (
            "scale_resource",
            {**CALL_ARGS, "intent": "다른 작업을 수행한다."},
            {"plan_id": "ap-unused"},
        ),
        ("scale_resource", CALL_ARGS, {"plan_id": "wrong-plan"}),
    ],
)
def test_pending_tool_args_plan_id가_다르면_DTO를_거부한다(
    monkeypatch, tmp_path, tool, args, metadata
):
    plan = _ready_plan(monkeypatch, tmp_path)
    if metadata["plan_id"] == "ap-unused":
        metadata = {"plan_id": plan.id}
    call = ToolCallPart(tool_name=tool, args=args, tool_call_id="call-123")

    with pytest.raises(ValueError, match="pending approval mismatch"):
        build_approval_request(call, metadata, default_namespace="study")
