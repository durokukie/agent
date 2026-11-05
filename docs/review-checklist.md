# PR 리뷰 체크리스트 — Kukie 에이전트

자동 리뷰(`.github/workflows/claude-review.yml`)가 매 PR 에서 먼저 읽는 파일. 사람이 리뷰할 때도 같은 목록을 본다.
범용 리뷰어가 모르는 **이 레포만의 규칙**을 적는다. 일반적인 버그·스타일은 여기 적지 않는다.

## 🔴 머지 전에 반드시 — 가드레일 (안전)

- **훅 우회**: 변경 툴(`MUTATING_TOOLS`)이 `guardrail/hook.py` 의 `guardrail()` 을 거치지 않고 `run_kubectl` 을 부르는 경로가 생겼나. 새 툴을 추가했으면 `MUTATING_TOOLS`·`RISK_STICKERS` 에 등록됐나 (누락 시 fail-closed 로 `ToolFailed` 여야 함).
- **중복 실행**: 승인 재개가 kubectl 을 두 번 돌릴 수 있나. 훅의 승인 분기는 `execution_result` → `decision` → `approve_for_execution` → `mark_executing` 순서로 Plan 을 먼저 본다. 이 순서를 바꾸거나 건너뛰는 변경은 위험. 대화 경로에서는 `decision`·`start` 가 kukie-server 에 기록된 뒤에만 kubectl 을 부른다 — 기록 실패(`ServerError`)를 삼키고 실행으로 넘어가는 코드는 위험.
- **승인 = 실행 동일성**: `assemble()` 밖에서 kubectl args 를 조립하나 (`kubectl/assemble.py` 재조립 금지 원칙). 승인 화면의 command 와 실행 command 가 같은 함수·같은 입력에서 나와야 한다.
- **args 검증 우회**: 승인 후 `approve_for_execution` 의 tool/args/command/risk/target 비교를 약화시켰나. `apply_manifest` 는 원문 대신 `manifest_sha256` 으로 비교한다.
- **위험도 자기신고**: LLM 이 채우는 필드(narration, intent 등)로 위험도·승인 여부·실행 결과를 판단하는 코드가 생겼나. 위험도는 `RISK_STICKERS`, 실행 결과는 `KubectlResult`/Plan 만이 근거다.
- **Plan 상태 전이**: `DRAFT → WAITING_APPROVAL → APPROVED → EXECUTING → APPLIED → (EFFECT_VERIFIED)` 와 옆길(`REJECTED`·`EXPIRED`·`STALE`·`FAILED`·`UNKNOWN`) 외의 전이 (`EFFECT_VERIFIED` 는 기획 09, `STALE` 은 기획 08 — 아직 채우는 곳이 없다), 또는 `decision`/`execution_result` 를 지우거나 덮어쓰는 코드. 대화 경로의 전이는 kukie-server 의 plan 동작이 판정하고(허용 안 되면 409 `PLAN_STATE_CONFLICT`), Plan 의 원본도 kukie-server 다 — `.md` 사본은 없다. `guardrail/action_plan.py` 의 상태 검사는 run 에 속하지 않는 flat `/chat`·`/approve` 계획(파일이 원본)에만 쓰이고 server 와 같은 규칙이어야 한다.

## 🔴 머지 전에 반드시 — 응답·세션

- **사실 칸 오염**: `ToolStep.command/output/access` 를 LLM 인자에서 채우거나, `build_response_for` 의 `steps` 제외를 건드렸나 (DURO-44: 사실 칸은 코드가 채운다).
- **`collect_steps` 누락**: 툴이 `KubectlResult` 가 아닌 것을 반환하면 화면 블록에서 빠진다 (`isinstance` 검사). kubectl 을 실행하는 툴은 `KubectlResult` 를 그대로 돌려줘야 한다.
- **잠금 틈**: `server.py` 의 `processing` 검사와 `True` 설정 사이에 `await` 가 들어갔나. 엔드포인트가 `async def` 인가 (동기 `def` 는 스레드풀에서 돌아 원자성이 깨진다).
- **세션 상태 갱신 시점**: `history`/`pending`/`decisions` 를 run 성공(`_to_payload`) 전에 갱신하나. 실패 시 재시도(`/resume`)가 같은 결정을 재조립할 수 있어야 한다.
- **재시도 안전성**: 새 실패 경로가 생겼으면 `/resume` 으로 복구되나, 아니면 사용자가 `POST /session` 밖에 못 하나.

