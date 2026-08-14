"""가드레일 훅 — 변경 툴 호출이 트리거인 승인·기록 파이프라인.

팀 결정 (가드레일 v2):
- 위험도 판별 시스템 없음. 등급은 RISK_STICKERS(함수에 미리 붙인 스티커) 조회 한 줄.
- LLM 작성 필드 검증 단계 없음 (intent 등은 MVP 제외).
- Action Plan에는 코드가 아는 사실만 기록 (명령·대상·dry-run·승인·결과).

트리거: 변경 툴 4종의 호출 (LLM이 쓰려는 시도 자체).
LLM은 발동 여부에 관여할 수 없다. wrap 훅 하나가 전 단계를 감싼다.
"""
from __future__ import annotations

from pydantic_ai.capabilities.hooks import Hooks

from kukie.guardrail.action_plan import ActionPlan
from kukie.guardrail.approval import cli_approve
from kukie.kubectl import assemble, run_kubectl
from kukie.tools.mutate import MUTATING_TOOLS, RISK_STICKERS, Risk

hooks = Hooks()


@hooks.on.tool_execute(tools=sorted(MUTATING_TOOLS))
async def guardrail(ctx, *, call, tool_def, args, handler):
    """파이프라인 (6단계).

    ① 명령 조립   → assemble() 1벌 호출 (재조립 금지)
    ② 등급 조회   → RISK_STICKERS[툴이름]. 미등록이면 DESTRUCTIVE (fail-closed)
    ③ Plan 생성   → ActionPlan.create_draft() — 코드가 아는 사실만
    ④ dry-run    → 실패 시 draft 삭제 후 ToolFailed
    ⑤ CLI 승인   → CAUTION 1회 / DESTRUCTIVE 이중 (대상 이름 타이핑).
                   거절 시 plan.mark("rejected") + SkipToolExecution
    ⑥ 실행·기록  → handler(조립 args) → record_result() + mark(executed/failed)

    비고: kube-system 등 보호 네임스페이스 승격(if문 2줄)은 옵션으로 보류 —
    필요해지면 ② 옆에 추가한다 (팀 결정 대기).
    """
    raise NotImplementedError  # TODO
