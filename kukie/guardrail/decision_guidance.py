"""승인 전 Action Plan에서 사용자의 추가 판단 항목을 생성한다.

**매니페스트 본문도 함께 넘긴다** (#50). Action Plan 에는 본문 대신 manifest_sha256 만 남으므로
(canonicalize_mutation_args), Plan 만 주면 모델이 "본문이 제시되지 않았다" 고 정직하게 답한다.
그런데 승인 카드 미리보기에는 본문이 그대로 보여서 **한 카드가 자기 모순**을 일으켰다.

승인 화면과 **같은 마스킹 함수**(_manifest_preview)를 거친 값을 준다. 보안 때문이 아니라
일관성 때문이다 — 본문은 애초에 모델이 쓴 것이라 다시 줘도 새로 새는 것이 없지만, 사용자가
보는 것과 모델이 보는 것이 다르면 화면에 없는 값을 근거로 조언하게 된다.
"""
from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from pydantic_ai import Agent

from kukie.guardrail.action_plan import ActionPlan


INSTRUCTIONS = """너는 아직 승인되지 않은 Kubernetes Action Plan의 검토자다.
사용자가 승인 또는 거절하기 전에 추가로 확인하거나 판단해야 할 사항만 작성한다.

규칙:
- 주어진 Action Plan과 매니페스트 미리보기만 근거로 사용한다.
- 미리보기가 있으면 그 설정값(이미지·리소스·포트 등)을 직접 보고 판단한다.
  "본문이 없다"거나 "manifest_sha256에 해당하는 실제 설정을 확인해야 한다"고 쓰지 않는다.
- 미리보기의 `<redacted>` 는 민감값이라 가린 것이다. 그 값 자체는 확인 대상이 아니고,
  필요하면 "가려진 값이 의도한 것인지" 정도만 짚는다.
- 미리보기가 없는 작업(scale·restart·delete)은 Action Plan의 대상과 명령으로 판단한다.
- 현재 클러스터 상태는 주어지지 않았다 — 실행 시점 상태와 다를 수 있다는 점은 짚어도 된다.
- 승인 또는 거절 결론을 대신 내리지 않는다.
- 위험 등급을 다시 판정하거나 변경하지 않는다.
- Intent, Expected Effects, Side Effects를 단순 반복하지 않는다.
- 대상의 적절성, 영향 범위, 복구 가능성, 실행 시점, 확인할 전제 중 필요한 것만 쓴다.
- 최대 3개의 짧은 항목으로 작성한다.
- 추가 판단이 필요하지 않으면 '추가 판단 없음'이라고 작성한다.
- 설명문만 반환한다."""

# 판단 가이드 모델도 환경변수로 — 메인 에이전트(KUKIE_MODEL)와 다른 제공사에 묶여 있으면
# 키를 두 벌 요구하게 된다. 실패해도 훅이 "guidance unavailable"로 삼키므로 터지지 않고
# 조용히 빈칸이 되는 자리라, 설정 가능해야 한다 (예: openrouter:openai/gpt-5-mini).
# `or` 인 이유: .env 의 `KUKIE_GUIDANCE_MODEL=` 처럼 빈 값도 환경변수로 등록되는데,
# get(키, 기본값)은 그때 빈 문자열을 돌려줘 Agent("") 가 import 시점에 UserError 로
# 터진다 (CodeRabbit 지적). 빈 값을 미설정과 같게 취급한다 — observability 와 같은 패턴.
def _guidance_model() -> str:
    return os.environ.get("KUKIE_GUIDANCE_MODEL") or "openai:gpt-5.6-luna"


GUIDANCE_MODEL = _guidance_model()

guidance_agent = Agent(
    GUIDANCE_MODEL,
    name="action-plan-decision-guidance",
    output_type=str,
    instructions=INSTRUCTIONS,
    model_settings={"openai_reasoning_effort": "medium"},
    defer_model_check=True,
)


def _json_safe(value: Any) -> Any:
    """json.dumps 가 모르는 값을 문자열로 바꾼다.

    PyYAML 은 따옴표 없는 날짜를 `datetime.date` 로, `!!set` 을 `set` 으로 만드는데
    _safe_scalar 는 bytes·float 만 손대므로 그대로 미리보기까지 나온다. 승인 카드는
    pydantic 이 직렬화해 멀쩡하고 **여기서만** TypeError 가 났다 (PR #74 리뷰).

    빠뜨리지 않고 문자열로 남긴다 — 모델에게 안 보여 주는 것이 곧 #50 이다.
    dict 키까지 바꾸는 이유는 `default=` 가 키에는 안 걸리기 때문이다.
    """
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _with_manifest(context: str, preview: list[dict] | None) -> str:
    """Plan 본문 뒤에 마스킹된 매니페스트를 붙인다. 없으면 그대로 (#50).

    길이 상한을 두지 않는다 — 이 본문은 **모델이 직접 쓴 것**이라 (사용자는 한국어만 보낸다)
    모델의 출력 한도보다 클 수 없다. 상한이 필요해지는 건 사람이 올린 파일을 싣게 될 때다.
    """
    if not preview:
        return context
    body = json.dumps(_json_safe(preview), ensure_ascii=False, indent=2)
    return f"{context}\n\n## 매니페스트 미리보기 (민감값은 <redacted>)\n\n```json\n{body}\n```\n"


async def generate_decision_guidance(
    plan: ActionPlan, manifest_preview: list[dict] | None = None,
) -> str:
    load_dotenv(".env")
    plan.validate_for_decision_guidance()
    context = _with_manifest(plan.render_markdown(include_decision_guidance=False), manifest_preview)
    result = await guidance_agent.run(context)
    guidance = result.output.strip()
    if not guidance:
        raise ValueError("LLM returned empty decision guidance")
    return guidance
