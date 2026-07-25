# 설정형 Agent 모듈

## 목적과 책임

공통 실행 엔진에 모델, prompt, tool 집합과 정책을 주입해 단순 agent를 구성한다.

## 포함할 구현

설정 schema, 공통 실행 graph, prompt 로딩과 tool 선택을 포함한다.

## 공개 인터페이스와 사용 방법

설정 객체로 agent를 생성하고 상위 `agents`의 공통 인터페이스로 실행한다.

## 의존성과 허용된 import 방향

상위 agent 계약, `models`, `tools`, `schemas`에 의존할 수 있다. 전용 agent와 orchestration에는 의존하지 않는다.

## 데이터 및 제어 흐름

설정을 로딩해 공통 graph를 구성하고 요청을 처리한 뒤 표준 agent result로 변환한다.

## 설계 결정과 제약사항

조건 분기가 복잡해지거나 고유 state가 필요하면 설정을 확장하지 말고 전용 agent로 승격한다.

## 테스트 전략

설정 검증, prompt 선택, 허용 tool 제한과 공통 인터페이스 준수를 검증한다.

## 변경 시 문서 갱신 조건

설정 schema, 공통 실행 흐름 또는 전용 agent 승격 기준이 바뀔 때 갱신한다.
