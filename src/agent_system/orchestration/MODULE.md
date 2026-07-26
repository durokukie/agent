# Orchestration 모듈

## 목적과 책임

Task와 실행 기록의 생명주기, 동적 supervisor graph, 에이전트 선택 및 반복·종료 정책을 관리한다.

## 포함할 구현

framework에 독립적인 `Task`, `WorkflowRun`, `AgentRun`, `Approval`, `ExecutionBudget`과 상태·단계 전이 정책을 포함한다. WorkflowRun 내부의 private immutable issuance 값은 AgentRun 발급 identity를 보존한다. `_graph.py`는 provider 중립 요청 분류, stable agent ID routing, Governance·사람 승인, bounded retry와 terminal 정책을 실제 compiled LangGraph로 구성한다. 공통 오류, UTC 시각 검증과 typed snapshot parsing은 private `_support.py`에 두고 공개 이름은 module root에서 제공한다.

## 공개 인터페이스와 사용 방법

`Task.receive()`는 `RECEIVED` snapshot을 만들고 `transition()`은 아래 표의 상태 전이만 새 snapshot으로 반환한다. `cancel()`은 활성 Task를 `CANCELLED`로 만드는 명시적 연산이다. 모든 변경은 version을 1 증가시키고 timezone-aware 시각을 요구하며, 시각 순서는 DST fold와 무관하게 UTC 절대시각으로 비교한다. 모든 persisted aggregate의 `to_snapshot()`/`from_snapshot()`은 framework 타입 없는 JSON 호환 경계를 제공한다. 단, `AgentRun.from_snapshot()`은 소유 `WorkflowRun`을 함께 받아 실행 권한을 재검증한다.

| 현재 status | 허용하는 다음 status |
| --- | --- |
| `RECEIVED` | `RUNNING`, `CANCELLED` |
| `RUNNING` | `WAITING_APPROVAL`, `COMPLETED`, `REJECTED`, `FAILED`, `CANCELLED`, `ESCALATED` |
| `WAITING_APPROVAL` | `RUNNING`, `REJECTED`, `CANCELLED`, `ESCALATED` |
| `COMPLETED`, `REJECTED`, `FAILED`, `CANCELLED`, `ESCALATED` | 없음 |

`RUNNING → WAITING_APPROVAL`에는 plan hash가 있어야 한다. `WAITING_APPROVAL → RUNNING`에는 현재 `task_id`, `version`, `plan_hash`에 모두 묶인 `Approval`이 필요하다. Task 식별자 불일치, plan 변경, 오래된 version은 각각 명시적인 Approval 오류이며 검증 순서는 식별자, plan, version 순이다. plan은 `RUNNING` 또는 `WAITING_APPROVAL`에서만 교체할 수 있고 교체 자체도 version을 증가시킨다. `Approval.binding`은 이 세 값을 안정적인 tuple로 노출한다.

`WorkflowRun.start()`는 `CLASSIFYING`에서 시작한다. `advance()`는 `CLASSIFYING → ANALYZING → PLANNING → GOVERNING → EXECUTING → VERIFYING` 순서만 허용하며 `VERIFYING → EXECUTING` 직접 전이는 금지한다. 승인으로 새 `RUNNING` Task version이 생기면 `rebind_task()`가 `GOVERNING` 단계이면서 budget과 issuance가 아직 비어 있을 때만 WorkflowRun을 그 version에 다시 결합한다. `begin_agent_run()`이 호출 budget 한 회를 소비하면서 새 `AgentRun`과 갱신된 `WorkflowRun`을 함께 반환한다. 이때 갱신된 WorkflowRun에는 발급한 실행의 exact identity가 immutable `agent_run_issuances`에 budget 순서대로 추가된다. `retry=True`는 `VERIFYING`에서만 가능하고 같은 원자적 연산 안에서 `EXECUTING`으로 이동한다. 마지막 slot은 정상 사용되며 이후 일반 호출과 재실행은 모두 `ExecutionBudgetExhaustedError`로 차단된다.

`AgentRun`은 `WorkflowRun.begin_agent_run()`으로만 새로 시작하며 직접 생성은 `AgentRunOwnershipError`로 거부한다. 해당 budget의 `budget_sequence`를 기록하고, 복원할 때는 workflow/task 식별자에 더해 `agent_run_id`, `agent_id`, `phase`, Task version, budget sequence, UTC 시작 instant가 소유 WorkflowRun의 한 issuance와 모두 일치해야 한다. 따라서 consumed 범위 안의 그럴듯한 위조 실행도 복원할 수 없다. `complete()`는 같은 `agent_id`의 결과로 한 번만 종료한다. 이 타입은 Agent 구현 계약이 아니라 orchestration이 소유하는 실행 이력이다.

