# Agent System

FastAPI로 사용자 요청, Alert, Ticket을 접수하고 LangGraph supervisor가 공통 `Agent`
interface를 통해 실행 대상을 동적으로 선택하는 단일 프로세스 MVP입니다. Task snapshot,
감사 event, 승인, WorkflowRun/AgentRun, 재시작 명령과 notification outbox는 SQLite에
영속화합니다.

## 설치

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)가 필요합니다.

```bash
uv sync
mkdir -p .data
cp .env.example .env
```

`.env`의 `UPSTAGE_API_KEY`를 실제 값으로 바꾸고 커밋하지 않습니다. 현재 model provider는
Upstage만 지원합니다. 기본 suite는 fake classifier/Agent를 주입하므로 외부 API를 호출하지
않습니다.

## 환경 변수

| 변수 | 필수/기본값 | 설명 |
| --- | --- | --- |
| `MODEL_PROVIDER` | 필수, `upstage` | model adapter 종류 |
| `MODEL_NAME` | 필수 | Upstage model 이름 |
| `UPSTAGE_API_KEY` | 필수 | 로그와 repr에 노출하지 않는 API key |
| `MODEL_TIMEOUT_SECONDS` | `30` | model 호출 timeout |
| `MODEL_MAX_RETRIES` | `2` | provider adapter retry 횟수 |
| `AGENT_SYSTEM_DB_PATH` | 필수 | 부모 디렉터리가 존재하는 SQLite 파일 경로 |
| `AGENT_MAX_RUNS` | `3` | Task당 Agent 실행 budget |
| `AGENT_QUEUE_CAPACITY` | `100` | process-local bounded queue 크기 |
| `AGENT_WORKER_COUNT` | `1` | background worker 수 |
| `AGENT_NOTIFICATION_LEASE_SECONDS` | `30` | outbox 전달 lease |
| `AGENT_NOTIFICATION_SEND_TIMEOUT_SECONDS` | `20` | sender timeout, lease보다 작아야 함 |
| `AGENT_NOTIFICATION_HEARTBEAT_SECONDS` | `5` | lease heartbeat, 두 주기가 lease보다 작아야 함 |

## 로컬 실행

```bash
set -a
source .env
set +a
uv run uvicorn agent_system.server:create_app_from_env --factory --host 127.0.0.1 --port 8000
```

Server factory가 설정 검증, Alembic migration, runtime/compiled graph/checkpointer와 FastAPI
lifespan을 조립합니다. 종료 시 새 명령 수락을 닫고 queue와 outbox를 drain한 뒤 SQLite
연결을 정리합니다.

## HTTP API

모든 명령 endpoint는 실행 완료가 아니라 durable 수락을 뜻하는 `202 Accepted`를
반환합니다. `GET`으로 최종 상태를 확인합니다. Schema에 없는 필드와 빈 문자열은 `422`,
없는 Task는 `404`, 오래된 version·plan 또는 terminal 충돌은 `409`입니다.

일반 Task 접수에는 선택적인 `Idempotency-Key`를 사용할 수 있습니다.

```bash
curl -i -X POST http://127.0.0.1:8000/v1/tasks \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: request-20260727-001' \
  -d '{"input":"checkout 장애 영향을 조사해 주세요."}'
```

Alert와 Ticket은 각각 `alert_id`, `ticket_id`가 멱등 identity입니다. 같은 identity와 같은
payload는 기존 Task를 replay하고 payload가 바뀌면 `409`입니다.

```bash
curl -i -X POST http://127.0.0.1:8000/v1/webhooks/alerts \
  -H 'Content-Type: application/json' \
  -d '{"alert_id":"alert-42","severity":"critical","message":"checkout 응답 없음"}'

curl -i -X POST http://127.0.0.1:8000/v1/webhooks/tickets \
  -H 'Content-Type: application/json' \
  -d '{"ticket_id":"ticket-9","subject":"로그인 실패","description":"신규 사용자가 로그인할 수 없습니다."}'
```

수락 응답의 `task_id`로 현재 persistence snapshot을 조회합니다.

```bash
curl -s http://127.0.0.1:8000/v1/tasks/<task_id>
```

변경 plan은 Governance를 통과한 뒤 `WAITING_APPROVAL`이 됩니다. 조회 응답의
`approval.task_version`과 `approval.plan_hash`를 그대로 승인 명령에 사용해야 합니다.
승인은 `task_id`, 해당 Task version, plan hash에 함께 묶이므로 plan이 바뀌거나 오래된
응답을 재사용하면 거부됩니다. `decision_id`는 결정의 안정적인 멱등 identity입니다.

