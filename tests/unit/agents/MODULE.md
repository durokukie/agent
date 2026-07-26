# Agents 단위 테스트 모듈

## 목적과 책임

`agent_system.agents`의 공개 계약과 registry 불변 조건을 외부 I/O 없이 검증한다.

## 포함할 구현

공통 Agent contract test, 결정 가능한 fake와 기본 구현의 동작, registry 조회·등록 오류 테스트를 포함한다.

## 공개 인터페이스와 사용 방법

테스트는 `Agent`, `AgentRequest`, `AgentResult`, `AgentMetadata`, `AgentRegistry`의 공개 API만 import한다. `PYTHONPATH=src python3 -m unittest discover -s tests -t .`로 실행할 수 있다.

## 의존성과 허용된 import 방향

표준 라이브러리와 `agent_system.agents`만 사용한다. 제품 코드는 이 디렉터리를 import하지 않는다.

## 데이터 및 제어 흐름

고정된 요청을 각 Agent 구현에 전달하고, 공통 결과 형태와 registry의 관찰 가능한 조회·오류 결과를 검증한다.

## 설계 결정과 제약사항

contract test는 구현 세부사항이나 mock 호출이 아니라 구조적 Agent 계약을 검증한다. 모든 fixture는 시간, 난수, 네트워크에 독립적이다.

## 테스트 전략

성공·실패 결과, 필수 실행 멱등 키의 strict validation, fake의 결정성, 정상 조회, 중복 ID, 미등록 ID를 독립된 단위 테스트로 다룬다.

## 변경 시 문서 갱신 조건

Agent 계약, registry 오류, contract 대상 구현 또는 테스트 실행 방법이 바뀔 때 갱신한다.