외부 입력은 `UserTaskInput`, `AlertInput`, `TicketInput`으로 구분한다. `RequestClassifier`는 추적 가능한 `RoutingDecision(request_kind, agent_id, action, reason, plan)`을 반환한다. `FakeRequestClassifier`는 외부 호출 없는 테스트 adapter다. `ChatModelRequestClassifier`는 주입된 LangChain `BaseChatModel`만 사용하고 응답 JSON을 Pydantic schema로 검증한다. provider SDK는 import하지 않는다. 변경 route에는 canonical SHA-256 hash를 제공하는 `ActionPlan`이 필수이고 read-only route에는 plan을 허용하지 않는다.

`OrchestratorService.start()`는 새 checkpoint thread를 시작하고 terminal 결과 또는 `ApprovalRequest` interrupt가 포함된 `OrchestrationResult`를 반환한다. 승인 요청은 전체 `ActionPlan`, `agent_id`, action과 Task version/hash를 노출한다. `resume()`은 필수 주입 seam인 `ApprovalConsumer`가 현재 Task/Approval binding을 원자적으로 소비해 반환한 exact successor를 검증한 뒤 `Command`를 전달한다. 같은 decision/binding의 `ALREADY_APPLIED`는 아직 승인 대기인 checkpoint에서 저장된 successor로 crash window를 복구하고, decision 또는 binding conflict는 거부한다. `get_result()`는 checkpoint 조회 seam이고 `recover()`는 저장된 next node에서 non-approval 실행을 계속한다. `start()`, `resume()`, active `recover()`는 모두 필수 주입 `ExecutionCoordinator`에서 동일한 `thread:{thread_id}` 실행권을 한 번만 획득한다. 시작은 checkpoint 소유권 검사 전부터, 재개는 Approval 소비 전부터, 복구는 claim 뒤 다시 읽은 authoritative checkpoint부터 graph 완료 또는 interrupt 반환까지 실행권을 유지한다. 정상·공개 오류·예기치 않은 오류·취소에서 해제하며 public facade를 서로 중첩 호출하지 않는다. 승인 대기와 terminal 복구는 claim이나 부수 효과 없이 현재 결과를 반환한다. 이미 사용된 thread의 새 시작, 승인 대기가 없는 thread의 resume, state가 없는 thread 조회·복구와 malformed checkpoint는 공개 orchestration 오류로 정규화한다. Runtime은 checkpointer, 승인 소비자, execution coordinator, UTC clock과 ID factory를 모두 주입한다. `FakeApprovalConsumer`와 `FakeExecutionCoordinator`는 test adapter다. Private compatibility adapter는 `BaseCheckpointSaver`의 async method가 지원되지 않을 때 같은 public sync method를 worker thread에서 호출하므로 orchestration은 `SqliteSaver` 구현을 import하지 않는다.

Task 6 runtime은 이미 저장한 exact `RECEIVED` v1을 `start(initial_task=...)`로 전달한다.
필수 `OrchestrationJournal` seam은 모든 Task version, WorkflowRun phase/rebind와 AgentRun
발급·완료를 persistence adapter에 전달하고 authoritative callback replay를 돌려받는다.
일반 callback은 상태 identity를 엄격히 검증하되 DB journal이 checkpoint보다 앞선 모든
node replay에서는 이미 저장된 Task version, phase/budget, AgentRun과 terminal 결과를
authoritative 값으로 돌려준다. 재실행 clock의 `updated_at`과 AgentRun `completed_at` 차이는
허용한다. `cancel(reason=...)`은 사유를 TASK_CANCELLED 감사 payload에 보존하고 활성 Task를
`CANCELLED`로 journal에 기록한 뒤 graph의
terminal checkpoint와 동기화하며 이후 `recover()`는 부수 효과 없이 같은 결과를 반환한다.

## 의존성과 허용된 import 방향

생명주기 모델은 Python 표준 라이브러리에만 의존한다. Graph는 LangGraph, LangChain Core의 `BaseChatModel`, Pydantic, `agents`의 공통 계약과 `AgentRegistry`에만 의존한다. 개별·전용 Agent 구현, `langchain_upstage`, SQLite·ORM 구현, persistence 모듈, HTTP/FastAPI, Rich를 import하지 않는다. LangGraph object와 model/Agent 내부 state는 Task·WorkflowRun·AgentRun snapshot에 포함하지 않는다.

