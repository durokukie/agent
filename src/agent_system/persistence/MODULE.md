# Persistence 모듈

## 목적과 책임

LangGraph checkpoint와 실행 이력을 SQLite에 저장하고 재개 가능한 실행을 지원한다.

## 포함할 구현

SQLite 연결 관리, checkpointer 구성, run metadata와 event 이력 저장을 포함한다.

## 공개 인터페이스와 사용 방법

runtime이 저장 자원을 생성해 orchestration에 주입한다. 호출자는 SQLite 연결 세부사항을 알 필요가 없다.

## 의존성과 허용된 import 방향

`config`, LangGraph checkpointer와 SQLite driver를 사용할 수 있다. CLI나 agent 구현에는 의존하지 않는다.

## 데이터 및 제어 흐름

실행 중 state checkpoint와 event가 thread/run 식별자로 기록되고 재개 요청 시 복원된다.

## 설계 결정과 제약사항

checkpoint와 관측용 실행 이력의 책임을 구분한다. 트랜잭션, migration, 보존 정책을 명시적으로 관리한다.

## 테스트 전략

임시 SQLite 데이터베이스로 저장·조회·재개·동시 접근·migration을 통합 테스트한다.

## 변경 시 문서 갱신 조건

schema, migration, transaction, 보존 또는 checkpoint 정책이 바뀔 때 갱신한다.