## 🔴 머지 전에 반드시 — 저장 (kukie-server 내부 API)

- **agent 의 저장소**: 대화·run·Plan·클러스터를 agent 가 직접 저장하는 코드(DB 드라이버, SQLite, 상태 파일)가 생겼나. agent 는 DB 를 갖지 않고 저장은 `server_api.py` 를 거쳐 kukie-server `/internal/**` 로만 한다 (DURO-113). flat 개발 엔드포인트의 `~/.kukie/plans` 만 예외다.
- **권한 판단**: 팀 소속·역할을 agent 가 다시 묻거나 판단하나, 또는 server 의 404·403 을 삼키고 진행하나. 대화 권한은 컨텍스트(`purpose=chat|approve`), 클러스터 권한은 연결 정보 호출이 본다.
- **사용자 토큰**: 입구가 아닌 내부 호출(run 갱신·중단, plan 동작, 클러스터 상태)에 사용자 Bearer 를 싣거나 그 토큰에 기대나. access token 은 30분이면 만료되어 run 도중 기록이 끊긴다.
- **fail-open**: `KUKIE_SERVER_URL`·`KUKIE_INTERNAL_TOKEN` 이 없거나 server 에 못 닿을 때 조용히 기록을 건너뛰고 진행하는 경로. 503 이어야 한다 — 예외는 kubectl 이 이미 돈 뒤의 결과 기록 실패(경고를 붙이고 run 종료 때 UNKNOWN)뿐이다.

## 🟡 고치면 좋음

- 새 툴에 `STEP_LABELS`(`response.py`) 항목이 없어 화면에 함수 이름이 그대로 뜬다.
- 새 kubectl 플래그가 `glossary.py:FLAG_GLOSSARY` 에 없다 (설명 칸이 빈다 — "배우면서" 가치).
- 스킬의 `extra_tools` 에 적은 이름이 `tools/` 카탈로그에 없다 (조용히 노출 안 됨).
- 테스트가 `agent.override(model=TestModel/FunctionModel)` 없이 실제 모델을 부른다, 또는 `PLAN_DIR` 을 `tmp_path` 로 안 바꿔 홈의 실제 계획서를 건드린다.
- `server_api.py` 에 함수를 더했는데 `server_api.API` 목록이나 `tests/fake_server.py` 의 가짜에 없다, 또는 `from kukie_agent.server_api import x` 로 가져다 쓴다 — 테스트의 가짜가 끼어들지 못해 진짜 server 를 부른다.
- `pydantic-ai` 의 private 속성(`_max_output_retries` 등)에 새로 의존한다 — 버전 올릴 때 깨진다.

## 보지 않아도 되는 것

- 포맷·네이밍·import 순서 (CI 가 안 잡는 건 리뷰에서도 안 잡는다).
- `docs/`·`*.md` 만 바뀐 PR (워크플로가 건너뛴다).
- 테스트 코드 안에서 일부러 규칙을 어기는 케이스 (예: 승인 없이 handler 호출해서 실패를 확인하는 테스트).

## 보고 형식

- 🔴 / 🟡 / 🟣(이 PR 이 만들지 않은 기존 버그) 로 시작.
- 근거는 `파일:줄` 로. 이름에서 추측한 동작 주장은 "확인 필요" 표시.
- 같은 PR 두 번째 리뷰부터는 새 커밋만 보고, 이미 단 코멘트를 반복하지 않는다.
- 고칠 게 없으면 한 줄로 끝. 칭찬·요약 없음.
