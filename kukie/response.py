"""응답 조립 — 사실 칸은 코드가, 해석 칸은 LLM이 (DURO-44).

KukieResponse.steps[].command / output / access / explanations 는 LLM이 옮겨 적지 않는다.
run 동안 실제로 실행된 툴 호출 기록(ctx.messages 의 ToolReturnPart → KubectlResult)에서
코드가 그대로 꺼내 채운다. 따라서 "화면에 보이는 명령 = 실제 실행된 명령"이
LLM의 성실함이 아니라 코드로 보장된다.

구현 방식: pydantic-ai 의 함수 output_type.
  - LLM에게 보이는 스키마 = 함수의 매개변수뿐 (narration, suggested_transition, 스킬 특화 필드)
  - steps 는 매개변수에 없으므로 LLM은 그 칸의 존재조차 모른다
  - 함수 본문이 steps 를 채워 완성된 응답 객체를 돌려준다
스킬별 특화 응답(DiagnosisResponse 등)은 build_response_for(응답클래스)로 같은 방식의
함수를 만든다 — 응답 클래스의 필드 중 steps 만 빼고 전부 LLM 매개변수가 된다.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart

from kukie.deps import Deps
from kukie.glossary import explain_command
from kukie.kubectl import KubectlResult
from kukie.skills.base import KukieResponse, ToolStep
from kukie.tools.mutate import MUTATING_TOOLS
from kukie.validators import log_unregistered_flags

# 화면 블록 제목 — 툴 이름을 사람 말로. 미등록 툴은 이름 그대로.
STEP_LABELS: dict[str, str] = {
    "list_resources": "리소스 목록 조회",
    "describe_resource": "리소스 상세 조회",
    "get_events": "이벤트 조회",
    "get_logs": "로그 조회",
    "explain_command": "스키마 설명 조회",
    "apply_manifest": "매니페스트 적용",
    "scale_resource": "레플리카 조정",
    "rollout_restart": "재시작",
    "delete_resource": "리소스 삭제",
}


def _current_turn(messages: Sequence[ModelMessage]) -> Sequence[ModelMessage]:
    """이번 턴의 메시지만 — 마지막 사용자 발화부터. 이전 턴의 툴 기록이 steps 에 섞이지 않게."""
    start = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, ModelRequest) and any(isinstance(p, UserPromptPart) for p in msg.parts):
            start = i
    return messages[start:]


def collect_steps(ctx: RunContext[Deps]) -> list[ToolStep]:
    """이번 턴에 실제 실행된 kubectl 툴 호출을 순서대로 ToolStep 으로 변환한다."""
    steps: list[ToolStep] = []
    for msg in _current_turn(ctx.messages):
        for part in getattr(msg, "parts", ()):
            if not isinstance(part, ToolReturnPart) or not isinstance(part.content, KubectlResult):
                continue   # 거부·실패로 끝난 호출, kubectl 이 아닌 툴은 블록으로 만들지 않는다
            result = part.content
            explanations, unknown = explain_command(result.command)
            log_unregistered_flags(part.tool_name, result.command, unknown)
            steps.append(ToolStep(
                step_label=STEP_LABELS.get(part.tool_name, part.tool_name),
                access="mutating" if part.tool_name in MUTATING_TOOLS else "read-only",
                command=result.command,
                output=result.stdout if result.success else (result.stderr or result.stdout),
                explanations=explanations,
            ))
    return steps


_cache: dict[type[KukieResponse], Callable[..., KukieResponse]] = {}


def build_response_for(response_cls: type[KukieResponse]) -> Callable[..., KukieResponse]:
    """응답 클래스 → pydantic-ai output_type 으로 꽂을 조립 함수.

    매개변수 = 응답 클래스의 필드 중 steps 를 뺀 전부 (LLM 몫).
    본문     = steps 를 코드로 채워 response_cls 인스턴스 반환.
    """
    if response_cls in _cache:
        return _cache[response_cls]

    llm_fields = {name: f for name, f in response_cls.model_fields.items() if name != "steps"}

    def build(ctx: RunContext[Deps], **llm_values: Any) -> KukieResponse:
        return response_cls(steps=collect_steps(ctx), **llm_values)

    # pydantic-ai 는 함수 시그니처에서 LLM 스키마를 만든다 — 여기서 steps 가 빠진 시그니처를 박는다.
    params = [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                annotation=RunContext[Deps])]
    annotations: dict[str, Any] = {"ctx": RunContext[Deps], "return": response_cls}
    for name, f in llm_fields.items():
        default = inspect.Parameter.empty if f.is_required() else f.default
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                                        annotation=f.annotation, default=default))
        annotations[name] = f.annotation
    build.__signature__ = inspect.Signature(params, return_annotation=response_cls)  # type: ignore[attr-defined]
    build.__annotations__ = annotations
    build.__name__ = f"build_{response_cls.__name__}"
    build.__doc__ = (response_cls.__doc__ or "") + "\n(steps 는 시스템이 실행 기록에서 채운다.)"

    _cache[response_cls] = build
    return build


# 기본 응답 조립기 — Agent(output_type=build_response)
build_response = build_response_for(KukieResponse)
