# Observability 모듈

## 목적과 책임

실행 상태를 디버깅하고 추적할 수 있도록 구조화 event와 logging 규칙을 제공한다.

## 포함할 구현

run·step 식별자, 구조화 event, 로그 context, 민감정보 제거와 선택적 tracing 연결을 포함한다.

## 공개 인터페이스와 사용 방법

내부 모듈은 표준 event를 발행하고 observability 구현이 이를 로그나 trace로 변환한다.

## 의존성과 허용된 import 방향

공용 `schemas`와 logging/tracing 라이브러리에 의존할 수 있다. 업무 모듈의 구체 구현에는 의존하지 않는다.

## 데이터 및 제어 흐름

오케스트레이터와 agent 실행 event에 correlation 정보를 추가하고 허용된 sink로 전달한다.

## 설계 결정과 제약사항

prompt, 사용자 입력, 모델 응답과 tool 결과는 기본적으로 민감할 수 있으므로 명시적 정책 없이 원문을 기록하지 않는다.

## 테스트 전략

event schema, correlation 전파, 민감정보 제거와 logging 실패 격리를 검증한다.

## 변경 시 문서 갱신 조건

event schema, 로그 필드, 민감정보 정책 또는 tracing 연동이 바뀔 때 갱신한다.