## 데이터 및 제어 흐름

Task 변경은 이전 snapshot을 보존한 채 새 version을 만든다. Supervisor는 입력 분류 후 read-only route를 바로 실행 단계로 보내고, mutating route는 Governance를 통과한 plan만 Task에 기록해 `WAITING_APPROVAL` interrupt를 만든다. 승인 수락은 `task_id/task_version/plan_hash`를 모두 검증하고 persisted successor로 WorkflowRun을 재결합한 뒤 실행한다. AgentRequest context에는 승인된 plan snapshot/hash와 approval binding, routing action/agent, 승인·실행 Task version을 함께 전달하고, 공개 `idempotency_key`에는 exact `agent_run_id`를 전달한다. 거절 또는 잘못된 binding은 `REJECTED`로 끝난다.

Agent 호출은 `issue_agent_run` node에서 `begin_agent_run()`으로 budget과 open AgentRun snapshot을 먼저 checkpoint한 뒤 별도 `call_agent` node에서 비동기로 수행한다. 성공은 `COMPLETED`, 실패·예외·결과 agent ID 불일치·미등록 route는 표준 `FailureCode`와 완료된 AgentRun을 남기고 다음 budget slot으로 재시도한다. 마지막 slot 실패는 `ESCALATED`로 끝난다. 과거 또는 현재 AgentRun 복원은 issuance ledger가 포함된 WorkflowRun을 owner로 사용한다.

## 설계 결정과 제약사항

모델은 frozen dataclass로 외부 변경을 막고 생성·복원 시에도 식별자, version, 도달 가능한 plan/status 조합, budget 범위와 UTC 절대시각 순서를 검증한다. plan이 없는 Task는 현재 공개 연산으로 version을 반복 증가시킬 수 없으므로 아래의 정확한 조합만 허용한다.

| plan 없는 status | 허용 version |
| --- | --- |
| `RECEIVED` | `1` |
| `RUNNING` | `2` |
| `WAITING_APPROVAL` | 없음 |
| `COMPLETED`, `REJECTED`, `FAILED`, `ESCALATED` | `3` |
| `CANCELLED` | `2`, `3` |

plan을 가진 `RUNNING`은 version 3 이상, `WAITING_APPROVAL`과 plan을 가진 terminal status는 version 4 이상이다. 활성 상태에서 plan을 반복 갱신할 수 있으므로 이 하한보다 높은 planful version은 합법적이다.

`agent_run_issuances`는 JSON array로 직렬화되며 각 항목은 `agent_run_id`, `agent_id`, `phase`, `task_version`, `budget_sequence`, `started_at`을 가진다. 항목 수는 `budget.consumed`와 같고 sequence는 1부터 빠짐없이 증가해야 하며, 한 WorkflowRun 안에서 `agent_run_id`는 고유하다. 발급 시각은 WorkflowRun 실행 구간 안에서 순서대로 증가한다. 이 조건은 직접 생성과 snapshot 복원 모두에 적용된다.

동일한 immutable `Task`와 `Approval`만으로는 프로세스 간 replay 소비 여부를 소유하지 않는다. 한 번만 승인되는 compare-and-transition은 주입된 `ApprovalConsumer`가 `Approval.binding`, decision ID와 현재 Task version을 같은 원자적 연산에서 비교·저장해 보장한다. CAS와 LangGraph checkpoint commit은 한 DB transaction일 수 없으므로 같은 decision/binding replay는 exact persisted successor를 반환해야 한다. Graph는 이 successor를 재검증하며 자체 global mutable consumed set을 두지 않는다.

Graph 안에서는 checkpoint `thread_id`가 실행 소유권이다. 같은 thread를 새 Task에 재사용하지 않고 terminal 후 `Command(resume=...)` replay도 거부한다. 단일 프로세스 MVP에서 Task 6 composition root는 모든 service instance에 같은 `ExecutionCoordinator`를 주입한다. Coordinator는 operation이나 decision ID와 무관한 `thread:{thread_id}` key로 start/resume/recover 사이의 실행도 직렬화하고, claim은 취소와 모든 종료 경로에서 해제된다. 다중 프로세스에서는 process-local coordinator만으로 부족하므로 같은 key 계약을 구현하는 distributed lease/claim adapter가 별도로 필요하다. 외부 Agent adapter는 claim과 별개로 동일 `AgentRequest.idempotency_key`의 effect를 deduplicate해야 한다.

