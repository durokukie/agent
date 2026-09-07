# 승인 흐름 검증

PR #41 / Issue #28은 최신 `develop`의 승인 구현을 유지하고 검증을 보완한다.
`/approve` 재개 실패 시 세션과 결정을 유지하며, `/resume`은 이미 저장된 실행 결과를 반환한다.
재실행 여부를 모르면 자동으로 mutation을 다시 실행하지 않는다.

## 검증 범위

| 경로 | 검증 위치 |
|---|---|
| 승인 DTO, 여러 카드의 승인·거절, 중복 결정, 동시 요청 차단 | `tests/test_server.py` |
| 네 mutation tool의 Hook 연결 및 승인 전 실행 차단 | `tests/test_agent.py` |
| DESTRUCTIVE 단일 승인·거절 및 Plan 최종 상태 | `tests/test_guardrail_integration.py` |
| dry-run 실패, 설명 오류, risk 누락 → 승인·실행 차단 | `tests/test_guardrail_integration.py` |
| dry-run 미지원·guidance 실패 → 원인 표시 후 승인, 보호 namespace의 risk 유지 | `tests/test_guardrail_integration.py` |
| mutation 성공·실패 후 모델 응답 실패 → HTTP 재개 → 저장 결과 반환, 실행 1회 | `tests/test_guardrail_integration.py` |
| 실제 클러스터의 apply·scale·restart·delete 승인/거절, Plan과 상태 일치 | `tests/e2e/test_guardrail_cluster.py` |
| apply의 명시 namespace 우선 적용·기본 namespace 미변경, dry-run/실행 stdin 일치 | `tests/e2e/test_guardrail_cluster.py` |

pytest 공통 `tests/conftest.py`에서 실제 LLM 요청을 차단한다. 테스트 파일을 하나만 실행해도 적용된다.
서버·Hook·Plan은 실제 구현을 쓰고, 통합 테스트는 모델과 kubectl 경계만 대체한다.
클러스터 E2E에서는 모델만 대체한다. 세션 기본 namespace와 요청 namespace는 서로 다른 격리 공간이다.
apply·scale·delete는 dry-run 성공과 guidance를, rollout restart는 dry-run 미지원 원인과 guidance unavailable 표시를 검증한다. Electron renderer 자체의 E2E는 이 저장소 범위 밖이다.

## 실행

```sh
uv run --extra dev pytest -q
```

`KUKIE_E2E_CONTEXT`가 없으면 실제 클러스터 테스트만 건너뛴다.
kind와 Docker가 준비된 환경에서는 별도 kubeconfig로 일회용 클러스터를 만든다.
아래 명령은 서브셸 안에서 실행되어 기존 kubeconfig와 환경변수를 보존한다.

```sh
(
  cluster_name="kukie-e2e-$(date +%s)-$$"
  e2e_dir=$(mktemp -d)
  export KUBECONFIG="$e2e_dir/config"
  export KUKIE_E2E_CONTEXT="kind-$cluster_name"
  export KUKIE_MODEL=test
  trap 'kind delete cluster --name "$cluster_name"; rm -rf "$e2e_dir"' EXIT
  kind create cluster --name "$cluster_name" --wait 120s &&
    uv run --extra dev pytest -q -m e2e
)
```

fixture는 kind 관리 목록 및 API server/CA 일치를 확인한 뒤 무작위 전용 namespace에서 테스트한다.
각 kubectl 호출에는 context를 명시하고, 테스트 후 namespace를 삭제한다.
CI의 별도 E2E job은 kind 클러스터 생성·테스트·항상 삭제를 수행한다.
