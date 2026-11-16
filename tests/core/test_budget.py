"""요청 예산과 강제 답 (budget.py) — 요청 N번째에 도구를 빼고 답 양식만 준다. 기본은 꺼짐."""
from __future__ import annotations

import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import UsageLimits

from kukie_agent import budget
from kukie_agent.agent import agent
from kukie_agent.budget import FORCED_ANSWER_PROMPT, ForcedAnswerFailed, RequestBudget
from kukie_agent.deps import Deps
from kukie_agent.kubectl import KubectlResult
from kukie_agent.skills import PRIVATE
from kukie_agent.tools import read as read_tools

FINDING = {
    "cause": "메모리 limit 이 너무 작아요", "evidence": "OOMKilled", "recommendation": "limits.memory 를 올린다",
    "cause_object": {"kind": "Deployment", "namespace": "study", "name": "web"},
    "cause_category": "oom", "fix_commands": [],
}
ANSWER = {"narration": "web 이 OOMKilled 예요", "suggested_next_action": None, "finding": FINDING}
DESCRIBE = ("describe_resource", {"kind": "pod", "name": "web"})


@pytest.fixture(autouse=True)
def fake_kubectl(monkeypatch):
    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        return KubectlResult(command="kubectl describe pod web", stdout="OOMKilled", stderr="", success=True)

    monkeypatch.setattr(read_tools, "run_kubectl", fake)


class Loop:
    """답을 내지 않고 조회만 되풀이하는 모델. 함수 도구가 없어진 요청에서는 forced 답을 낸다.
    요청마다 받은 함수 도구 이름과 마지막 요청에 붙은 사용자 문장을 모은다."""

    def __init__(self, forced: list[dict] | None = None, call_tool_when_forced: bool = False):
        self.forced = list(forced or [ANSWER])
        self.call_tool_when_forced = call_tool_when_forced
        self.tools_seen: list[list[str]] = []
        self.user_texts: list[list[str]] = []

    def __call__(self, messages, info):
        n = len(self.tools_seen) + 1
        self.tools_seen.append(sorted(t.name for t in info.function_tools))
        self.user_texts.append([p.content for p in messages[-1].parts if isinstance(p, UserPromptPart)])
        if info.function_tools or self.call_tool_when_forced:
            name, args = DESCRIBE
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=f"t{n}")])
        args = self.forced.pop(0) if len(self.forced) > 1 else self.forced[0]
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args, tool_call_id=f"o{n}")])


def _run(model, *, n: int | None, limit: int = 50):
    deps = Deps(context="kind-dev", namespace="study", skill=PRIVATE)
    with agent.override(model=FunctionModel(model)):
        return agent.run_sync("web 파드가 왜 죽어?", deps=deps, output_type=PRIVATE.output_fn,
                              usage_limits=UsageLimits(request_limit=limit),
                              **({"capabilities": [RequestBudget(n)]} if n is not None else {}))


def test_예산이_없으면_지금처럼_상한에_걸린다():
    loop = Loop()
    with pytest.raises(UsageLimitExceeded):
        _run(loop, n=None, limit=5)
    assert all(tools for tools in loop.tools_seen)            # 도구가 빠진 요청이 없다
    assert not any(FORCED_ANSWER_PROMPT in t for t in loop.user_texts)


def test_N번째_요청은_도구가_없고_강제_답_문장이_붙는다():
    loop = Loop()
    result = _run(loop, n=4)
    assert len(loop.tools_seen) == 4
    assert all(loop.tools_seen[:3]) and loop.tools_seen[3] == []
    assert loop.user_texts[3] == [FORCED_ANSWER_PROMPT] and not any(FORCED_ANSWER_PROMPT in t for t in loop.user_texts[:3])
    assert result.output.finding.cause_category == "oom"
    # 강제 답 문장은 새 질문이 아니다 — 화면 블록(steps)에 앞선 조회 3번이 그대로 남는다
    assert len(result.output.steps) == 3
    # 대화 기록에도 남는다 (평가 harness 가 messages.json 으로 저장한다)
    forced = [m for m in result.all_messages() if budget.is_forced_request(m)]
    assert len(forced) == 1 and any(isinstance(p, ToolReturnPart) for p in forced[0].parts)


def test_예산_전에_답하면_아무것도_바뀌지_않는다():
    calls = []

    def model(messages, info):
        calls.append(sorted(t.name for t in info.function_tools))
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name=DESCRIBE[0], args=DESCRIBE[1], tool_call_id="t1")])
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=ANSWER, tool_call_id="o2")])

    result = _run(model, n=4)
    assert len(calls) == 2 and all(calls)
    assert not any(budget.is_forced_request(m) for m in result.all_messages())


def test_강제_답_턴에서_finding이_null이면_실패로_끝난다():
    loop = Loop(forced=[{**ANSWER, "finding": None}])
    with pytest.raises(ForcedAnswerFailed):
        _run(loop, n=3)
    assert len(loop.tools_seen) == 3                            # 되묻는 요청을 더 보내지 않는다


def test_강제_답_턴에서_양식이_틀리면_상한보다_먼저_실패로_끝난다():
    loop = Loop(forced=[{"narration": "답"}])                   # finding 키 빠짐 → 형식 재시도가 필요
    with pytest.raises(ForcedAnswerFailed):
        _run(loop, n=3, limit=3)                                # 요청 상한이 N 과 같아도 ForcedAnswerFailed 가 먼저
    assert len(loop.tools_seen) == 3


def test_강제_답_턴에서_없는_도구를_불러도_실패로_끝난다():
    loop = Loop(call_tool_when_forced=True)
    with pytest.raises(ForcedAnswerFailed):
        _run(loop, n=3)
    assert len(loop.tools_seen) == 3


def test_사용자가_같은_문장을_새_질문으로_보내면_강제_답이_아니다():
    assert not budget.is_forced_request(ModelRequest(parts=[UserPromptPart(content=FORCED_ANSWER_PROMPT)]))
    assert budget.is_forced_request(ModelRequest(parts=[
        ToolReturnPart(tool_name="get_events", content="x", tool_call_id="t1"),
        UserPromptPart(content=FORCED_ANSWER_PROMPT)]))


@pytest.mark.parametrize("n", [0, 1, 51])
def test_예산_범위(n):
    with pytest.raises(ValueError):
        RequestBudget(n)


def test_환경변수가_비면_꺼짐(monkeypatch):
    monkeypatch.delenv(budget.ENV, raising=False)
    assert budget.from_env() == []
    monkeypatch.setenv(budget.ENV, " ")
    assert budget.from_env() == []
    monkeypatch.setenv(budget.ENV, "30")
    assert budget.from_env() == [RequestBudget(30)]
    monkeypatch.setenv(budget.ENV, "abc")
    with pytest.raises(ValueError):
        budget.from_env()
