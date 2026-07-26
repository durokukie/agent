# Persistence revision 모듈

## 목적과 책임

각 app schema 변경을 순서와 rollback 정보를 가진 Alembic revision으로 보존한다.

## 포함할 구현

Task, event, WorkflowRun, AgentRun, Approval, request idempotency와 outbox table을 만드는
`0001_initial`, 승인·거절 `ApprovalResponse`와 exact successor/failure를 decision key 및
Task binding에 결합하는 `0002_approval_decisions`, START/APPROVAL/CANCEL 의도와
PENDING/COMPLETED 실행 상태를 보존하는 `0003_runtime_commands`, outbox의 Task version
dedupe·retry eligibility·lease·delivery 시각을 추가하는 `0004_notification_outbox`를 포함한다.

## 공개 인터페이스와 사용 방법

호출자가 직접 사용하지 않는다. Alembic이 revision identifier와 `upgrade()`/
`downgrade()`를 탐색해 실행한다.

## 의존성과 허용된 import 방향

Alembic operation과 SQLAlchemy schema type만 사용할 수 있다. application module과
domain type은 import하지 않는다.

## 데이터 및 제어 흐름

revision 순서대로 DDL을 적용하고 downgrade 시 역순으로 제거한다. append-only event
trigger도 table과 같은 revision에서 관리한다.

## 설계 결정과 제약사항

revision 파일은 이미 배포된 뒤 수정하지 않고 새 revision을 추가한다. SQLite가 직접
지원하지 않는 변경은 Alembic batch operation을 사용한다.

## 테스트 전략

빈 DB upgrade, head 반복 upgrade, 0001 데이터 보존 upgrade와 downgrade/upgrade
왕복에서 schema가 일관적인지 검증한다.

## 변경 시 문서 갱신 조건

revision naming, downgrade 정책, SQLite DDL 전략 또는 포함 schema가 바뀔 때 갱신한다.
