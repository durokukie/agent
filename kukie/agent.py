"""에이전트 조립 — 프롬프트·툴·훅·검증기를 한곳에서 연결.

에이전트는 하나. 스킬(deps.skill)에 따라 프롬프트·툴·응답 스키마가 갈아끼워진다.
"""
from __future__ import annotations

import logging
import os
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import Provider, infer_provider, infer_provider_class
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from pydantic_ai.tools import DeferredToolRequests, ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from tenacity import retry_if_exception, stop_after_attempt

# pydantic-ai 2.4x 부터 공급자 SDK(anthropic 1.x 등)가 httpx 대신 httpx2 클라이언트를 받는다.
# 2.3x 는 httpx 만 받는다. 어느 쪽인지는 설치된 버전이 정하므로 둘 다 준비해 두고 맞는 쪽을 쓴다.
try:
    import httpx2
    from pydantic_ai.retries import AsyncHTTPX2TenacityTransport
except ImportError:                          # pydantic-ai 2.3x — httpx2 전송층 없음
    httpx2 = None                            # type: ignore[assignment]
    AsyncHTTPX2TenacityTransport = None      # type: ignore[assignment,misc]

from kukie.deps import Deps
from kukie.guardrail.hook import hooks as guardrail_hooks
from kukie.response import build_response
from kukie.tools.mutate import MUTATE_TOOLS
from kukie.tools.read import READ_TOOLS

logger = logging.getLogger(__name__)

BASE_PROMPT = """너는 쿠버네티스를 처음 배우는 연수생을 돕는 조수 Kukie다.
1. 실행한 명령·결과·플래그 설명은 시스템이 자동으로 화면에 붙인다 — 너는 narration에서
   그 결과가 무엇을 뜻하는지 연수생 눈높이로 풀어 설명해라. 명령어를 다시 옮겨 적지 마라.
2. '무엇을' 하는지와 '왜' 하는지를 항상 함께 설명한다.
3. 제공된 툴로 할 수 없는 작업은 실행하려 하지 말고, 사용자가 직접 터미널에
   실행할 kubectl 명령을 만들어 안내하고 각 플래그의 의미를 설명해라.
4. 클러스터를 변경하는 툴을 호출하면 시스템 가드레일이 자동 개입한다.
   위험도는 시스템이 판정한다 — 네가 판정하거나 우회하거나 실행됐다고 말하지 마라.
   승인은 Electron 승인 화면에서 사용자가 직접 결정해야 성립한다.
   채팅 메시지는 승인으로 해석하지 마라."""

# ── 툴 등록 ──────────────────────────────────────────────────
_toolset: FunctionToolset[Deps] = FunctionToolset([*READ_TOOLS, *MUTATE_TOOLS])


def _only_skill_tools(ctx: RunContext[Deps], tool_def: ToolDefinition) -> bool:
    """현재 스킬(deps.skill)에 허용된 툴만 LLM에게 노출한다 (스킬 = 프롬프트+툴+응답형식)."""
    return tool_def.name in ctx.deps.skill.allowed_tools


toolset = _toolset.filtered(_only_skill_tools)


# 모델은 환경변수로 지정한다. 미지정 시 'test'(TestModel) — 키 없이 import·테스트 가능.
#   예: KUKIE_MODEL=anthropic:claude-sonnet-4-6  (ANTHROPIC_API_KEY 필요)
# TODO: Model Adapter로 Upstage 등 교체 경계 정리 (Architecture.md 6.5)
MODEL = os.environ.get("KUKIE_MODEL", "test")


# ── 모델 HTTP 재시도 (DURO-66 ④) ──────────────────────────────
# 네트워크 끊김·타임아웃·429·5xx 는 서버(/approve)까지 올라오기 전에 모델 클라이언트 안에서
# 재시도한다. 여기서 풀리면 사용자는 "다시 시도" 를 누를 일이 없다. 4xx(잘못된 키·요청)는
# 다시 보내도 같은 답이므로 재시도하지 않는다.
_HTTP_LIBS = tuple(lib for lib in (httpx2, httpx) if lib is not None)
_STATUS_ERRORS = tuple(lib.HTTPStatusError for lib in _HTTP_LIBS)
_TRANSIENT_ERRORS = tuple(err for lib in _HTTP_LIBS for err in (lib.TimeoutException, lib.NetworkError))


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, _STATUS_ERRORS):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, _TRANSIENT_ERRORS)


def _retry_config() -> RetryConfig:
    return RetryConfig(
        retry=retry_if_exception(_retryable),
        wait=wait_retry_after(),              # 429 의 Retry-After 를 지키고, 없으면 지수 대기
        stop=stop_after_attempt(3),
        reraise=True,                         # 다 실패하면 tenacity RetryError 가 아니라 원래 예외
    )