```bash
curl -i -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/approval \
  -H 'Content-Type: application/json' \
  -d '{"decision_id":"decision-42","decision":"approve","task_version":4,"plan_hash":"sha256:<조회한-hash>"}'

curl -i -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/approval \
  -H 'Content-Type: application/json' \
  -d '{"decision_id":"decision-43","decision":"reject","task_version":4,"plan_hash":"sha256:<조회한-hash>","reason":"변경 창구가 닫혔습니다."}'
```

활성 Task 취소도 조회한 현재 version에 결합합니다.

```bash
curl -i -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/cancel \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":4,"reason":"운영자 요청"}'
```

Task 상태는 `RECEIVED`, `RUNNING`, `WAITING_APPROVAL`, `COMPLETED`, `REJECTED`,
`FAILED`, `CANCELLED`, `ESCALATED` 중 하나입니다.

## 저장, 재시작 복구와 알림

`AGENT_SYSTEM_DB_PATH`의 한 SQLite 파일을 애플리케이션 table과 LangGraph checkpoint가
공유하되 schema 책임은 분리합니다. 애플리케이션 연결은 WAL, foreign key와 busy timeout을
사용합니다. DB 파일과 함께 생성되는 `-wal`, `-shm` 파일은 실행 중 정상적인 SQLite
파일이며 저장소에 커밋하지 않습니다.

Runtime은 요청/승인/취소 의도를 먼저 durable command로 저장합니다. 프로세스가 승인 소비,
AgentRun 발급 또는 journal commit 뒤 중단돼도 다음 startup이 같은 Task ID checkpoint와
AgentRun idempotency key로 미완료 작업을 재개합니다. `WAITING_APPROVAL`은 사람 결정 없이
자동 실행하지 않습니다.

`WAITING_APPROVAL`, `COMPLETED`, `REJECTED`, `FAILED`, `ESCALATED`, `CANCELLED` 전이는
Task/event와 같은 transaction에서 outbox에 기록됩니다. 기본 adapter는 알림 ID, Task ID,
version, status, channel만 표준 logging에 남깁니다. 전달 실패는 Task 상태를 되돌리지 않고
lease와 bounded backoff로 재시도합니다. 실제 Slack/Email adapter는 아직 제공하지 않으며,
추가 adapter는 `NotificationSender.send()`를 구현하고 `notification_id`를 외부 멱등 key로
사용해야 합니다.

## 테스트와 품질 검사

```bash
uv run python -m unittest discover -v
uvx ruff check .
uvx ruff format --check .
uv run python -m compileall -q src tests
uv lock --check
uv build
uv run python -c 'import agent_system; from agent_system.server import create_app_from_env'
```

End-to-end suite는 임시 SQLite와 실제 FastAPI ASGI, bounded runtime, compiled LangGraph,
journal/checkpointer/outbox를 연결합니다. Fake classifier/Agent/notification sender로 승인 완료,
거절, retry 소진과 별도 application connection의 crash/restart를 결정 가능하게 검증합니다.

Migration만 별도 확인하려면 다음처럼 빈 임시 파일을 head까지 올릴 수 있습니다.

```bash
AGENT_MIGRATION_SMOKE="$(mktemp -d)/migration.sqlite3"
uv run python -c "from pathlib import Path; from agent_system.persistence import upgrade_database; upgrade_database(Path('$AGENT_MIGRATION_SMOKE'))"
```

## 범위와 교체 지점

현재 구현은 write concurrency가 낮은 단일 프로세스 MVP입니다. Queue, execution coordinator,
notification dispatcher가 process-local이므로 여러 worker/process로 수평 확장하지 않습니다.
실제 Kubernetes 변경 로직, Cluster Insight/Remediation/Governance 전문 Agent, Slack/Email,
Redis, Kafka, Celery와 Alert correlation은 범위 밖입니다.

향후 원격 Agent는 `agents.Agent` interface adapter로 교체합니다. Model provider는 LangChain
`BaseChatModel`, 저장소와 LangGraph saver는 `persistence`, 알림 채널은
`NotificationSender` seam 뒤에 있습니다. 운영 다중 writer 환경에서는 runtime 호출 계약을
유지한 채 SQLite store/checkpointer와 process-local coordinator를 PostgreSQL 및 분산 실행권
adapter로 교체해야 합니다. 모듈별 책임과 import 방향은 각 `MODULE.md`, 개발 규칙은
`AGENTS.md`를 참고합니다.
