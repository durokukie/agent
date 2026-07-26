# Persistence 모듈

## 목적과 책임

생명주기 aggregate와 감사 이력을 SQLite에 저장하고, LangGraph checkpoint와
애플리케이션 table이 같은 데이터베이스 파일을 안전하게 공유하도록 한다. ORM row,
SQL, SQLite 연결 수명은 이 모듈 안에 숨기고 호출자에게 orchestration domain 값과
persistence 결과 값만 반환한다.

## 포함할 구현

다음을 포함한다.

- SQLAlchemy engine과 짧고 명시적인 transaction을 사용하는 `SQLiteStore`
- Task snapshot과 append-only Task event
- WorkflowRun snapshot과 모든 AgentRun 이력
- 기존 Approval 기록과 ApprovalResponse 승인·거절 decision의 단일 소비 및 exact 결과 snapshot
- webhook/request 멱등성 key
- transactional notification outbox와 상태 전이
- durable START/APPROVAL/CANCEL runtime command와 실행 상태·실패 이력
- non-terminal startup recovery 조회
- 별도 `sqlite3.Connection`을 소유하는 LangGraph `SqliteSaver`
- Alembic migration 실행 함수와 revision

## 공개 인터페이스와 사용 방법

runtime은 migration을 적용한 뒤 데이터베이스 경로를 주입해 `SQLiteStore`를 만든다.
호출자는 store의 context manager 수명 안에서 Task 생성·변경, 실행 기록, 승인 소비,
outbox와 복구 후보를 다룬다. 모든 read interface는 `Task`, `WorkflowRun`,
`AgentRun` 또는 frozen persistence value를 반환하며 ORM model을 노출하지 않는다.

Task 생성은 선택적인 `IdempotencyKey`와 함께 한 transaction으로 처리한다. 같은
namespace/key와 같은 fingerprint는 기존 Task를 replay 결과로 반환하고, fingerprint가
다르면 `IdempotencyConflictError`다. Task 변경은 현재 version과 정확히 다음 version을
비교한다. 불일치는 `OptimisticConcurrencyError`이며 snapshot과 event 어느 것도
부분 commit하지 않는다.

Task 생성은 선택적인 START command까지 같은 transaction에 묶을 수 있다. 이후 APPROVAL과
CANCEL command는 authoritative Task snapshot에 CAS로 결합한다. Task별 PENDING command는
하나이며 같은 fingerprint/type/payload만 exact replay다. 성공한 worker는 COMPLETED로
전환하고 실패한 worker는 PENDING 상태에서 attempt와 안정적인 오류 code를 갱신한다.

`WAITING_APPROVAL → RUNNING`은 일반 Task 저장으로 우회할 수 없다. 기존
`apply_approval()`은 `Approval` 수락 호출과 outbox 계약을 유지한다.
`consume_approval()`은 orchestration의 `ApprovalResponse`를 public 입력으로 받아 승인과
거절을 모두 처리한다. 제시된 Task snapshot, response binding, decision key와 현재 Task를
한 write transaction에서 비교하고 decision record, exact successor, append-only event를
함께 commit한다. 승인 successor는 `RUNNING`, 거절 successor는 `HUMAN_REJECTED` failure가
결합된 `REJECTED`다. 같은 decision/content/binding replay는 저장한 exact successor를
`ALREADY_APPLIED`로 반환한다. 같은 decision key의 다른 내용·binding, 같은 binding의
다른 decision은 `ApprovalConflictError`, 아직 소비되지 않은 authoritative version/status
불일치는 `OptimisticConcurrencyError`다. `list_approval_decisions()`는 감사와 복구를 위해
저장된 응답·successor·failure를 반환한다.

WorkflowRun 최초 저장과 일반 phase 갱신을 분리한다. AgentRun 발급 저장은 이전
WorkflowRun snapshot, issuance가 추가된 다음 snapshot과 AgentRun을 받아 세 값의
연속성과 소유권을 검증한 뒤 한 transaction에 기록한다. AgentRun 조회는 같은
transaction에서 소유 WorkflowRun을 먼저 복원하고 `AgentRun.from_snapshot(...,
workflow=owner)` 검증을 반드시 거친다.

