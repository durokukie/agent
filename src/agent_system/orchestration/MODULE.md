# Orchestration 모듈

## 목적과 책임

Task와 실행 기록의 생명주기, 동적 supervisor graph, 에이전트 선택 및 반복·종료 정책을 관리한다.

## 포함할 구현

현재는 framework에 독립적인 `Task`, `WorkflowRun`, `AgentRun`, `Approval`, `ExecutionBudget`과 상태·단계 전이 정책을 포함한다. 공통 오류, UTC 시각 검증과 typed snapshot parsing은 private `_support.py`에 두고 공개 이름은 module root에서 제공한다. 이후 LangGraph graph 구성, supervisor node와 routing이 이 모델을 사용한다.

## 공개 인터페이스와 사용 방법

`Task.receive()`는 `RECEIVED` snapshot을 만들고 `transition()`은 아래 표의 상태 전이만 새 snapshot으로 반환한다. `cancel()`은 활성 Task를 `CANCELLED`로 만드는 명시적 연산이다. 모든 변경은 version을 1 증가시키고 timezone-aware 시각을 요구하며, 시각 순서는 DST fold와 무관하게 UTC 절대시각으로 비교한다. 모든 persisted aggregate의 `to_snapshot()`/`from_snapshot()`은 framework 타입 없는 JSON 호환 경계를 제공한다.

| 현재 status | 허용하는 다음 status |
| --- | --- |
| `RECEIVED` | `RUNNING`, `CANCELLED` |
| `RUNNING` | `WAITING_APPROVAL`, `COMPLETED`, `REJECTED`, `FAILED`, `CANCELLED`, `ESCALATED` |
| `WAITING_APPROVAL` | `RUNNING`, `REJECTED`, `CANCELLED`, `ESCALATED` |
| `COMPLETED`, `REJECTED`, `FAILED`, `CANCELLED`, `ESCALATED` | 없음 |

`RUNNING → WAITING_APPROVAL`에는 plan hash가 있어야 한다. `WAITING_APPROVAL → RUNNING`에는 현재 `task_id`, `version`, `plan_hash`에 모두 묶인 `Approval`이 필요하다. Task 식별자 불일치, plan 변경, 오래된 version은 각각 명시적인 Approval 오류이며 검증 순서는 식별자, plan, version 순이다. plan은 `RUNNING` 또는 `WAITING_APPROVAL`에서만 교체할 수 있고 교체 자체도 version을 증가시킨다. `Approval.binding`은 이 세 값을 안정적인 tuple로 노출한다.

`WorkflowRun.start()`는 `CLASSIFYING`에서 시작한다. `advance()`는 `CLASSIFYING → ANALYZING → PLANNING → GOVERNING → EXECUTING → VERIFYING` 순서만 허용하며 `VERIFYING → EXECUTING` 직접 전이는 금지한다. `begin_agent_run()`이 호출 budget 한 회를 소비하면서 새 `AgentRun`과 갱신된 `WorkflowRun`을 함께 반환한다. `retry=True`는 `VERIFYING`에서만 가능하고 같은 원자적 연산 안에서 `EXECUTING`으로 이동한다. 마지막 slot은 정상 사용되며 이후 일반 호출과 재실행은 모두 `ExecutionBudgetExhaustedError`로 차단된다.

`AgentRun`은 `WorkflowRun.begin_agent_run()`으로만 새로 시작하며 해당 budget의 `budget_sequence`를 기록한다. `complete()`는 같은 `agent_id`의 결과로 한 번만 종료한다. 이 타입은 Agent 구현 계약이 아니라 orchestration이 소유하는 실행 이력이다.

## 의존성과 허용된 import 방향

현재 생명주기 모델은 Python 표준 라이브러리에만 의존한다. 이후 graph는 `agents`의 공통 인터페이스와 registry, `schemas`, 관측 인터페이스를 사용할 수 있지만 개별 agent 구현은 직접 import하지 않는다. SQLite, HTTP, Rich, LangGraph 타입은 생명주기 snapshot에 포함하지 않는다.

## 데이터 및 제어 흐름

Task 변경은 이전 snapshot을 보존한 채 새 version을 만든다. WorkflowRun이 phase와 budget을 추적하고 `begin_agent_run()`이 budget 소비와 개별 호출 기록 생성을 한 결과로 묶는다. 이후 supervisor는 이 값들을 함께 저장한 뒤 Agent를 호출하고 결과를 AgentRun에 반영한다.

## 설계 결정과 제약사항

모델은 frozen dataclass로 외부 변경을 막고 생성·복원 시에도 식별자, version, 도달 가능한 plan/status 조합, budget 범위와 UTC 절대시각 순서를 검증한다. `RECEIVED`는 version 1과 plan 없음만 허용하고 이후 status는 version 2 이상이어야 한다. 현재 동작에서 plan을 가진 `RUNNING`은 version 3 이상, `WAITING_APPROVAL`과 plan을 가진 terminal status는 version 4 이상이며, `COMPLETED`·`REJECTED`·`FAILED`·`ESCALATED`는 version 3 이상이다. 이 최소 하한 위의 version은 향후 합법적인 상태 변경을 막지 않도록 허용한다.

동일한 immutable `Task`와 `Approval`만으로는 프로세스 간 replay 소비 여부를 소유하지 않는다. 한 번만 승인되는 compare-and-transition은 Task 4 persistence가 `Approval.binding`과 현재 Task version을 같은 optimistic transaction에서 비교·저장해 보장해야 한다. Domain에는 global mutable consumed set을 두지 않는다.

## 테스트 전략

모든 status·phase 조합을 표 기반 단위 테스트로 검증한다. Approval의 task/version/plan 결합과 deterministic binding, 도달 불가능한 Task snapshot, plan 변경, 취소, budget과 AgentRun의 원자적 결합, DST fold, 모든 aggregate의 JSON snapshot 왕복·오류 정규화를 외부 I/O 없이 검증한다.

Task 4 persistence 통합 테스트는 같은 Approval의 동시·반복 전달에서 `binding`과 Task version을 optimistic transaction으로 비교해 정확히 한 요청만 `WAITING_APPROVAL → RUNNING`을 저장하고 나머지는 stale/idempotent 결과가 되는지 반드시 검증한다. 또한 `begin_agent_run()`이 반환한 WorkflowRun과 AgentRun을 한 transaction에 함께 저장하는지 검증한다.

## 변경 시 문서 갱신 조건

상태·phase 전이 행렬, 도달 가능성 하한, snapshot 필드, Approval 결합·멱등성 소유권, budget/AgentRun 원자성, 시각 비교, graph 구조, routing 정책 또는 종료 조건이 바뀔 때 갱신한다.
