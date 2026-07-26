# Agents 모듈

## 목적과 책임

오케스트레이터가 모든 에이전트를 동일하게 호출할 수 있는 공통 인터페이스와 registry를 정의한다.

## 포함할 구현

요청·결과 계약, `Agent` Protocol, agent metadata, registry와 결정 가능한 기본 구현을 포함한다. 설정형 및 전용 agent 구현은 이후 하위 모듈에서 이 계약을 사용해 추가한다.

## 공개 인터페이스와 사용 방법

`AgentMetadata`는 안정적인 `agent_id`, 표시 이름, 설명을 제공한다. `AgentRequest`는 `task_id`, 입력 문자열, 필수 `idempotency_key`, 선택 context를 전달하고, `AgentResult`는 실행 agent ID, `AgentOutcome`, 출력 문자열을 반환한다. `idempotency_key`는 비어 있지 않은 문자열이며 orchestration이 발급한 `agent_run_id`와 같다. 모든 Agent adapter는 같은 key로 반복된 호출의 외부 effect를 중복 적용하지 않아야 한다. 실패 사유도 출력 문자열에 담아 호출자가 결과 형식을 일관되게 처리한다.

`Agent`는 `metadata`와 `async run(request)`를 요구하는 구조적 Protocol이다. `AgentRegistry.register()`는 ID를 하나만 등록하고 중복 시 `DuplicateAgentIdError`를 발생시킨다. `get()`은 미등록 ID에 `AgentNotFoundError`를 발생시킨다. `FakeAgent`는 고정된 결과를 반환하고 받은 요청을 기록하는 결정 가능한 테스트 adapter다.

Agent 구현이 한 번 반환하는 결과와 orchestration의 `AgentRun`은 구분한다. `AgentRun`은 task/workflow/phase와 호출 시각, 완료 결과를 연결하는 orchestration 소유 실행 이력이며 Agent의 공개 interface에 포함되지 않는다. 따라서 agents 모듈은 `AgentRun`을 import하거나 생성하지 않는다.

## 의존성과 허용된 import 방향

공용 `schemas`, `tools`, `models`를 사용할 수 있다. `orchestration`과 다른 구체 agent 구현에는 의존하지 않으며, 실행 이력 기록은 호출자인 orchestration이 담당한다.

## 데이터 및 제어 흐름

runtime이 구현체를 registry에 등록하고, registry가 요청된 agent adapter를 반환한다. 오케스트레이터는 공통 Protocol로 요청과 실행 멱등 키를 전달해 구조화 결과를 받는다. retry의 새 AgentRun은 새 key를 받고, crash recovery로 같은 열린 AgentRun을 다시 호출할 때는 기존 key를 재사용한다.

## 설계 결정과 제약사항

설정형 agent, 자체 LangGraph agent, 향후 원격 adapter는 모두 같은 구조적 Protocol을 만족해야 한다. registry는 조회만 담당하며 실행 정책이나 구현 종류를 알지 못한다. 기본 `EchoAgent`는 외부 I/O 없이 입력을 그대로 반환하는 최소 참조 구현이다.

## 테스트 전략

공통 contract test를 `EchoAgent`와 `FakeAgent`에 적용하고 `AgentRequest.idempotency_key` 검증, registry의 정상 조회, 중복 식별자, 미등록 조회를 검증한다. fake는 네트워크·시간·난수에 의존하지 않는다.

## 변경 시 문서 갱신 조건

공통 인터페이스, 요청·결과 필드, metadata, registry 동작, agent 종류 또는 Agent 호출 결과와 `AgentRun` 사이의 책임 경계가 바뀔 때 갱신한다.
