# Orchestration 모듈

## 목적과 책임

동적 supervisor graph, 전체 실행 state, 에이전트 선택 및 반복·종료 정책을 관리한다.

## 포함할 구현

LangGraph graph 구성, supervisor node, routing, 실행 budget과 종료 정책을 포함한다.

## 공개 인터페이스와 사용 방법

초기 요청과 실행 컨텍스트를 받아 최종 결과 또는 실행 event stream을 반환하는 작은 인터페이스를 제공한다.

## 의존성과 허용된 import 방향

`agents`의 공통 인터페이스와 registry, `schemas`, 관측 인터페이스를 사용할 수 있다. 개별 agent 구현은 직접 import하지 않는다.

## 데이터 및 제어 흐름

supervisor가 현재 state를 평가하고 agent를 선택하며 결과를 state에 반영한 뒤 재호출 또는 종료를 결정한다.

## 설계 결정과 제약사항

무한 반복을 막기 위해 호출 횟수, 비용 또는 시간 budget을 명시한다. 라우팅 판단은 추적 가능한 구조화 결과로 남긴다.

## 테스트 전략

결정 가능한 fake agent로 라우팅, 반복, 실패, budget 소진과 종료 조건을 단위 테스트한다.

## 변경 시 문서 갱신 조건

graph 구조, state schema, routing 정책 또는 종료 조건이 바뀔 때 갱신한다.