Classifier는 `BaseChatModel.ainvoke()`를 직접 await하며 provider 예외 상세를 `ClassificationError`로 제거하고 실행 취소는 전파한다. Snapshot 경계는 bool을 int로 받거나 coercion 가능한 문자열·tuple·mapping 유사값을 허용하지 않고 필드별 exact type을 요구한다. 주입 seam의 `ApprovalConsumeResult`와 `ExecutionClaimResult`도 생성 시 exact enum/aggregate/failure 조합을 검증하고 facade에서 다시 구성해 forged typed instance를 차단한다. 잘못된 collaborator 결과는 dereference 전에 cause-free `OrchestrationDependencyError`로 정규화한다. Classifier·Governance·Agent 구현에서 나온 예외 문자열은 결과에 노출하지 않고 안정적인 `FailureCode`로 정규화한다. Graph의 모든 cycle은 남은 `ExecutionBudget`을 감소시키므로 무한 loop가 없고 terminal Task에는 outgoing edge가 없다.

## 테스트 전략

모든 status·phase 조합을 표 기반 단위 테스트로 검증한다. Approval의 task/version/plan 결합과 deterministic binding, planless Task의 정확한 version 도달 가능성과 planful 반복 갱신, plan 변경, 취소, budget과 AgentRun의 원자적 결합·직접 생성 차단·exact issuance 기반 복원, historical/current 실행 복원, malformed issuance ledger, DST fold, 모든 aggregate의 JSON snapshot 왕복·오류 정규화를 외부 I/O 없이 검증한다.

Fake 기반 supervisor service 통합 테스트는 User/Alert/Ticket 분류, ChatModel JSON 검증, dynamic registry routing, read-only 완료, Governance 거절·예외, Agent FAILURE·예외·결과 불일치·missing route, 성공 재시도와 budget 소진 escalation을 검증한다. 단위 architecture test는 orchestration의 Upstage·SQLite/ORM·FastAPI·Rich import를 금지한다.

LangGraph 통합 테스트는 실제 `InMemorySaver`, `interrupt`, `Command`를 사용해 승인 대기, human 거절, stale/wrong/plan-changed approval, consume 직후 crash의 accept/reject healing, terminal replay 차단과 thread 재사용 차단을 검증한다. 공유 checkpointer·승인 소비자·coordinator를 쓰는 두 service의 경합에서 하나만 실행되는지도 확인한다. Blocking Agent로 외부 호출 전에 open AgentRun issuance가 checkpoint되는 순서와, 취소 후 SQLite 연결을 다시 열어 같은 issuance/budget/idempotency key로 복구하는 경로를 검증한다. 실제 compiled graph와 별도 SQLite 연결 두 개로 start/start, blocked resume/recover, `issue_agent_run` 직전과 열린 `call_agent`의 recover/recover를 경합시키며 모두 한 issuance/call/effect/budget과 안정적인 loser 오류만 허용한다. Start/resume/recover 취소 뒤 같은 coordinator로 재시도해 claim 해제를 검증하고, malformed state·collaborator result와 `ainvoke` 오류 정규화도 확인한다. 승인 대기와 terminal 복구는 claim 없는 무동작이어야 한다.

Task 4 persistence 통합 테스트는 같은 Approval의 동시·반복 전달에서 `binding`과 Task version을 optimistic transaction으로 비교해 정확히 한 요청만 `WAITING_APPROVAL → RUNNING`을 저장하고 나머지는 stale/idempotent 결과가 되는지 반드시 검증한다. 또한 `begin_agent_run()`이 반환한 issuance 포함 WorkflowRun과 AgentRun을 한 transaction에 함께 저장하고, 저장된 모든 historical/current AgentRun을 해당 WorkflowRun snapshot과 함께 복원하는지 검증한다.

Journal 통합 테스트는 모든 생명주기 callback 순서, DB-ahead Task/phase/issuance/completion/
terminal callback, persisted clock과 issuance의 authoritative replay, journal 오류 정규화,
취소 사유 감사, 승인 대기·실행 중 취소와 checkpoint terminal
동기화를 실제 compiled graph에서 검증한다.

## 변경 시 문서 갱신 조건

상태·phase 전이 행렬, plan 유무별 version 도달 가능성, snapshot 필드·복원 입력, 입력·분류·Governance·facade 계약, Approval 결합·멱등성 소유권, budget/AgentRun 원자성과 소유권, checkpoint thread 규칙, 시각 비교, graph 구조, routing·오류·재시도·종료 정책 또는 허용 import가 바뀔 때 갱신한다.
