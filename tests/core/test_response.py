"""응답 조립 검증 — steps 는 코드가 실행 기록에서 채운다 (DURO-44).

TestModel 로 LLM 을 흉내 내고, kubectl 은 가짜 결과로 바꿔 돌린다.
핵심 보장: 화면에 보이는 command/output = 실제 실행된 KubectlResult 그대로, 개수·순서 = 실제 호출.
"""
import logging

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from kukie_agent import response as response_mod
from kukie_agent.agent import agent
from kukie_agent.deps import Deps
from kukie_agent.glossary import explain_command
from kukie_agent.kubectl import KubectlResult
from kukie_agent.response import build_response_for
from kukie_agent.skills import CAUSE_CATEGORIES, CAUSE_CATEGORY_GUIDE, PRIVATE, SHARED, Finding, KukieResponse
from kukie_agent.tools import read as read_tools

FAKE_COMMAND = "kubectl --context kind-dev get pods -n study -o wide"


@pytest.fixture
def fake_kubectl(monkeypatch):
    """읽기 툴의 kubectl 실행을 가짜 결과로 교체. 호출된 args 를 기록한다."""
    calls: list[list[str]] = []

    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        calls.append(args)
        return KubectlResult(command=FAKE_COMMAND, stdout="nginx-abc  1/1  Running",
                             stderr="", success=True)

    monkeypatch.setattr(read_tools, "run_kubectl", fake)
    return calls


def _deps(skill=PRIVATE) -> Deps:
    return Deps(context="kind-dev", namespace="study", skill=skill)


def _run(model, deps=None, **kw):
    with agent.override(model=model):
        return agent.run_sync("파드 보여줘", deps=deps or _deps(), **kw)


# ── 1. 사실 칸은 실행 기록 그대로 ───────────────────────────────

def test_steps는_실제_실행된_명령과_결과를_그대로_담는다(fake_kubectl):
    m = TestModel(call_tools=["list_resources"], custom_output_args={"finding": None, "narration": "파드 하나 떠 있어요"})
    result = _run(m)
    assert len(result.output.steps) == 1 == len(fake_kubectl)
    step = result.output.steps[0]
    assert step.command == FAKE_COMMAND            # LLM 이 옮겨 적은 게 아니라 원본
    assert step.output == "nginx-abc  1/1  Running"
    assert step.access == "read-only"
    assert step.step_label == "리소스 목록 조회"
    assert result.output.narration == "파드 하나 떠 있어요"   # 해석 칸만 LLM


def test_툴을_안_부르면_steps는_비어있다(fake_kubectl):
    m = TestModel(call_tools=[], custom_output_args={"finding": None, "narration": "개념 설명이에요"})
    result = _run(m)
    assert result.output.steps == []
    assert fake_kubectl == []


# ── 2. LLM 스키마와 다음 행동 안내 ─────────────────────────────

@pytest.mark.parametrize("skill", [PRIVATE, SHARED], ids=["Private", "Shared"])
@pytest.mark.parametrize("values", [
    {},
    {"suggested_next_action": None},
    {"suggested_next_action": "컨테이너 로그를 확인해 보세요."},
])
def test_두_스킬의_응답_스키마와_다음_행동_값(skill, values):
    """Private·Shared 는 같은 응답 스키마 — finding 은 null 을 허용하는 필수 칸이다 (DURO-100, 관문 R2)."""
    m = TestModel(call_tools=[], custom_output_args={"finding": None, "narration": "설명", **values})
    result = _run(m, deps=_deps(skill), output_type=skill.output_fn)
    schema = m.last_model_request_parameters.output_tools[0].parameters_json_schema
    assert set(schema["properties"]) == {"narration", "suggested_next_action", "finding"}
    assert set(schema["required"]) == {"narration", "finding"}       # 키는 꼭 보내고
    finding = schema["properties"]["finding"]
    assert {"type": "null"} in finding["anyOf"] and "default" not in finding   # 값은 null 이어도 된다
    assert "healthy" in finding["description"] and "지어내지 않는다" in finding["description"]
    assert result.output.finding is None
    field = schema["properties"]["suggested_next_action"]
    assert {item["type"] for item in field["anyOf"]} == {"string", "null"}
    assert field["default"] is None
    assert "suggested_next_action" not in schema.get("required", [])
    assert result.output.suggested_next_action == values.get("suggested_next_action")
    assert result.output.steps == []


