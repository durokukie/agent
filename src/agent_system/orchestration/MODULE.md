# Orchestration 모듈

## 목적과 책임

Task와 실행 기록의 생명주기, 동적 supervisor graph, 에이전트 선택 및 반복·종료 정책을 관리한다.

## 포함할 구현

현재는 framework에 독립적인 `Task`, `WorkflowRun`, `AgentRun`, `Approval`, `ExecutionBudget`과 상태·단계 전이 정책을 포함한다. 이후 LangGraph graph 구성, supervisor node와 routing이 이 모델을 사용한다.

## 공개 인터페이스와 사용 방법

`Task.receive()`는 `RECEIVED` snapshot을 만들고 `transition()`은 아래 표의 상태 전이만 새 snapshot으로 반환한다. `cancel()`은 활성 Task를 `CANCELLED`로 만드는 명시적 연산이다. 모든 변경은 version을 1 증가시키고 timezone-aware 시각을 요구한다. `to_snapshot()`/`from_snapshot()`은 framework 타입 없는 JSON 호환 경계를 제공한다.

| 현재 status | 허용하는 다음 status |
| --- | --- |
| `RECEIVED` | `RUNNING`, `CANCELLED` |
| `RUNNING` | `WAITING_APPROVAL`, `COMPLETED`, `REJECTED`, `FAILED`, `CANCELLED`, `ESCALATED` |
| `WAITING_APPROVAL` | `RUNNING`, `REJECTED`, `CANCELLED`, `ESCALATED` |
| `COMPLETED`, `REJECTED`, `FAILED`, `CANCELLED`, `ESCALATED` | 없음 |

`RUNNING → WAITING_APPROVAL`에는 plan hash가 있어야 한다. `WAITING_APPROVAL → RUNNING`에는 현재 `task_id`, `version`, `plan_hash`에 모두 묶인 `Approval`이 필요하다. Task 식별자 불일치, plan 변경, 오래된 version은 각각 명시적인 Approval 오류이며 검증 순서는 식별자, plan, version 순이다. plan은 `RUNNING` 또는 `WAITING_APPROVAL`에서만 교체할 수 있고 교체 자체도 version을 증가시킨다.

`WorkflowRun.start()`는 `CLASSIFYING`에서 시작한다. `advance()`는 `CLASSIFYING → ANALYZING → PLANNING → GOVERNING → EXECUTING → VERIFYING` 순서와 budget으로 제한되는 재실행을 위한 `VERIFYING → EXECUTING`만 허용한다. `consume_budget()`은 Agent 호출 직전에 1회를 소비하며 남은 횟수가 없으면 `ExecutionBudgetExhaustedError`를 발생시킨다.

`AgentRun.start()`는 한 번의 Agent 호출을 열린 기록으로 만들고 `complete()`는 같은 `agent_id`의 결과로 한 번만 종료한다. 이 타입은 Agent 구현 계약이 아니라 orchestration이 소유하는 실행 이력이다.

## 의존성과 허용된 import 방향

현재 생명주기 모델은 Python 표준 라이브러리에만 의존한다. 이후 graph는 `agents`의 공통 인터페이스와 registry, `schemas`, 관측 인터페이스를 사용할 수 있지만 개별 agent 구현은 직접 import하지 않는다. SQLite, HTTP, Rich, LangGraph 타입은 생명주기 snapshot에 포함하지 않는다.

## 데이터 및 제어 흐름

Task 변경은 이전 snapshot을 보존한 채 새 version을 만든다. WorkflowRun이 phase와 budget을 추적하고 AgentRun이 개별 호출 결과를 기록한다. 이후 supervisor는 이 값들을 평가해 agent를 선택하고 결과를 반영한 뒤 재호출 또는 종료를 결정한다.

## 설계 결정과 제약사항

모델은 frozen dataclass로 외부 변경을 막고 생성·복원 시에도 식별자, version, plan/status 조합, budget 범위와 시각을 검증한다. 상태와 phase의 자기 전이 및 표에 없는 건너뛰기는 금지한다. 실행 횟수 budget은 양의 정수이며 마지막 허용 호출까지 소비할 수 있고 그다음 호출은 명시적으로 실패한다. 라우팅 판단은 추적 가능한 구조화 결과로 남긴다.

## 테스트 전략

모든 status·phase 조합을 표 기반 단위 테스트로 검증한다. Approval의 task/version/plan 결합, plan 변경, 취소, budget 경계, JSON snapshot 왕복과 AgentRun 완료 불변 조건을 외부 I/O 없이 검증한다. 이후 결정 가능한 fake agent로 routing과 종료 조건을 검증한다.

## 변경 시 문서 갱신 조건

상태·phase 전이 행렬, snapshot 필드, Approval 결합, budget, AgentRun 의미, graph 구조, routing 정책 또는 종료 조건이 바뀔 때 갱신한다.
