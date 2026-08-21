"""가드레일 훅 — 변경 툴 호출이 트리거인 승인·기록 파이프라인.

팀 결정 (가드레일 v2):
- 위험도 판별 시스템 없음. 등급은 RISK_STICKERS(함수에 미리 붙인 스티커) 조회 한 줄.
- LLM이 호출하는 Action Plan 툴(조회/평가)은 MVP 제외 —
  Plan은 이 훅이 내부적으로 생성·기록한다.
- intent/expected_effects/side_effects는 변경 툴의 필수 인자 (스키마가 강제) —
  승인 화면과 Plan 본문에 실린다.

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
    ③ Plan 생성   → ActionPlan.create_draft() — 사실(frontmatter) + intent 등("왜" 본문)
                   intent가 빈 문자열이면 ModelRetry로 재작성 요구 (한 줄 검사)
    ④ dry-run    → 결과 기록. 실패 시 plan.mark("failed") 후 ToolFailed
                   실패한 Plan도 삭제하지 않고 히스토리로 보관
    ⑤ CLI 승인   → 명령·대상·intent·영향·부작용·등급 표시.
                   CAUTION 1회 / DESTRUCTIVE 이중 (대상 이름 타이핑).
                   거절 시 plan.mark("rejected") + SkipToolExecution
    ⑥ 실행·기록  → handler(조립 args) → record_result() + mark(executed/failed)

    비고: kube-system 등 보호 네임스페이스 승격(if문 2줄)은 옵션으로 보류 —
    필요해지면 ② 옆에 추가한다 (팀 결정 대기).
    """
    raise NotImplementedError  # TODO