# ── 3. explanations 는 사전에서 ─────────────────────────────────

def test_explanations는_사전에서_채워진다(fake_kubectl):
    m = TestModel(call_tools=["list_resources"], custom_output_args={"finding": None, "narration": "x"})
    result = _run(m)
    fields = {e.field for e in result.output.steps[0].explanations}
    assert {"--context kind-dev", "get", "-n study", "-o wide"} <= fields
    meanings = {e.field: e.meaning for e in result.output.steps[0].explanations}
    assert "네임스페이스" in meanings["-n study"]


def test_사전_미등록_플래그는_로그만_남기고_응답은_그대로_나간다(monkeypatch, caplog):
    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        return KubectlResult(command="kubectl --context kind-dev get pods --weird-flag -n study",
                             stdout="ok", stderr="", success=True)
    monkeypatch.setattr(read_tools, "run_kubectl", fake)
    m = TestModel(call_tools=["list_resources"], custom_output_args={"finding": None, "narration": "x"})
    with caplog.at_level(logging.WARNING, logger="kukie_agent.validators"):
        result = _run(m)
    assert len(result.output.steps) == 1                      # 반려 안 함
    assert any("미등록" in r.message and "--weird-flag" in r.message for r in caplog.records)


# ── 4. 이전 턴의 기록은 섞이지 않는다 ──────────────────────────

def test_collect_steps는_마지막_사용자_발화_이후의_호출만_담는다():
    """지난 턴의 ToolReturnPart 가 기록에 남아 있어도 이번 턴 steps 에 섞이지 않는다."""
    from types import SimpleNamespace
    from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart, ToolCallPart,
                                      ToolReturnPart, UserPromptPart)

    def kubectl(cmd):
        return KubectlResult(command=cmd, stdout="ok", stderr="", success=True)

    messages = [
        ModelRequest(parts=[UserPromptPart(content="지난 턴 질문")]),
        ModelResponse(parts=[ToolCallPart(tool_name="list_resources", args={}, tool_call_id="old")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="list_resources", content=kubectl("kubectl get pods"),
                                           tool_call_id="old")]),
        ModelResponse(parts=[TextPart(content="지난 턴 답")]),
        ModelRequest(parts=[UserPromptPart(content="이번 턴 질문")]),
        ModelResponse(parts=[ToolCallPart(tool_name="get_logs", args={}, tool_call_id="new")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="get_logs", content=kubectl("kubectl logs nginx"),
                                           tool_call_id="new")]),
    ]
    steps = response_mod.collect_steps(SimpleNamespace(messages=messages))
    assert [s.command for s in steps] == ["kubectl logs nginx"]


def test_이어지는_턴에서도_steps는_그_턴의_실행_기록과_일치한다(fake_kubectl):
    m = TestModel(call_tools=["list_resources"], custom_output_args={"finding": None, "narration": "x"})
    first = _run(m)
    calls_before = len(fake_kubectl)
    second = _run(m, message_history=first.all_messages())
    calls_in_second = len(fake_kubectl) - calls_before
    assert len(second.output.steps) == calls_in_second   # 지난 턴 것이 섞이면 이 등식이 깨진다


