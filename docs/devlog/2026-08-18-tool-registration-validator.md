# 읽기 툴 등록 및 설명 필드 강제 검증기

- 날짜: 2026-08-18
- PR: [#19](https://github.com/durokukie/agent/pull/19) (`pty1381/duro-42-tool-registration-validator` → `develop`)
- Linear: DURO-42 (부모 DURO-41 마일스톤 1)
- 기반: #17(읽기 툴) 머지본

## 무엇을

지금까지 만든 읽기 툴 5종을 에이전트에 연결하고, 응답의 설명 필드를 코드로 강제했다. 이 PR로 `agent.run()` 한 줄이 "LLM이 툴 고르고 → 실행 → 설명 붙여 응답" 흐름을 처음으로 돈다 (TestModel 기준).

| 파일 | 변경 |
|---|---|
| `kukie/agent.py` | `FunctionToolset(READ_TOOLS).filtered(스킬 필터)` → `toolsets=[...]`. 모델을 `KUKIE_MODEL` 환경변수로 (기본 `test`) |
| `kukie/validators.py` | `enforce_explanations` 본체 — 설명 비면/플래그 누락 시 `ModelRetry` |
| `tests/test_agent.py` | 신규 8개 — 스킬별 툴 노출, 변경 툴 미노출, 검증기 반려/통과 |

## 왜

- **툴 등록**: `agent.py`에 TODO로 비어 있어 LLM이 툴 목록을 아예 못 보던 상태. 이게 채워져야 "부품은 있는데 조립 안 된 자동차"가 굴러간다.
- **설명 강제**: "배우면서"는 프롬프트로 부탁만 하면 모델이 가끔 빼먹는다. `output_validator`로 걸면 빠질 수 없다 — 서비스 정체성을 신뢰가 아니라 코드로 보장.
- **모델 환경변수**: `Agent("anthropic:...")`은 import 시점에 API 키를 요구해 테스트·CI가 전부 깨진다. 기본 `test`로 두면 키 없이 돌고, 실제 실행 때만 `KUKIE_MODEL` 지정.

## 어떻게

### 툴 등록 (3단계)

```python
_read_toolset = FunctionToolset(READ_TOOLS)                       # ① 함수 5개를 툴셋으로
toolset = _read_toolset.filtered(                                 # ② 스킬 필터
    lambda ctx, tool_def: tool_def.name in ctx.deps.skill.allowed_tools)
agent = Agent(MODEL, toolsets=[toolset], ...)                      # ③ 에이전트에 꽂기
```

`Deps.skill` 하나 넣으면 프롬프트(`add_skill_prompt`)·툴(`filtered`)·응답형식(`output_type`) 세 요소가 함께 갈아끼워진다 — "스킬 = 3요소" 설계가 코드로 성립.

**변경 툴은 등록하지 않았다.** 훅 본체가 `NotImplementedError`라 지금 등록하면 승인 없이 delete가 나갈 수 있다. 마일스톤 2에서 훅과 함께 추가.

### 설명 강제 (2규칙)

1. 명령을 실행한 step에 `explanations`가 비면 → `ModelRetry("… 채워라")`
2. 명령의 플래그(`-n`, `--tail` 등)가 `explanations`의 `field` 어디에도 없으면 → 누락 목록을 명시해 `ModelRetry`

`kubectl`, `--context <값>`은 세션 정보라 설명 대상에서 제외. steps가 비어 있는(개념 설명만 한) 턴은 검사하지 않는다.

반려 메시지 실례:
```
step 1 ('kubectl --context kind-dev logs nginx-1 -n study --tail=50 -c app --previous')의
explanations에 다음 플래그 설명이 빠졌다: --previous, --tail, -c. …
```
빠진 것을 콕 집어 주므로 모델이 그 부분만 보완한다.

## 검증

- `pytest tests/` — **15 passed** (기존 7 + 신규 8)
- 스킬별 LLM 노출 툴 (TestModel `last_model_request_parameters`로 확인):

| 스킬 | 노출 툴 |
|---|---|
| 학습 | list_resources, explain_command |
| 진단 | + describe_resource, get_events, get_logs |
| 실습 | + describe_resource (변경 툴 0 — 의도) |

- 실제 LLM 눈 검증은 미수행 — API 키 세팅 후 CLI PR(DURO-43)에서

## 결정 / 미결

| 항목 | 상태 |
|---|---|
| 모델 지정 방식 | `KUKIE_MODEL` 환경변수, 기본 `test`. Model Adapter(Upstage 교체 경계)는 후속 |
| 검증기 한계 | 기계적 검사(빈 값·플래그 누락)까지. 설명의 정확성은 코드로 판정 불가 — 사용자 검증(1-1, 2-2) 몫 |
| 변경 툴 등록 | 훅 완성 후 (마일스톤 2) |

## 작업 흐름 메모

- Linear 이슈(DURO-42) 먼저 → 브랜치명에 `duro-42` 포함 → PR 자동 연결. 이 순서를 이제 기본으로.
- 브랜치명은 Linear가 준 형식을 따르되 한글은 영문으로 축약 (`pty1381/duro-42-tool-registration-validator`).
