# 전용 Agent 모듈

## 목적과 책임

고유 state, node 또는 복잡한 workflow가 필요한 agent를 독립 모듈로 관리한다.

## 포함할 구현

각 하위 디렉터리에 하나의 전용 agent와 자체 graph, state, node, prompt를 둔다.

## 공개 인터페이스와 사용 방법

각 전용 agent는 상위 `agents`의 공통 인터페이스를 adapter로 구현하고 registry에 등록된다.

## 의존성과 허용된 import 방향

공용 agent 계약, `models`, `tools`, `schemas`를 사용할 수 있다. 다른 전용 agent나 orchestration을 직접 import하지 않는다.

## 데이터 및 제어 흐름

표준 요청을 내부 state로 변환해 자체 graph를 실행하고 표준 결과로 변환해 반환한다.

## 설계 결정과 제약사항

한 agent의 구현과 지식은 해당 디렉터리에 모아 변경의 locality를 유지한다.

## 테스트 전략

공통 contract test와 agent 고유 graph·node 테스트를 함께 작성한다.

## 변경 시 문서 갱신 조건

전용 agent 공통 규칙이나 디렉터리 구성 원칙이 바뀔 때 갱신한다.