def test_승인_후_재개된_턴의_변경_실행은_mutating_블록으로_잡힌다():
    """2차 run: 새 사용자 발화 없이 이어지므로 1차 발화부터가 이번 턴 — 실행 기록이 steps 에 들어간다.
    dry-run 은 훅이 직접 실행한 것이라 콜 기록이 아니므로 블록이 되지 않는다."""
    from types import SimpleNamespace
    from pydantic_ai.messages import (ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart,
                                      UserPromptPart)

    executed = KubectlResult(command="kubectl --context kind-dev delete deployment nginx -n study",
                             stdout="deployment.apps/nginx deleted", stderr="", success=True)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="nginx 지워줘")]),           # 1차 run 시작
        ModelResponse(parts=[ToolCallPart(tool_name="delete_resource", args={}, tool_call_id="c1")]),
        # (1차: ApprovalRequired 로 멈춤 — 콜에 답이 없는 상태로 기록됨)
        # (2차: 재개 — 새 UserPromptPart 없이 실행 결과만 추가됨)
        ModelRequest(parts=[ToolReturnPart(tool_name="delete_resource", content=executed,
                                           tool_call_id="c1")]),
    ]
    steps = response_mod.collect_steps(SimpleNamespace(messages=messages))
    assert len(steps) == 1
    assert steps[0].access == "mutating"
    assert steps[0].step_label == "리소스 삭제"
    assert steps[0].command == executed.command
    assert {e.field for e in steps[0].explanations} >= {"delete", "-n study"}


def test_읽기_목록에_없는_툴은_mutating_으로_표시한다():
    """access 는 READ_TOOL_NAMES 기준이다 — 모르는 툴은 보수적으로 mutating.
    변경 툴 목록 등록을 빠뜨려도 화면에 read-only 로 안심시키지 않는다 (fail-closed)."""
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

    result = KubectlResult(command="kubectl --context kind-dev cordon node-1",
                           stdout="node/node-1 cordoned", stderr="", success=True)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="노드 격리해줘")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="cordon_node", content=result,
                                           tool_call_id="c1")]),   # 어느 목록에도 없는 툴
    ]
    steps = response_mod.collect_steps(SimpleNamespace(messages=messages))
    assert steps[0].access == "mutating"
    assert steps[0].step_label == "cordon_node"   # 라벨 미등록이면 툴 이름 그대로


def test_거부되거나_실패한_호출은_블록이_되지_않는다():
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

    messages = [
        ModelRequest(parts=[UserPromptPart(content="nginx 지워줘")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="delete_resource", content="사용자가 거절함",
                                           tool_call_id="c1")]),   # content 가 KubectlResult 가 아님
    ]
    assert response_mod.collect_steps(SimpleNamespace(messages=messages)) == []


# ── 5. finding 도 LLM 몫, steps 는 코드 몫 ───────────────────────

def test_finding은_LLM이_채우고_steps는_코드가_채운다(fake_kubectl):
    m = TestModel(call_tools=["list_resources"], custom_output_args={
        "narration": "원인 찾았어요",
        "finding": {"cause": "OOM", "evidence": "Exit 137", "recommendation": "limits 상향"},
    })
    result = _run(m, deps=_deps(PRIVATE), output_type=PRIVATE.output_fn)
    assert isinstance(result.output, KukieResponse)
    assert result.output.finding.cause == "OOM"
    assert result.output.steps[0].command == FAKE_COMMAND


def test_조립_함수는_응답_클래스마다_하나로_캐시된다():
    assert build_response_for(KukieResponse) is build_response_for(KukieResponse)
    assert build_response_for(KukieResponse) is PRIVATE.output_fn is SHARED.output_fn


def test_앱이_그리는_JSON_모양은_예전_진단_응답과_같다():
    """finding 칸 이름·모양이 바뀌면 앱의 원인 분석 블록이 사라진다. 새 칸은 더해질 뿐 옛 칸은 그대로다 (DURO-264)."""
    dumped = KukieResponse(narration="x", finding={"cause": "a", "evidence": "b", "recommendation": "c"}).model_dump()
    assert {k: dumped["finding"][k] for k in ("cause", "evidence", "recommendation")} == {
        "cause": "a", "evidence": "b", "recommendation": "c"}
    assert set(dumped) == {"narration", "steps", "suggested_next_action", "finding"}


# ── 5-1. 일치 채점용 구조 칸 (DURO-264) ──────────────────────────

FULL_FINDING = {
    "cause": "메모리 limit 이 너무 작아요",
    "evidence": "Last State: Terminated, Reason: OOMKilled",
    "recommendation": "limits.memory 를 올린다",
    "cause_object": {"kind": "Deployment", "namespace": "study", "name": "web"},
    "cause_category": "oom",
    "fix_commands": ["kubectl set resources deployment/web -n study --limits=memory=256Mi"],
}