`open_checkpointer()`는 store와 같은 파일에 연결한 `SqliteSaver` context manager다.
checkpointer table과 raw connection의 생성·종료는 이 context가 독립적으로 소유한다.
반환한 saver는 context 밖에서 사용할 수 없다.

`upgrade_database()`는 저장소 root의 설정 파일에 의존하지 않고 설치된
`agent_system.persistence` package 안의 `migrations` resource를 기준으로 Alembic
설정을 조립한다. 따라서 wheel 설치나 package만 복사한 실행 환경에서도 같은 revision을
적용할 수 있다.

## 의존성과 허용된 import 방향

`orchestration`의 공개 생명주기 값, SQLAlchemy, Alembic, LangGraph SQLite
checkpointer와 Python `sqlite3`를 사용할 수 있다. Agent 구현, model adapter,
FastAPI, Rich와 CLI에는 의존하지 않는다. orchestration은 persistence를 import하지
않는다.

## 데이터 및 제어 흐름

Task 명령은 저장된 snapshot을 읽고 optimistic 조건을 검증한 뒤 새 snapshot, event,
선택적 outbox를 commit한다. ApprovalResponse 소비는 decision replay를 먼저 판별하고,
새 decision이면 binding과 authoritative Task를 검증해 successor/event/decision record를
원자 저장한다. WorkflowRun이 발급한 immutable issuance ledger와 해당 AgentRun은 함께
저장된다. 시작 시 recovery 조회는 terminal Task를 제외하고
`WAITING_APPROVAL`을 대기 항목으로, 나머지 active Task를 재개 항목으로 구분한다.
재개 항목은 최초 수락부터 안정적인 Task ID를 thread id로 사용하고 별도 checkpointer가 기존 checkpoint를 읽어 graph 실행을
계속한다. Runtime command recovery는 생성 시각 순으로 PENDING 의도를 읽어 bounded queue에
공급하며 Task journal이 checkpoint보다 앞선 경우에도 command를 잃지 않는다.

## 설계 결정과 제약사항

SQLite는 WAL, foreign key, busy timeout을 모든 애플리케이션 연결과 checkpointer
연결에 설정한다. 애플리케이션 transaction은 write 선점을 명확히 하는
`BEGIN IMMEDIATE`와 context manager commit/rollback을 사용하며 외부 model, Agent,
알림 호출 동안 열어두지 않는다. timezone-aware 시각은 offset을 잃지 않도록 ISO 8601
문자열로 저장하고 domain snapshot 복원으로 검증한다. JSON은 canonical UTF-8 text로
저장한다. 여러 offset이 섞인 시각 기반 목록은 복원한 timezone-aware `datetime`의 실제
instant와 안정적인 식별자로 정렬한다.

Task event는 update/delete trigger로 append-only를 DB에서도 강제한다. app table은
Alembic만 생성·변경하며 `MetaData.create_all()`을 migration 대체 수단으로 사용하지
않는다. LangGraph가 소유한 checkpoint table은 Alembic metadata와 app migration에
포함하지 않는다. 지원 범위는 low-write 단일 프로세스 MVP이며 다중 writer 또는 수평
확장이 필요하면 PostgreSQL adapter 도입을 검토한다.

## 테스트 전략

임시 파일 SQLite로 빈 DB와 반복 migration, 저장소 layout 없는 package migration 및
schema drift, WAL/foreign key/busy timeout, domain snapshot fidelity, optimistic
rollback, event append-only, request idempotency race, Approval replay/conflict와 동시
소비, ApprovalResponse 승인·거절의 별도 connection 경합·exact replay·rollback·명시적
decision/binding/version/terminal conflict, runtime command의 원자 생성·exact fingerprint
replay·Task별 pending 순서·failure retry, WorkflowRun/AgentRun의 정확한 다음 상태 원자 저장·소유 복원, offset 혼합 목록
정렬, outbox 전이, terminal 제외 recovery를 통합 테스트한다. 실제 LangGraph graph를
interrupt한 뒤 checkpointer를 닫고 새 connection에서 resume한다. 외부 서비스는
사용하지 않는다.

## 변경 시 문서 갱신 조건

schema/revision, 공개 store interface, transaction과 optimistic 조건, 멱등성 key,
Approval 소비, recovery 분류, outbox 상태, SQLite PRAGMA, 자원 수명 또는 checkpoint
정책이 바뀔 때 갱신한다.
