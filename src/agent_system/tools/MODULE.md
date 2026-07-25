# Tools 모듈

## 목적과 책임

에이전트가 외부 기능을 안전하고 일관되게 호출할 수 있는 tool 계약과 registry를 관리한다.

## 포함할 구현

tool metadata, 입력·출력 schema, registry, 권한 및 내장 tool을 포함한다.

## 공개 인터페이스와 사용 방법

에이전트는 registry에서 허용된 tool을 조회해 구조화된 입력으로 호출한다.

## 의존성과 허용된 import 방향

공용 `schemas`와 필요한 외부 adapter에 의존할 수 있다. agent나 orchestration의 구체 구현에는 의존하지 않는다.

## 데이터 및 제어 흐름

agent 요청이 tool 호출로 변환되고 검증·실행된 결과가 구조화되어 agent로 돌아간다.

## 설계 결정과 제약사항

tool 이름은 안정적으로 유지하고 부수 효과, 권한 요구사항, 오류 형태를 인터페이스에 명시한다.

## 테스트 전략

입력 검증, 권한 거부, timeout, adapter 오류와 정상 결과를 fake 외부 시스템으로 검증한다.

## 변경 시 문서 갱신 조건

tool 계약, registry, 권한 모델 또는 공통 오류 정책이 바뀔 때 갱신한다.
