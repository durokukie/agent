"""Tool 카탈로그 — 스펙은 여기 한 번만 정의, 스킬은 이름으로 참조.

읽기 6종 (read.py):   자동 실행, 훅 없음
변경 4종 (mutate.py): 호출 시 가드레일 훅 발동 (guardrail/hook.py)

MVP 제외 (팀 결정 — 가드레일 v2): LLM이 호출하는 Action Plan 툴
(list_action_plans / get_action_plan / evaluate_action).
Action Plan은 훅이 내부적으로 생성·기록하며, 조회 기능은 히스토리 스킬과 함께 추후 추가.
"""
