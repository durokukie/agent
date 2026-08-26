"""가드레일 계층 (기능 2 — Action Plan 가드레일).

구성:
    hook.py         가드레일 훅 — 8단계 파이프라인 (변경 툴 호출이 트리거)
    rules.py        룰 엔진 — LLM 밖 결정론 판정, fail-closed
    rules.yaml      룰셋 — ⚠️ 초안. k8s 운영 경험자 리뷰 필수
    action_plan.py  Action Plan .md 생성/갱신
    approval.py     Plan 기반 Electron 승인 DTO·응답 검증

원칙 (기능 2 §0): 가드레일은 스킬이 아니라 훅이다.
어떤 스킬에서든 변경 툴이 호출되면 자동 발동하며, LLM도 사용자도 끌 수 없다.
"""