def _retry_clients() -> Iterator[Any]:
    """공급자에 넘겨볼 클라이언트 후보 — httpx2(새 SDK) 먼저, 그다음 httpx(옛 SDK).

    둘 다 시간 제한을 600초/접속 5초로 — httpx 기본 5초는 LLM 응답에 너무 짧다.
    맞지 않는 쪽은 공급자가 TypeError 로 거절하므로 순서대로 시도하면 된다.
    """
    if httpx2 is not None and AsyncHTTPX2TenacityTransport is not None:
        yield httpx2.AsyncClient(
            transport=AsyncHTTPX2TenacityTransport(
                config=_retry_config(),
                validate_response=lambda response: response.raise_for_status(),
            ),
            timeout=httpx2.Timeout(600, connect=5),
        )
    with warnings.catch_warnings():           # 2.4x 에선 deprecated — 옛 SDK 일 때만 여기까지 온다
        warnings.simplefilter("ignore", DeprecationWarning)
        transport = AsyncTenacityTransport(
            config=_retry_config(),
            validate_response=lambda response: response.raise_for_status(),
        )
    yield httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600, connect=5))


def _provider_with_retry(name: str) -> Provider:
    """infer_model 의 provider_factory — 기본 공급자에 재시도 전송층만 끼운 것."""
    provider_cls = infer_provider_class(name)
    for client in _retry_clients():
        try:
            return provider_cls(http_client=client)
        except TypeError:                     # 이 클라이언트 종류는 안 받음 — 다음 후보
            continue
    logger.warning("공급자 %s 는 http_client 를 받지 않아 HTTP 재시도 없이 진행한다", name)
    return infer_provider(name)


def _build_model(spec: str) -> str | Model:
    """KUKIE_MODEL 문자열 → 모델. 'test' 나 공급자 접두어가 없으면 문자열 그대로 (pydantic-ai 가 해석)."""
    if spec == "test" or ":" not in spec:
        return spec
    return infer_model(spec, provider_factory=_provider_with_retry)


agent = Agent(
    _build_model(MODEL),
    name="kukie",
    deps_type=Deps,
    output_type=[build_response, DeferredToolRequests],  # 일반 응답 또는 승인 대기 요청
                                      # 스킬 특화 응답은 run마다 output_type=skill.output_fn 으로 오버라이드
    retries={"output": 2},            # 응답 형식 실패는 run 안에서 두 번까지 흡수 (DURO-66 ③). 툴은 기본 1
    instructions=BASE_PROMPT,
    toolsets=[toolset],               # 스킬 필터를 거친 툴 목록
    capabilities=[guardrail_hooks],   # 가드레일 훅 장착
)


# ── 동적 프롬프트 (등록형 함수) ────────────────────────────────
# 아래 두 함수는 코드 어디에서도 직접 호출하지 않는다. 그런데도 매 run마다 실행된다.
#
# 이유는 `@agent.instructions` 데코레이터 때문이다.
#   @agent.instructions
#   def f(ctx): ...
# 는 사실
#   def f(ctx): ...
#   f = agent.instructions(f)
# 와 같다. 즉 정의 직후 f를 agent.instructions()에 넘겨서 "이 함수를 프롬프트 생성기로
# 등록해라"라고 알려주는 것. 그 뒤로는 pydantic-ai가 run을 시작할 때마다 등록된 함수를
# 전부 호출해 반환 문자열을 BASE_PROMPT 뒤에 이어 붙인다 (agent-flow.md 그림1 ①).
#
# 그래서 grep으로 호출부를 찾으면 안 나오지만 "미사용"이 아니다 — 지우면 그 프롬프트가
# 조용히 사라진다. add_skill_prompt를 지우면 학습/진단/실습이 전부 똑같이 행동한다.
#
# 이 파일에서 같은 원리로 동작하는 등록형 함수:
#   @agent.instructions      → 프롬프트 생성기 (아래 둘)
#   FunctionToolset(...)     → 툴 (READ_TOOLS/MUTATE_TOOLS의 함수들; LLM이 이름으로 호출)
#   @hooks.on.tool_execute   → 훅 (guardrail/hook.py의 guardrail())
#   output_type=build_response → 최종 응답 조립기 (response.py). 함수라서 LLM 스키마는
#     매개변수(narration, suggested_transition)뿐이고, steps는 본문에서 코드가 실행 기록으로 채운다.
#     사전에 없는 플래그는 validators.log_unregistered_flags가 로그만 남긴다 (반려 없음).
#
# 함수형(문자열 대신 함수)으로 두는 이유: BASE_PROMPT는 고정값이라 문자열로 충분하지만,
# 아래 둘은 run마다 달라지는 값(현재 대상, 현재 스킬)을 ctx.deps에서 읽어야 하므로
# 실행 시점에 계산돼야 한다.

@agent.instructions
def add_target(ctx: RunContext[Deps]) -> str:
    """현재 작업 대상 주입 — 대화만으로 대상을 바꾸지 않는다.

    매 run 시작 시 pydantic-ai가 자동 호출. 반환값이 시스템 프롬프트에 추가된다.
    """
    return f"현재 작업 대상: context={ctx.deps.context}, namespace={ctx.deps.namespace}"


@agent.instructions
def add_skill_prompt(ctx: RunContext[Deps]) -> str:
    """현재 스킬의 전용 프롬프트 주입 (스킬 = 프롬프트+툴+응답형식).

    매 run 시작 시 pydantic-ai가 자동 호출. deps.skill이 학습이면 학습 프롬프트,
    진단이면 진단 프롬프트가 붙는다 — 이게 "에이전트 하나로 모드를 갈아끼우는" 장치.
    """
    return ctx.deps.skill.prompt
