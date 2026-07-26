# 통합 테스트 모듈

## 목적과 책임

여러 모듈과 실제 로컬 adapter가 함께 동작하는 경로를 검증한다.

## 포함할 구현

LangGraph graph 연결·interrupt·Command resume·실행 중 issuance checkpoint, SQLite app migration·transaction·checkpoint 재개, runtime 조립과 CLI 실행 테스트를 포함한다.

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
rollback, event append-only, 멱등성 race, Approval 단일 소비, WorkflowRun/AgentRun
원자성, outbox와 startup recovery를 검증한다. Supervisor graph는 실제 compiled graph와
`InMemorySaver`로 승인 binding·거절·resume·replay와 Agent 호출 전 issuance 저장 순서를
검증한다. SQLite checkpoint는 interrupt 후 연결을 닫고 새 연결에서 재개한다. 이후
runtime 조립과 저장소 오류 경로를 확장한다.

## 변경 시 문서 갱신 조건

통합 범위, 외부 의존성 정책 또는 임시 자원 관리가 바뀔 때 갱신한다.
