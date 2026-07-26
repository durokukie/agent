# agent_system 패키지

## 목적과 책임

HTTP/CLI 전송, 애플리케이션 조립, 오케스트레이션, 에이전트, 도구, 모델, 저장,
알림 및 관측 기능을 하나의 모듈형 모놀리스로 제공한다.

## 포함할 구현

`http`, `cli`, `runtime`, `orchestration`, `agents`, `tools`, `models`,
`persistence`, `notifications`, `observability`, `schemas`, `config` 모듈을 포함한다.

## 공개 인터페이스와 사용 방법

HTTP와 CLI는 내부 모듈을 직접 조립하지 않고 `runtime`의 application interface를 사용한다.

## 의존성과 허용된 import 방향

의존성 방향은 `http/cli → runtime → orchestration → agents`를 기본으로 한다. 공용 타입은 `schemas`에 두며 역방향 import를 금지한다.

## 데이터 및 제어 흐름

HTTP/CLI 요청은 runtime에서 먼저 영속화되고 background queue를 거쳐 orchestration graph가
에이전트를 선택한다. 조회는 persistence authority에서 조립되며 대상 Task 전이는
transactional outbox와 dispatcher를 거쳐 알림 sender에 전달된다.

## 설계 결정과 제약사항

초기에는 단일 프로세스로 실행하지만 `Agent` seam을 통해 향후 원격 adapter로 전환할 수 있어야 한다.

## 테스트 전략

모듈별 단위 테스트와 전체 실행 경로 통합 테스트를 분리한다.

## 변경 시 문서 갱신 조건

최상위 모듈, 호출 흐름, 의존성 규칙 또는 공개 진입점이 바뀔 때 갱신한다.
