# Runtime 모듈

## 목적과 책임

HTTP와 CLI가 공유하는 application interface와 composition root다. 외부 명령을 먼저
SQLite authority에 저장하고 bounded background queue에서 orchestration을 실행하며,
프로세스 시작 시 미완료 Task를 복구하고 종료 시 소유 자원을 정리한다.

## 포함할 구현

`TaskApplication` 계약, `RuntimeApplication`, `build_runtime()`, 제출·승인·취소 명령과
조회 값, process-local worker queue를 포함한다. `SQLiteLifecycleJournal`은 graph의 Task,
WorkflowRun, AgentRun 변경을 짧은 transaction에 기록하고 같은 callback replay에는 먼저
저장된 authoritative snapshot을 반환한다. `SQLiteApprovalConsumer`는 승인·거절 CAS를,
`InProcessExecutionCoordinator`는 같은 checkpoint thread의 실행권을 adapter 계약에 맞춘다.
제출·승인·취소 의도는 `runtime_commands`에 먼저 기록한다. 승인 CAS 또는 journal commit 뒤
graph checkpoint 전 crash는 client retry 없이 startup pump가 저장된 exact command를 복원한다.
실패 command의 `attempt_count`와 `updated_at`은 재시작 뒤에도 같은 exponential backoff
eligibility를 계산하는 durable retry ledger다.

## 공개 인터페이스와 사용 방법

`build_runtime(settings, ...)`은 migration, store, checkpointer, classifier, Governance,
Agent registry와 orchestrator를 조립해 `RuntimeApplication`을 반환한다. 테스트와 다른 배포
구성은 classifier, Governance, registry, model factory, clock과 ID factory를 주입할 수 있다.
전송 adapter는 `start()`/`stop()` 수명 안에서 `submit()`, `get_task()`, `approve()`,
`cancel()`만 사용한다. 명령 반환은 실행 완료가 아니라 durable 수락 결과다.

## 의존성과 허용된 import 방향

composition root이므로 `config`, `models`, `agents`, `orchestration`, `persistence`를 조립할
수 있다. FastAPI와 Rich는 import하지 않으며 HTTP와 CLI가 runtime을 import한다. 외부
adapter의 동기 SQLite 호출은 `asyncio.to_thread()` 뒤에 둔다.

## 데이터 및 제어 흐름

제출은 canonical payload fingerprint와 namespace별 identity를 사용해 `RECEIVED` v1, 요청
event와 START command를 한 transaction에 commit한 다음 stable Task ID thread로 queue에
넣는다. 승인·취소도 semantic command fingerprint와 payload를 commit한 뒤 수락한다. worker는
start/recover/resume/cancel을 호출하며 성공한 command만 완료 처리한다. 실패는 stable code와
attempt를 남겨 최대 5분의 exponential backoff 뒤 다시 실행한다. exact client replay는
backoff를 기다리지 않는 명시적 재시도이고, 다른 worker의 완료는 아직 eligible하지 않은
command를 다시 실행하지 않는다. 조회는 SQLite의
Task, event, workflow와 AgentRun에서 승인·결과 metadata를 조립한다. 시작 시 terminal을
제외한 recovery 후보를 읽어 `RECEIVED`는 재시작하고, 승인 대기는 사람 입력을 기다리며,
나머지는 Task ID checkpoint에서 복구한다. 종료는 queue를 drain하고 checkpointer와 store를
각각 한 번 닫는다.

## 설계 결정과 제약사항

HTTP handler는 graph를 직접 실행하지 않는다. queue는 설정된 용량으로 제한되고 exact
Task/command fingerprint만 process 안에서 합친다. 같은 Task의 다른 pending command는
순서 충돌로 거부한다. queue가 가득 차도 이미 commit한 durable command의 수락 응답은
유지하고 worker pump가 용량이 생길 때 다시 올린다. migration 전 command row 없는 recovery
후보도 deferred queue에 보존하므로 startup은 queue pressure로 실패하지 않는다. 승인·취소가
checkpoint terminal 반영 뒤 command 완료 전 중단된 경우 Task ID recover로 조정한 뒤
recover 결과와 SQLite Task snapshot이 같고 decision ID·승인 결과 또는 취소 사유의
authoritative event provenance가 command와 일치할 때만 command를 완료한다. 외부 webhook
identity와 일반 idempotency key는 서로
다른 namespace를 사용한다. SQLite journal의 callback identity는 aggregate의 상태/version/
phase/budget과 version별 event가 뜻하는 Task status에 결합하며, 실제 `TASK_RECEIVED` request
payload도 일치해야 한다. 재실행마다 달라질 수 있는 `updated_at`은 authoritative 저장값으로
복원한다. 다중 프로세스 배포에서는 queue와 execution coordinator를 별도 adapter로 교체해야
한다.

`stop()`은 시작 즉시 admission을 닫고 예약된 retry wakeup을 취소·대기한 다음 기존 queue를
drain한다. durable commit과 enqueue 사이에 stop이 시작된 명령은 SQLite에 pending으로 남아
다음 startup에서 복구되며, worker sentinel 뒤에는 새 work를 enqueue하지 않는다. durable
command와 command row 없는 recovery가 함께 밀릴 때는 매 worker 완료마다 pump 우선순위를
번갈아 적용해 한쪽의 지속적인 starvation을 막는다.

## 테스트 전략

임시 SQLite와 orchestration fake로 durable-before-queue, webhook replay/conflict, recovery,
승인·거절, 취소, 자원 종료를 검증한다. 실제 FastAPI ASGI, compiled LangGraph, SQLite
checkpointer와 fake classifier/Governance/Agent를 함께 사용해 read-only 완료, 사람 승인,
취소와 durable journal replay를 수직 통합 테스트한다.

## 변경 시 문서 갱신 조건

application 계약, 조립 대상, queue/recovery 정책, Task thread identity, journal/승인 adapter,
멱등성 namespace, 자원 수명 또는 runtime import 방향이 바뀔 때 갱신한다.
