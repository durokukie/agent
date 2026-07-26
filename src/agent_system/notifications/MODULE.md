# Notification 모듈

## 목적과 책임

Task 상태 전이에서 생성된 안정적인 알림 값을 외부 channel로 전달한다. 알림 wire
계약과 sender seam, retry dispatcher를 이 모듈 안에 두어 orchestration과 HTTP가 전달
adapter나 SQLite 구현을 알지 않게 한다.

## 포함할 구현

- `Notification`, `NotificationChannel` 불변 wire 값
- `NotificationSender`, `NotificationOutbox` protocol
- 표준 logging으로 전달하는 `LoggingNotificationSender`
- 테스트와 로컬 실행용 `FakeNotificationSender`
- lease 기반 claim을 소비하고 bounded exponential backoff를 적용하는
  `NotificationDispatcher`

## 공개 인터페이스와 사용 방법

Runtime composition root는 outbox adapter와 sender를 주입해 dispatcher를 만들고
`start()`, `stop()`, `drain()` 수명을 소유한다. Sender는 `send(notification)`만 구현하며
Task, WorkflowRun, ORM row를 받지 않는다. 실제 sender는 `notification_id`를 외부 channel의
idempotency key로 사용하고 cancellation을 지연 없이 따라야 한다. Outbox adapter는 eligible
item claim, lease 갱신, 성공 확정, 실패 기록만 제공한다.

## 의존성과 허용된 import 방향

Python 표준 라이브러리만 사용한다. `orchestration`, `persistence`, `runtime`, FastAPI,
model 또는 Agent 구현을 import하지 않는다. Runtime은 이 모듈의 공개 계약을 조립할 수
있고 persistence adapter는 `NotificationOutbox`를 구현할 수 있다.

## 데이터 및 제어 흐름

Dispatcher는 notification 전용 topic에서 현재 시각 기준으로 전달 가능한 한 건을 lease와
함께 claim한다. Claim에
성공하면 payload를 `Notification`으로 복원해 sender에 전달하고, 성공은 delivered로
확정한다. 실패는 안정적인 오류 code와 다음 eligibility를 기록하며 Task 상태는 변경하지
않는다. Malformed payload는 안정적인 오류와 backoff를 기록하고 bounded skip loop에서 다음
eligible row를 계속 찾는다. 같은
dispatcher의 worker와 여러 dispatcher가 동시에 실행되어도 SQLite CAS로 한 lease만 소유한다.

## 설계 결정과 제약사항

Payload는 알림 ID, Task ID/version/status, channel, 발생 시각과 제한된 metadata만 가진다.
원본 webhook, provider 예외 문자열, 비밀정보는 포함하지 않는다. Backoff는 상한이 있는
지수 증가이며 sender timeout은 lease보다 짧고 heartbeat 두 주기는 lease보다 짧아야 한다.
Active sender가 cancellation을 지연해도 heartbeat CAS로 lease를 갱신한다. Lease 상실 시
sender를 취소하고 stale 결과를 확정하지 않는다. 외부 `notification_id` 멱등성은 crash와
분산 지연에 대한 defense-in-depth다. In-process
wakeup은 내구성의 근거가 아니다. 재시작 시 persisted
`next_attempt_at`과 만료 lease가 복구의 authority다.

## 테스트 전략

불변 wire 값의 deep copy, logging/fake sender, 전달 성공과 실패, backoff eligibility,
dispatcher 경쟁, cancellation-resistant sender heartbeat, lease 상실, stale finalize, lease 만료
복구, secret-injected malformed/legacy topic과 valid-behind-poison,
stop/drain과 재시작 retry를 실제 임시 SQLite outbox
adapter와 함께 검증한다. 외부 network는 사용하지 않는다.

## 변경 시 문서 갱신 조건

Wire field, sender/outbox interface, retry·lease 정책, dispatcher 수명, import 방향 또는
오류 보존 정책이 바뀔 때 이 문서를 갱신한다.
