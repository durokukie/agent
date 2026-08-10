"""가드레일 훅 — 8단계 파이프라인 (기능 2 §1.1).

트리거: 변경 툴 4종의 호출 (LLM이 쓰려는 시도 자체).
LLM은 발동 여부에 관여할 수 없다. wrap 훅 하나로 Pre(①~⑥)/실행(⑦)/Post(⑧)를 감싼다.

evaluate_action(사전 검토)은 이 파일의 ②③⑤ 단계 함수를 그대로 공유한다 —
별도 판정 로직을 만들면 "검토 결과 ≠ 실제 판정" 불일치가 생긴다 (금지).
"""
from __future__ import annotations

from pydantic_ai.capabilities.hooks import Hooks

from kukie.guardrail.action_plan import ActionPlan
from kukie.guardrail.approval import cli_approve
from kukie.guardrail.rules import Risk, RuleEngine
from kukie.kubectl import assemble, run_kubectl
from kukie.tools.mutate import MUTATING_TOOLS

hooks = Hooks()
rule_engine = RuleEngine()


def validate_plan_fields(args: dict) -> str | None:
    """① 입력 검증 — 기계적 최저선: 빈 값 / 최소 길이 / 상투어 / 인자 동어반복.

    '성의'의 완전한 판정은 불가 — 최종 판단은 승인 화면의 사용자 몫.
    반환: 반려 사유 (통과면 None)
    """
    raise NotImplementedError  # TODO


@hooks.on.tool_execute(tools=sorted(MUTATING_TOOLS))
async def guardrail(ctx, *, call, tool_def, args, handler):
    """파이프라인 본체.

    ① 입력 검증     → 부실하면 ModelRetry (재작성 요구)
    ② 명령 조립     → assemble() 1벌 호출 (재조립 금지)
    ③ 룰 판정       → rule_engine.classify() — 결정론, fail-closed
    ④ Plan 생성     → ActionPlan.create_draft()
    ⑤ dry-run       → 실패 시 draft 정리 후 ToolFailed
    ⑥ CLI 승인      → safe 생략 / caution 1회 / destructive 이중.
                      거절 시 plan.mark("rejected") + SkipToolExecution
    ⑦ 실행          → handler(조립된 args 전달)
    ⑧ 기록          → plan.record_result() + mark(executed/failed)
    """
    raise NotImplementedError  # TODO
