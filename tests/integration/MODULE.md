# 통합 테스트 모듈

## 목적과 책임

여러 모듈과 실제 로컬 adapter가 함께 동작하는 경로를 검증한다.

## 포함할 구현

LangGraph graph 연결·분류·routing·오류 정책·interrupt·Command resume·실행 중 issuance checkpoint, SQLite app migration·transaction·checkpoint 재개, runtime 조립과 CLI 실행 테스트를 포함한다.

## 공개 인터페이스와 사용 방법

각 테스트는 실제 사용자 흐름 또는 명확한 모듈 조합 하나를 실행한다.

## 의존성과 허용된 import 방향

실제 로컬 adapter를 사용할 수 있지만 외부 네트워크와 유료 모델 호출은 기본 테스트에서 제외한다.

## 데이터 및 제어 흐름

임시 자원과 fake 모델로 애플리케이션 흐름을 실행하고 결과, 저장 상태와 event를 검증한다.

## 설계 결정과 제약사항

테스트마다 독립된 임시 데이터베이스와 실행 식별자를 사용한다.

## 테스트 전략

임시 파일 데이터베이스만 사용해 migration 반복 적용, WAL과 foreign key, optimistic
rollback, event append-only, 멱등성 race, Approval 단일 소비와 ApprovalResponse
승인·거절의 별도 SQLiteStore 경합·exact replay·rollback·conflict, WorkflowRun/AgentRun
원자성, outbox와 startup recovery를 검증한다. Supervisor graph는 실제 compiled graph와
`InMemorySaver`로 승인 binding·거절·resume·replay, consume 뒤 checkpoint 전 crash healing, 두 service의 승인 소비 경합과 Agent 호출 전 issuance 저장 순서를
검증한다. Persistence 공개 sync saver를 async supervisor에 주입해 실행하고 새 연결에서
결과를 복원하는 호환 경로도 검증한다. SQLite checkpoint는 interrupt 또는 열린 issuance 뒤 연결을 닫고 새
연결에서 동일 budget, 실행 ID와 idempotency key로 재개한다. 공유 coordinator와 별도 SQLite 연결을 사용하는 두 service의 start/start, blocked resume/recover, `issue_agent_run` 직전과 열린 `call_agent`의 recover/recover 경합에서 동일 thread claim이 한 issuance·호출·effect·budget만 허용하는지 확인한다. Claim 취소 뒤 같은 coordinator 재시도, 오류 해제, forged/malformed collaborator result와 malformed recovery 오류 정규화를 검증한다. 승인 대기·terminal 복구는 claim 없는 무동작임을 확인한다. Fake classifier/Governance/Agent로 read-only·mutating 분기, 입력 종류,
malformed 결과와 모든 retry failure code를 compiled graph의 공개 facade에서 검증한다. 이후
FastAPI ASGI부터 runtime queue, compiled graph, SQLite journal/checkpoint까지 read-only 완료,
사람 승인과 취소를 실제 수직 경로로 검증한다. Runtime contract는 durable-before-queue,
webhook 멱등성, queue 포화 뒤 durable pump, background failure exact retry, startup command
recovery, CAS 뒤 approval 자동 복구, approval/cancel 명령과 취소 사유 감사, DB-ahead 전체
callback과 completion clock replay, OpenAPI 409/503 계약과
소유 자원 종료를 검증한다.

## 변경 시 문서 갱신 조건

통합 범위, 외부 의존성 정책 또는 임시 자원 관리가 바뀔 때 갱신한다.
