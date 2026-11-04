"""관측(OpenTelemetry/Logfire) 계측 — 스모크·운영에서 run 내부를 보기 위한 것.

pydantic-ai 는 계측이 내장이라 켜기만 하면 run·LLM 호출·툴 실행이 span 트리로 남는다.
무엇을 보게 되는가:
    [span] agent run
      ├─ LLM 호출 1 (모델, 토큰 수)
      ├─ 툴: list_resources (인자, 결과)
      └─ LLM 호출 2 ...
승인 흐름에서는 훅이 던진 ApprovalRequired 로 run 이 멈춘 지점까지 그대로 보인다.

켜지는 조건: LOGFIRE_TOKEN 또는 OTEL_EXPORTER_OTLP_ENDPOINT 중 하나라도 있을 때.
둘 다 없으면 아무 일도 하지 않는다 — 테스트와 평상시 실행이 계측 때문에 느려지거나
바깥으로 데이터를 보내는 일이 없어야 한다 (opt-in).

목적지는 코드가 아니라 환경변수가 정한다.
    LOGFIRE_TOKEN=...                       Logfire 클라우드
    OTEL_EXPORTER_OTLP_ENDPOINT=https://... 자가 호스팅 수집기 (OTLP 표준)
전환에 코드 변경이 필요 없다 (DURO-59 첨부 "에이전트 옵스 계측 상세" 참고).

내용 포함 여부: KUKIE_TRACE_CONTENT=1 이면 프롬프트·응답 본문까지 기록한다.
스모크 단계에서는 켜야 판단 품질을 볼 수 있고, 배포 시점 정책은 DURO-59 미정 항목이다.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(
        os.environ.get("LOGFIRE_TOKEN")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def setup() -> bool:
    """계측을 켠다. 켰으면 True, 조건 미충족이거나 실패하면 False.

    관측은 본업의 곁가지다 — 계측 설정이 실패해도 에이전트는 정상 동작해야 하므로
    예외를 밖으로 내보내지 않고 경고만 남긴다.
    """
    if not _enabled():
        return False
    try:
        import logfire

        from kukie_agent import prompts

        logfire.configure(
            service_name="kukie",
            # 토큰이 없으면 SaaS 로 보내지 않는다 — OTLP 엔드포인트만 쓰는 구성 지원
            send_to_logfire=bool(os.environ.get("LOGFIRE_TOKEN")),
            # 모든 span 의 resource 에 프롬프트 버전(kukie.prompt.version)과 파일별 지문(kukie.prompt.files) — prompts 모듈
            resource_attributes=prompts.resource_attributes(),
        )
        logfire.instrument_pydantic_ai(
            include_content=os.environ.get("KUKIE_TRACE_CONTENT") == "1",
        )
    except Exception:
        logger.exception("observability setup failed — 계측 없이 계속 진행한다")
        return False
    return True
