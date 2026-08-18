# 읽기 툴 4종 구현 및 탈출구 제거 후속 정리

- 날짜: 2026-08-17
- PR: [#17](https://github.com/durokukie/agent/pull/17) (`feat/read-tools` → `develop`)
- 기반: 가드레일 v2 머지본 ([#16](https://github.com/durokukie/agent/pull/16))

## 무엇을

스캐폴드에서 `NotImplementedError`로 남아 있던 읽기 툴 4종을 구현하고, v2에서 결정된 "자유형 조회 탈출구 제거"의 후속 정리를 함께 반영했다.

| 파일 | 변경 |
|---|---|
| `kukie/tools/read.py` | `describe_resource` / `get_events` / `get_logs` / `explain_command` 구현 |
| `kukie/agent.py` | 기본 프롬프트 3번 문구 교체 (탈출구 → 안내 폴백) |
| `kukie/skills/base.py` | `COMMON_TOOLS`에서 `run_readonly_kubectl` 제거 |
| `kukie/skills/learning.py` | 낡은 주석 정정 |
| `tests/test_tools.py` | 탈출구 화이트리스트 테스트 → "LLM 직접 조립 경로 없음" 검증으로 교체 |

## 왜

가드레일 v2 결정에 따라 LLM이 kubectl args를 직접 조립하는 경로를 0개로 만들었다. 탈출구(`run_readonly_kubectl`)가 빠졌으므로 읽기는 전용 툴 5종만으로 커버해야 하고, 전용 툴로 안 되는 조회는 "사용자가 직접 실행할 명령을 안내 + 플래그 설명"으로 폴백한다 (프롬프트 규칙).

## 어떻게

### 공통 설계

- **LLM은 구조화 인자만 제출** (`kind`, `name`, `namespace` 등). kubectl 명령은 툴 함수 안의 코드가 조립한다.
- **`--context` 항상 명시** — 세션의 context를 매 호출에 붙여 엉뚱한 클러스터 접근을 막는다.
- **namespace 조립 헬퍼 `_ns()` 공유** — 명시값 > 세션 기본값. 대화만으로 대상을 바꾸지 않는다는 원칙을 코드로 유지.
- **docstring = 툴 선택 근거** — 모델이 언제 이 툴을 쓸지 docstring으로 판단하므로, "describe vs logs 차이"처럼 헷갈리는 지점을 docstring에 명시했다.

### 툴별 포인트

| 툴 | 조립되는 명령 | 설계 포인트 |
|---|---|---|
| `describe_resource` | `describe <kind> <name> -n <ns>` | 쿠버네티스가 보는 리소스 상태. 앱 로그는 `get_logs`로 — docstring에 구분 명시 |
| `get_events` | `get events --sort-by=.lastTimestamp -n <ns>` | 시간순 정렬 기본. `all_namespaces` 지원 |
| `get_logs` | `logs <pod> -n <ns> --tail=N [-c <container>] [--previous]` | CrashLoopBackOff 시 `previous=True`로 직전 컨테이너 로그. 멀티 컨테이너는 `container` 지정 |
| `explain_command` | `explain <resource.field>` | 순수 학습 툴. 결과를 연수생 눈높이로 재해설하라는 지시를 docstring에 포함 |

## 검증

### 명령 조립 8케이스 (실행 없이 조립 결과만 확인)

`run_kubectl`을 가로채 조립된 명령을 검사했다. 전부 기대대로 조립됨.

```
list_resources 기본      kubectl --context kind-dev get pods -n study -o wide
list_resources 전체ns    kubectl --context kind-dev get pods --all-namespaces -o wide
describe                 kubectl --context kind-dev describe pod nginx-1 -n study
describe ns지정          kubectl --context kind-dev describe deployment nginx -n prod
events                   kubectl --context kind-dev get events --sort-by=.lastTimestamp -n study
logs 기본                kubectl --context kind-dev logs nginx-1 -n study --tail=100
logs 크래시(previous)    kubectl --context kind-dev logs nginx-1 -n study --tail=50 -c app --previous
explain                  kubectl --context kind-dev explain deployment.spec.replicas
```

### 테스트

`.venv/bin/python -m pytest tests/` — 7 passed.

### 미수행

- **실클러스터 실행 검증** — 로컬에 kind/도커 미구성, kubeconfig의 DO 클러스터 2개는 삭제 상태(DNS 미해석). 조립 로직만 검증했고 실제 실행은 다음 PR부터 kind 환경에서 수행 예정.

## 결정 / 미결

| 항목 | 상태 |
|---|---|
| 툴 → 에이전트 등록 방식 | **미결.** `FunctionToolset`(스킬별 묶음)으로 가는 방향 — 후속 PR |
| 변경 툴 4종 본체 / 훅 파이프라인 | 후속 PR |
| 실행 검증 환경 | 오라클 VM 또는 로컬 도커에 kind 구성 필요 |

## 작업 흐름 메모

- 작업 도중 `develop`에 #16이 머지되어, 미커밋 변경을 `git stash` → `develop` fast-forward → 새 브랜치 `feat/read-tools` → `stash pop`으로 옮겼다. 충돌 없음.
- 이미 머지된 브랜치(`feat/guardrail-v2`) 위에 계속 쌓지 않고 최신 develop에서 새 브랜치를 따는 것을 기본으로 한다.
