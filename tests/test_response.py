"""응답 조립 검증 — steps 는 코드가 실행 기록에서 채운다 (DURO-44).

TestModel 로 LLM 을 흉내 내고, kubectl 은 가짜 결과로 바꿔 돌린다.
핵심 보장: 화면에 보이는 command/output = 실제 실행된 KubectlResult 그대로, 개수·순서 = 실제 호출.
"""
import logging

import pytest
from pydantic_ai.models.test import TestModel

from kukie import response as response_mod
from kukie.agent import agent
from kukie.deps import Deps
from kukie.glossary import explain_command
from kukie.kubectl import KubectlResult
from kukie.response import build_response_for
from kukie.skills import SKILLS
from kukie.skills.diagnosis import DiagnosisResponse
from kukie.tools import read as read_tools

FAKE_COMMAND = "kubectl --context kind-dev get pods -n study -o wide"


@pytest.fixture
def fake_kubectl(monkeypatch):
    """읽기 툴의 kubectl 실행을 가짜 결과로 교체. 호출된 args 를 기록한다."""
    calls: list[list[str]] = []

    def fake(args, *, context, dry_run=False, stdin=None, timeout=30):
        calls.append(args)
        return KubectlResult(command=FAKE_COMMAND, stdout="nginx-abc  1/1  Running",
                             stderr="", success=True)

    monkeypatch.setattr(read_tools, "run_kubectl", fake)
    return calls


def _deps(skill="학습") -> Deps:
    return Deps(context="kind-dev", namespace="study", skill=SKILLS[skill])


def _run(model, deps=None, **kw):
    with agent.override(model=model):
        return agent.run_sync("파드 보여줘", deps=deps or _deps(), **kw)


# ── 1. 사실 칸은 실행 기록 그대로 ───────────────────────────────

def test_steps는_실제_실행된_명령과_결과를_그대로_담는다(fake_kubectl):
    m = TestModel(call_tools=["list_resources"], custom_output_args={"narration": "파드 하나 떠 있어요"})
    result = _run(m)
    assert len(result.output.steps) == 1 == len(fake_kubectl)
    step = result.output.steps[0]
    assert step.command == FAKE_COMMAND            # LLM 이 옮겨 적은 게 아니라 원본
    assert step.output == "nginx-abc  1/1  Running"
    assert step.access == "read-only"
    assert step.step_label == "리소스 목록 조회"
    assert result.output.narration == "파드 하나 떠 있어요"   # 해석 칸만 LLM


def test_툴을_안_부르면_steps는_비어있다(fake_kubectl):
    m = TestModel(call_tools=[], custom_output_args={"narration": "개념 설명이에요"})
    result = _run(m)
    assert result.output.steps == []
    assert fake_kubectl == []


# ── 2. LLM 스키마에는 steps 가 없다 ─────────────────────────────

def test_LLM에게_보이는_출력_스키마에_steps가_없다(fake_kubectl):
    m = TestModel(call_tools=[], custom_output_args={"narration": "x"})
    _run(m)
    schema = m.last_model_request_parameters.output_tools[0].parameters_json_schema
    keys = set(schema["properties"])
    assert "steps" not in keys
    assert keys == {"narration", "suggested_transition"}


# ── 3. explanations 는 사전에서 ─────────────────────────────────

def test_explanations는_사전에서_채워진다(fake_kubectl):
    m = TestModel(call_tools=["list_resources"], custom_output_args={"narration": "x"})
    result = _run(m)
    fields = {e.field for e in result.output.steps[0].explanations}
    assert {"--context kind-dev", "get", "-n study", "-o wide"} <= fields
    meanings = {e.field: e.meaning for e in result.output.steps[0].explanations}
    assert "네임스페이스" in meanings["-n study"]


def test_사전_미등록_플래그는_로그만_남기고_응답은_그대로_나간다(monkeypatch, caplog):
    def fake(args, *, context, dry_run=False, stdin=None, timeout=30):
        return KubectlResult(command="kubectl --context kind-dev get pods --weird-flag -n study",
                             stdout="ok", stderr="", success=True)
    monkeypatch.setattr(read_tools, "run_kubectl", fake)
    m = TestModel(call_tools=["list_resources"], custom_output_args={"narration": "x"})
    with caplog.at_level(logging.WARNING, logger="kukie.validators"):
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
    m = TestModel(call_tools=["list_resources"], custom_output_args={"narration": "x"})
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


def test_거부되거나_실패한_호출은_블록이_되지_않는다():
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

    messages = [
        ModelRequest(parts=[UserPromptPart(content="nginx 지워줘")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="delete_resource", content="사용자가 거절함",
                                           tool_call_id="c1")]),   # content 가 KubectlResult 가 아님
    ]
    assert response_mod.collect_steps(SimpleNamespace(messages=messages)) == []


# ── 5. 스킬 특화 응답도 같은 방식 ──────────────────────────────

def test_스킬_특화_응답은_특화_필드만_LLM이_채우고_steps는_코드가_채운다(fake_kubectl):
    m = TestModel(call_tools=["list_resources"], custom_output_args={
        "narration": "원인 찾았어요",
        "finding": {"cause": "OOM", "evidence": "Exit 137", "recommendation": "limits 상향"},
    })
    result = _run(m, deps=_deps("진단"), output_type=SKILLS["진단"].output_fn)
    assert isinstance(result.output, DiagnosisResponse)
    assert result.output.finding.cause == "OOM"
    assert result.output.steps[0].command == FAKE_COMMAND


def test_조립_함수는_응답_클래스마다_하나로_캐시된다():
    assert build_response_for(DiagnosisResponse) is build_response_for(DiagnosisResponse)
    assert build_response_for(DiagnosisResponse) is SKILLS["진단"].output_fn


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
