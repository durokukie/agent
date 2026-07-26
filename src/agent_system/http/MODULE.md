# HTTP adapter 모듈

## 목적과 책임

FastAPI로 외부 HTTP 요청을 검증하고 runtime application interface의 명령과 조회로
변환한다. HTTP status, JSON schema와 안정적인 오류 응답만 소유하며 orchestration
graph나 영속 구현의 세부사항을 노출하지 않는다.

## 포함할 구현

Task 생성, Alert/Ticket webhook, Task 조회, 승인·거절과 취소 endpoint, strict Pydantic
request/response/error schema, FastAPI application factory와 lifespan 연결을 포함한다.

## 공개 인터페이스와 사용 방법

`create_app(application)`은 runtime이 제공하는 작은 application protocol 구현을 받아
FastAPI application을 반환한다. 일반 Task 생성은 선택적 `Idempotency-Key` HTTP header를
명시적인 멱등성 key로 사용한다. Alert와 Ticket webhook은 각각 `alert_id`, `ticket_id`를
외부 identity로 사용하며 body 전체의 canonical fingerprint가 같을 때만 replay한다.

공개 endpoint는 다음 여섯 개로 고정한다.

- `POST /v1/tasks`
- `POST /v1/webhooks/alerts`
- `POST /v1/webhooks/tickets`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/approval`
- `POST /v1/tasks/{task_id}/cancel`

명령 수락은 `202`, 알 수 없는 Task는 `404`, 멱등성·version·terminal·승인 경합은
`409`, request schema 오류는 FastAPI의 `422`, bounded queue 포화는 `503`을 반환한다.
모든 POST command endpoint는 가능한 `409`와 `503`을 OpenAPI 응답으로 선언한다.

## 의존성과 허용된 import 방향

FastAPI와 Pydantic, Python 표준 라이브러리, `runtime`의 공개 application interface만
import한다. LangGraph/checkpointer, SQLite·SQLAlchemy·persistence 구현, provider/model
adapter, concrete Agent 구현, Rich를 import하지 않는다. `runtime → http` 역방향 import도
금지한다.

## 데이터 및 제어 흐름

HTTP body와 header를 strict schema로 검증한 뒤 runtime 명령을 await한다. 명령은 작업을
queue에 넣고 즉시 수락 결과를 돌려주며 handler가 graph를 직접 실행하지 않는다. 조회는
runtime이 persistence authority에서 만든 공개 snapshot을 직렬화한다. FastAPI lifespan은
주입된 application의 시작과 종료만 호출한다.

## 설계 결정과 제약사항

Schema는 알 수 없는 필드를 거부하고 문자열 길이를 제한한다. 오류 body는 안정적인 code와
일반화된 message만 가지며 provider 예외, 비밀값과 내부 traceback을 포함하지 않는다.
HTTP adapter는 Rich 출력이나 색상 설정을 사용하지 않는다.

## 테스트 전략

`httpx.AsyncClient`와 ASGI transport로 실제 FastAPI routing·validation·lifespan 경계를
검증한다. runtime fake를 이용한 contract test와 임시 SQLite·compiled graph를 이용한 핵심
수직 통합 경로를 분리하고 외부 API는 호출하지 않는다.

## 변경 시 문서 갱신 조건

Endpoint, status/error mapping, request/response 필드, 멱등성 identity, input bound,
lifespan 소유권 또는 허용 import 방향이 바뀔 때 이 문서를 갱신한다.