def test_새_칸은_그대로_직렬화된다():
    dumped = KukieResponse(narration="x", finding=FULL_FINDING).model_dump()
    assert dumped["finding"] == FULL_FINDING


def test_클러스터_범위_원인은_namespace_가_없다():
    finding = Finding(cause="a", evidence="b", recommendation="c",
                      cause_object={"kind": "Node", "name": "worker-1"}, cause_category="unschedulable")
    assert finding.model_dump()["cause_object"] == {"kind": "Node", "namespace": None, "name": "worker-1"}


@pytest.mark.parametrize("blank", ["", "  "])
def test_빈_namespace_는_null_과_같다(blank):
    """모델이 null 대신 "" 를 보내도 채점에서 클러스터 범위로 읽혀야 한다."""
    finding = Finding(cause="a", evidence="b", recommendation="c",
                      cause_object={"kind": "Node", "namespace": blank, "name": "worker-1"})
    assert finding.cause_object.namespace is None


@pytest.mark.parametrize("bad", [
    {"cause_category": "OOMKilled"},          # 목록에 없는 값
    {"cause_category": "Oom"},                # 대소문자도 정확히
    {"cause_object": {"kind": "Pod"}},        # 이름 빠짐
    {"cause_object": {"kind": "", "name": "web"}},
    {"fix_commands": "kubectl delete pod web"},   # 목록이 아닌 문자열
])
def test_잘못된_값은_거부한다(bad):
    with pytest.raises(ValidationError):
        Finding(cause="a", evidence="b", recommendation="c", **bad)


def test_새_칸이_없는_예전_응답도_읽힌다():
    """저장된 기록·예전 클라이언트가 보낸 finding 은 세 칸뿐이다."""
    finding = KukieResponse.model_validate(
        {"narration": "x", "finding": {"cause": "a", "evidence": "b", "recommendation": "c"}}).finding
    assert finding.cause_object is None and finding.cause_category == "other" and finding.fix_commands == []


def test_분류_목록은_한_곳에서_정한다():
    assert CAUSE_CATEGORIES == (
        "bad-command", "image-not-found", "oom", "probe-misconfig", "selector-mismatch", "missing-config",
        "unschedulable", "quota", "rbac", "network-policy",
        "volume-permission", "port-conflict", "admission-rejected", "other", "healthy")
    assert set(Finding.model_json_schema()["properties"]["cause_category"]["enum"]) == set(CAUSE_CATEGORIES)


@pytest.mark.parametrize("category", ["volume-permission", "port-conflict", "admission-rejected"])
def test_DURO_268_분류도_받는다(category):
    assert Finding(cause="a", evidence="b", recommendation="c", cause_category=category).cause_category == category


def test_분류마다_설명이_스키마로_간다():
    """enum 값만 보내면 모델은 이름만 보고 고른다 — 뜻과 경계가 스키마 설명에 같이 가야 한다 (DURO-268)."""
    assert tuple(CAUSE_CATEGORY_GUIDE) == CAUSE_CATEGORIES
    description = Finding.model_json_schema()["properties"]["cause_category"]["description"]
    for name, text in CAUSE_CATEGORY_GUIDE.items():
        assert text.strip()
        assert f"- {name}: {text}" in description


@pytest.mark.parametrize("category,neighbors", [
    ("bad-command", {"missing-config"}),
    ("missing-config", {"bad-command", "volume-permission"}),
    ("admission-rejected", {"quota", "rbac"}),
    ("rbac", {"admission-rejected"}),
    ("quota", {"admission-rejected"}),
    ("volume-permission", {"missing-config"}),
    ("port-conflict", {"probe-misconfig"}),
    ("unschedulable", {"port-conflict"}),
])
def test_헷갈리는_분류는_경계를_적는다(category, neighbors):
    assert all(n in CAUSE_CATEGORY_GUIDE[category] for n in neighbors)


