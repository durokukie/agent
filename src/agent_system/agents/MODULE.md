# Agents 모듈

## 목적과 책임

오케스트레이터가 모든 에이전트를 동일하게 호출할 수 있는 공통 인터페이스와 registry를 정의한다.

## 포함할 구현

요청·결과 계약, agent metadata, registry, 설정형 및 전용 agent 구현을 포함한다.

## 공개 인터페이스와 사용 방법

핵심 인터페이스는 agent 식별자와 metadata 조회, 비동기 실행이다. 구체적인 타입과 오류 형태는 구현 시 이 문서에 기록한다.

## 의존성과 허용된 import 방향

공용 `schemas`, `tools`, `models`를 사용할 수 있다. `orchestration`과 다른 구체 agent 구현에는 의존하지 않는다.

## 데이터 및 제어 흐름

registry가 요청된 agent adapter를 반환하고 오케스트레이터가 공통 인터페이스로 요청을 전달해 구조화 결과를 받는다.

## 설계 결정과 제약사항

설정형 agent와 자체 LangGraph agent가 같은 외부 인터페이스를 만족하도록 내부 복잡성을 숨긴다.

## 테스트 전략

공통 contract test를 모든 agent adapter에 적용하고 registry의 중복 식별자와 미등록 조회를 검증한다.

## 변경 시 문서 갱신 조건

공통 인터페이스, metadata, registry 동작 또는 agent 종류가 바뀔 때 갱신한다.
