"""Kukie — 쿠버네티스를 배우면서 안전하게 쓰는 에이전트.

패키지 구조:
    agent.py       에이전트 조립 (프롬프트·툴·훅·검증기 연결)
    deps.py        세션 의존성 (context/namespace/skill)
    router.py      스킬 선택 (MVP: 직접 선택 + 전환 제안)
    cli.py         진입점 (kukie chat)
    validators.py  출력 검증 (설명 필드 강제 — 기능 1 핵심 훅)
    kubectl/       kubectl 실행 계층 (조립·subprocess)
    tools/         읽기 6종 + 변경 4종
    skills/        스킬 5종 (프롬프트 + 툴 목록 + 응답 스키마)
    guardrail/     가드레일 훅 (룰 엔진·Action Plan·승인) — 기능 2

설정(.env)은 여기서 가장 먼저 읽는다. KUKIE_MODEL·KUKIE_GUIDANCE_MODEL 은 각 모듈이
import 시점에 os.environ 에서 읽으므로, 그 전에 로드되지 않으면 .env 값이 무시된다.
load_dotenv 는 이미 있는 환경변수를 덮어쓰지 않는다 — 셸에서 준 값이 항상 우선.
"""
from dotenv import load_dotenv

load_dotenv(".env")
