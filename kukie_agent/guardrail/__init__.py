"""가드레일 계층 (기능 2 — Action Plan 가드레일).

구성:
    hook.py         변경 툴의 dry-run·Deferred 승인·실행 Hook
    action_plan.py  Action Plan .md 생성/갱신
    approval.py     Plan 기반 Electron 승인 DTO·응답 검증
    decision_guidance.py  승인 전 판단 보조 생성
    mutation_request.py   Hook과 승인 응답의 mutation 인자 정규화

원칙 (기능 2 §0): 가드레일은 스킬이 아니라 훅이다.
어떤 스킬에서든 변경 툴이 호출되면 자동 발동하며, LLM도 사용자도 끌 수 없다.
"""
