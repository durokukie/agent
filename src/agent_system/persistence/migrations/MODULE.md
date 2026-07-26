# Persistence migration 모듈

## 목적과 책임

애플리케이션이 소유하는 SQLite schema의 순서 있는 변경 이력을 Alembic으로 관리한다.

## 포함할 구현

Alembic 실행 환경과 `versions` 하위 revision을 포함한다. LangGraph checkpoint table은
이 모듈의 migration 대상이 아니다.

## 공개 인터페이스와 사용 방법

직접 import하는 공개 Python interface는 없다. `agent_system.persistence`의 migration
실행 함수가 Alembic configuration과 데이터베이스 경로를 주입한다.

## 의존성과 허용된 import 방향

Alembic과 SQLAlchemy migration primitive를 사용하고 drift 검사를 위해 private
`_schema.Base.metadata`를 읽을 수 있다. orchestration, runtime, CLI, Agent 구현을
import하지 않는다.

## 데이터 및 제어 흐름

빈 데이터베이스 또는 이전 revision에 `upgrade head`를 적용해 app table과 제약을
만든다. 현재 head에 반복 적용하면 schema 변경 없이 종료한다.

## 설계 결정과 제약사항

revision은 명시적 DDL로 작성한다. 제품 시작 경로에서 `create_all()`을 사용하지 않으며
app schema와 checkpointer schema의 소유권을 섞지 않는다.

## 테스트 전략

임시 빈 파일에 head migration을 적용하고 table, foreign key, index, trigger와 현재
revision을 검사한다. 같은 파일에 반복 적용해 idempotency를 검증한다.

## 변경 시 문서 갱신 조건

migration 위치, 실행 방식, schema 소유권 또는 revision 정책이 바뀔 때 갱신한다.