def test_LLM_스키마에서는_finding_의_모든_칸이_필수다(fake_kubectl):
    """모델이 새 칸을 빼먹지 않게 — 파이썬 기본값은 예전 기록 호환용일 뿐이다."""
    m = TestModel(call_tools=[], custom_output_args={"finding": None, "narration": "원인 찾았어요", "finding": FULL_FINDING})
    result = _run(m, deps=_deps(PRIVATE), output_type=PRIVATE.output_fn)
    schema = m.last_model_request_parameters.output_tools[0].parameters_json_schema
    finding_schema = schema["$defs"]["Finding"]
    assert set(finding_schema["required"]) == set(FULL_FINDING)
    assert "실행하지 않는다" in finding_schema["properties"]["fix_commands"]["description"]
    assert "admission-rejected: " in finding_schema["properties"]["cause_category"]["description"]
    assert result.output.finding.cause_category == "oom"
    assert result.output.finding.cause_object.name == "web"


# ── 6. 사전 파서 단위 ──────────────────────────────────────────

@pytest.mark.parametrize("command,expected_fields,expected_unknown", [
    ("kubectl --context c get events --sort-by=.lastTimestamp -n study",
     {"--context c", "get", "--sort-by=.lastTimestamp", "-n study"}, []),
    ("kubectl --context c logs nginx -n study --tail=100 -c app --previous",
     {"--context c", "logs", "-n study", "--tail=100", "-c app", "--previous"}, []),
    ("kubectl --context c delete deployment nginx -n study --dry-run=server -o yaml",
     {"--context c", "delete", "-n study", "--dry-run=server", "-o yaml"}, []),
    ("kubectl --context c get pods --nope -n study", {"--context c", "get", "-n study"}, ["--nope"]),
])
def test_explain_command_파서(command, expected_fields, expected_unknown):
    explanations, unknown = explain_command(command)
    assert {e.field for e in explanations} == expected_fields
    assert unknown == expected_unknown
    assert all(e.meaning for e in explanations)


# ── 7. finding 누락 막기 (관문 R2) ─────────────────────────────
# 큰 오픈 모델이 원인을 narration 에만 쓰고 finding 키를 빼던 실패. 키 누락은 형식 재시도, 진단 조사 뒤의 null 은 한 번 되묻는다.

from pydantic_ai import ModelResponse  # noqa: E402
from pydantic_ai.messages import RetryPromptPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

HEALTHY_FINDING = {
    "cause": "고장 없음", "evidence": "파드 1/1 Running, 경고 이벤트 없음", "recommendation": "할 일 없음",
    "cause_object": None, "cause_category": "healthy", "fix_commands": [],
}


class Script:
    """툴을 차례로 부른 뒤 answers 를 차례로 final 답으로 낸다. 모델이 받은 재시도 문구(retries)를 모은다."""

    def __init__(self, tools: list[tuple[str, dict]], answers: list[dict]):
        self.tools, self.answers = list(tools), list(answers)
        self.retries: list[str] = []
        self.calls = 0

    def __call__(self, messages, info):
        self.calls += 1
        self.retries += [p.content if isinstance(p.content, str) else str(p.content)
                         for p in messages[-1].parts if isinstance(p, RetryPromptPart)]
        if self.tools:
            name, args = self.tools.pop(0)
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=f"t{self.calls}")])
        args = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args,
                                                 tool_call_id=f"o{self.calls}")])


DESCRIBE = ("describe_resource", {"kind": "pod", "name": "web"})
CAUSE_IN_NARRATION = {"narration": "web 파드가 OOMKilled 로 죽어요. limit 을 올리세요."}


def _script_run(script, skill=PRIVATE):
    return _run(FunctionModel(script), deps=_deps(skill), output_type=skill.output_fn)


@pytest.mark.parametrize("skill", [PRIVATE, SHARED], ids=["Private", "Shared"])
def test_finding_키를_빼면_형식_재시도가_되고_다시_보낸_finding을_받는다(fake_kubectl, skill):
    script = Script([DESCRIBE], [CAUSE_IN_NARRATION, {**CAUSE_IN_NARRATION, "finding": FULL_FINDING}])
    result = _script_run(script, skill)
    assert result.output.finding.cause_category == "oom"
    assert len(script.retries) == 1 and "finding" in script.retries[0]


def test_진단_조사_뒤_finding이_null이면_한_번_되묻는다(fake_kubectl):
    script = Script([DESCRIBE], [{**CAUSE_IN_NARRATION, "finding": None},
                                 {**CAUSE_IN_NARRATION, "finding": FULL_FINDING}])
    result = _script_run(script)
    assert result.output.finding.cause == FULL_FINDING["cause"]
    assert len(script.retries) == 1 and script.retries[0].startswith(response_mod.FINDING_RETRY_MARK)
    assert "healthy" in script.retries[0] and "지어내지는 마세요" in script.retries[0]   # 억지 원인을 요구하지 않는다
    assert result.output.steps[0].command == FAKE_COMMAND


def test_키를_빼고_다시_null로_보내도_되묻기는_한_번이고_결국_null로_통과한다(fake_kubectl):
    """진단이 아니었다면 null 로 끝낼 수 있어야 한다 — 되묻기가 반복되면 형식 재시도 횟수를 다 써서 run 이 실패한다."""
    script = Script([DESCRIBE, ("get_logs", {"pod": "web"})],
                    [{"narration": "로그예요"}, {"narration": "로그예요", "finding": None}])
    result = _script_run(script)
    assert result.output.finding is None
    assert result.output.narration == "로그예요"
    assert sum(r.startswith(response_mod.FINDING_RETRY_MARK) for r in script.retries) == 1


@pytest.mark.parametrize("tools", [[], [("list_resources", {"kind": "pods"})], [("explain_command", {"resource_or_field": "pod"})]],
                         ids=["툴없음", "목록조회", "개념설명"])
def test_진단이_아닌_답은_finding_null로_바로_통과한다(fake_kubectl, tools):
    script = Script(tools, [{"narration": "설명이에요", "finding": None}])
    result = _script_run(script)
    assert result.output.finding is None and script.retries == []


def test_조사했는데_고장이_없으면_healthy_finding으로_바로_통과한다(fake_kubectl):
    script = Script([DESCRIBE, ("get_events", {})], [{"narration": "문제없어요", "finding": HEALTHY_FINDING}])
    result = _script_run(script)
    assert result.output.finding.cause_category == "healthy" and result.output.finding.cause_object is None
    assert script.retries == []


def _ctx(parts_per_request, retry=0, max_retries=2):
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    messages = [ModelRequest(parts=[UserPromptPart(content="왜 안 떠?")])]
    messages += [ModelRequest(parts=parts) for parts in parts_per_request]
    return SimpleNamespace(messages=messages, retry=retry, max_retries=max_retries)


def _ret(tool, content=None):
    from pydantic_ai.messages import ToolReturnPart
    content = content or KubectlResult(command=FAKE_COMMAND, stdout="x", stderr="", success=True)
    return ToolReturnPart(tool_name=tool, content=content, tool_call_id=f"c-{tool}")


@pytest.mark.parametrize("parts,retry,expected", [
    ([[_ret("describe_resource")]], 0, True),
    ([[_ret("get_logs")]], 1, True),
    ([[_ret("describe_resource")]], 2, False),                          # 형식 재시도 횟수를 다 썼다 — run 을 실패시키지 않는다
    ([[_ret("describe_resource", content="권한 없음")]], 0, False),       # 실패한 조회는 조사로 치지 않는다
    ([[_ret("describe_resource")], [_ret("scale_resource")]], 0, False),   # 변경이 있는 턴은 변경 결과를 말하는 답이다
    ([[_ret("describe_resource")],
      [RetryPromptPart(content=response_mod.FINDING_RETRY_MESSAGE, tool_name="final_result")]], 1, False),   # 이미 되물었다
], ids=["describe", "logs", "재시도소진", "실패한조회", "변경턴", "이미되물음"])
def test_되묻기_조건(parts, retry, expected):
    assert response_mod.needs_finding_retry(_ctx(parts, retry), None) is expected


def test_finding이_있으면_되묻지_않는다():
    assert response_mod.needs_finding_retry(_ctx([[_ret("describe_resource")]]), FULL_FINDING) is False
