# Kukie Agent

**쿠버네티스를 배우면서 안전하게 쓰는 AI 에이전트.**
자연어로 클러스터를 조회하고 변경하되, 변경은 반드시 사람이 승인한 뒤에만 실행된다.

```
사용자: nginx 파드가 자꾸 죽는데 왜 그래?
Kukie : CrashLoopBackOff — 로그를 보니 설정 파일 경로가 잘못됐습니다.
        (실행한 명령과 각 플래그의 뜻을 함께 보여줌)

사용자: 그럼 재시작해줘
Kukie : [승인 요청] kubectl rollout restart deployment/nginx -n study
        위험도: CAUTION · dry-run: 통과 · 판단 가이드: "롤링 재시작이므로 무중단…"
        → 승인 / 거절
```

## 왜 만들었나

kubectl 은 배우기 어렵고, 잘못 치면 서비스가 죽는다. LLM 에게 맡기면 편하지만 LLM 이 `delete` 를 부르는 것을 막을 방법이 프롬프트뿐이라면 운영에 쓸 수 없다.

Kukie 는 이 둘을 분리한다.

- **읽기**는 LLM 이 자유롭게 한다. 실행한 명령과 플래그 설명을 매번 보여 주므로 쓰면서 배운다.
- **변경**은 LLM 이 "하려는 시도" 자체가 트리거가 되어 코드 레벨 가드레일을 거친다. LLM 은 이 훅의 발동 여부에 관여할 수 없다.

## 핵심: 가드레일 파이프라인

변경 툴(`apply` `scale` `rollout restart` `delete`) 호출 하나가 아래 과정을 통과해야 kubectl 이 실행된다.

```
LLM 이 delete_resource(...) 호출
  │
  ├─ ① 위험도 판정 ─ 코드가 붙인 스티커(RISK_STICKERS) 조회. 미등록 툴은 fail-closed 로 거부
  ├─ ② Action Plan 생성 ─ 변경 1건 = 계획 1장 (Kukie API 서버에 기록, 상태 전이는 server 가 판정).
  │      대상이 ArgoCD 관리 리소스면 Plan·승인 카드에 경고를 싣는다 (막지는 않는다, kukie_agent/argocd.py)
  ├─ ③ dry-run ─ 실패하면 승인 요청 없이 종료
  ├─ ④ 판단 가이드 ─ 별도 LLM 이 "이 변경을 승인해도 되는가" 검토문 작성
  ├─ ⑤ 승인 대기 ─ 앱에 명령·매니페스트 미리보기·위험도·가이드를 띄우고 사람을 기다림
  ├─ ⑥ 승인 → 승인 시점의 tool/args/command 가 실행 시점과 동일한지 검증 후 정확히 1회 실행
  └─ ⑦ 결과 기록 ─ APPLIED / FAILED / UNKNOWN 을 Plan 에 남김
```

ArgoCD 가 관리하고 매니페스트 레포가 연결된 리소스는 kubectl 대신 **PR 길**로 간다 (`propose_git_change`, DURO-134).
같은 틀에서 ③ dry-run 자리가 "고칠 곳 찾기 + 렌더 비교 + 승인 전 PR 열기" 로, ⑥ 실행이 "PR 머지 → ArgoCD sync·health 지켜보기" 로
바뀐다. 승인 순간 길을 다시 판단해 레포 연결·Application·PR 이 바뀌었으면 실행하지 않고 STALE 로 닫는다.
레포 연결이 대화 도중 바뀌어도 AI 가 옛 답변을 따라가지 않게, 틀린 길의 호출은 훅이 `ModelRetry` 로 돌려보내고
연결이 바뀐 때만 기록 끝에 메모 한 줄을 넣는다 (DURO-135, `kukie_agent/gitops/memo.py`). instructions·툴 목록은 그대로다.

설계에서 지킨 원칙:

| 결정 | 주체 | 이유 |
|---|---|---|
| 어떤 툴을 쓸지, 왜 쓰는지 (intent) | LLM | LLM 이 잘하는 일 |
| kubectl 명령 문장 | 코드 | 승인 화면의 명령과 실행되는 명령이 같은 함수에서 나와야 한다 |
| 위험도 | 코드 | LLM 의 자기 신고를 믿지 않는다 |
| 실행 여부 | 사람 | 최종 책임은 사람에게 |
| 실행 결과·명령 (화면의 "사실 칸") | 코드 | LLM 이 결과를 지어내지 못하게 실행 기록에서 직접 채운다 |
| 결과 해석·다음 행동 제안 | LLM | 학습 가치 |

그 외:

- **재시도 안전성** — 승인 뒤 서버가 죽어도 `/resume` 으로 복구된다. 이미 실행된 계획은 저장된 결과를 돌려주고 kubectl 을 두 번 돌리지 않는다.
- **fail-closed** — Kukie API 서버 주소·내부 토큰 미설정, 위험도 미등록 툴 등은 전부 "조용히 통과" 가 아니라 거부다. kubectl 직전에 server 에 기록하지 못하면 실행하지 않는다.
- **자격증명** — 등록된 클러스터의 토큰·키는 kukie-server 가 암호화해 보관한다. agent 는 실행 직전에만 내부 API 로 받아 임시 kubeconfig(0600)를 만들고, 끝나면 지운다. 실행 중에 강제 종료돼 남은 파일은 다음 기동 때 지운다.

## 구조

```
kukie-electron (앱·웹) ──/member/*──▶ kukie-server (Kukie API 서버, Spring) ──▶ PostgreSQL (DB 는 이것 하나)
        │                                   ▲
        │                                   │ /users/me   토큰 검증 (Bearer 헤더 · 웹은 kukie_access 쿠키)
        │                                   │ /internal/* 저장·조회·권한 판단 (내부 토큰 + 입구에서만 사용자 Bearer)
        └──/api/*────▶ kukie agent (이 레포) ─┘
                              └──subprocess──▶ kubectl ──▶ 클러스터
```

이 레포는 에이전트 서버다. **agent 는 DB 를 갖지 않는다** — 대화·run·Action Plan·클러스터의 원본은 kukie-server 하나이고,
agent 는 워크플로(LLM 루프·가드레일·kubectl)만 실행하며 저장은 kukie-server 의 내부 API(`/internal/**`)로 한다 (DURO-113).
대화 만들기·목록·조회, 클러스터 등록·목록·수정·삭제, Action Plan 목록·상세는 클라이언트가 kukie-server 에 직접 부른다.
agent 에 남은 공개 API 는 `POST /conversations/{id}/chat`·`/approve`·`/resume` 과 `POST /clusters/{id}/test`(연결 확인)·`GET /clusters/{id}/overview`(대시보드 개요)·`GET /clusters/{id}/argocd/applications`(ArgoCD 앱 목록·상세·`…/rendered` 렌더 결과)·`GET /clusters/{id}/gitops/repos/{owner}/{repo}/tree`·`/file`(매니페스트 레포 보기) 뿐이다.
"이 사용자가 이 대화·클러스터에 이 일을 해도 되나" 도 server 가 판단한다.

세 레포 중 [kukie-server](https://github.com/durokukie/kukie-server) 는 공개, 앱은 비공개.

| 영역 | 파일 |
|---|---|
| 에이전트 정의, 스킬(Private·Shared 대화)별 툴 필터 | `kukie_agent/agent.py`, `kukie_agent/skills/` |
| 가드레일 훅, Action Plan, 판단 가이드 | `kukie_agent/guardrail/` |
| 읽기 툴 5종, 레포 읽기 툴 3종(`list_repo_dir`·`read_repo_file`·`render_app`), 변경 툴 4종, PR 길 변경 툴 `propose_git_change` | `kukie_agent/tools/` |
| kubectl 명령 조립·실행 (`shell=False`) | `kukie_agent/kubectl/` |
| FastAPI 서버, 대화 실행(chat·approve·resume)·클러스터 연결 확인·개요 | `kukie_agent/server.py`, `kukie_agent/conversations_api.py`, `kukie_agent/clusters_api.py` |
| 채팅방별 세션·잠금·재시작 복원 | `kukie_agent/conversations.py` |
| Kukie API 서버 내부 API 클라이언트 (저장·권한 판단, fail-closed) | `kukie_agent/server_api.py` |
| 실행 직전 임시 kubeconfig, 개요 응답 정리 | `kukie_agent/clusters/` |
| ArgoCD 관리 리소스 판별 (가드레일 경고·describe·앱 목록 API) | `kukie_agent/argocd.py` |
| GitOps PR 길 — 길 판단 · GitHub(브랜치·커밋·PR·머지) · sync 지켜보기, 승인 훅 | `kukie_agent/gitops/`, `kukie_agent/guardrail/gitops_hook.py` |
| 매니페스트 레포 고칠 곳 찾기 · 렌더 비교 | `kukie_agent/manifests/` |
| 인증 (kukie-server `/users/me`, fail-closed) | `kukie_agent/auth.py` |

**스택**: Python 3.14+ · [pydantic-ai](https://ai.pydantic.dev/) (에이전트 루프, 승인 훅 `ApprovalRequired`/`DeferredToolRequests`) · FastAPI · httpx · pytest · kind (E2E)

## 실행

```bash
git clone https://github.com/durokukie/agent.git && cd agent
uv sync                     # .python-version 의 Python 과 uv.lock 의 버전 그대로 .venv 에 깐다 (uv: https://docs.astral.sh/uv/)
source .venv/bin/activate   # 아래 명령은 이 가상환경에서
cp .env.example .env        # 모델·API 키 설정. 미설정 시 TestModel 로 키 없이 동작
python -m pytest -q         # 단위 테스트
uvicorn kukie_agent.server:app --port 8000   # 로컬 서버 (kukie-server 없이 flat 엔드포인트만 쓰려면 .env 에 KUKIE_DEV_AUTH=1)
```

의존성은 `uv add <패키지>`(개발용은 `uv add --dev`)로 더한다 — `pyproject.toml` 과 `uv.lock` 이 같이 바뀌니 둘 다 커밋한다. CI 는 `uv sync --locked` 라 둘이 어긋나면 멈춘다.

kind 클러스터 E2E: `KUKIE_E2E_CONTEXT=kind-<name> python -m pytest -m e2e` — flat 엔드포인트로 돌아 kukie-server 는 필요 없다.

**환경변수** (자세한 설명과 예시는 `.env.example`)

| 변수 | 뜻 |
|---|---|
| `KUKIE_SERVER_URL` | Kukie API 서버 주소 (예 `http://spring:8080`, 끝 `/` 는 뗀다). 토큰 검증(`/users/me`)과 내부 API 가 같은 주소를 쓴다 |
| `KUKIE_INTERNAL_TOKEN` | 내부 API 토큰 — kukie-server 의 `KUKIE_INTERNAL_TOKEN` 과 같은 값. 위 둘 중 하나라도 없으면 대화 경로는 503 |
| `KUKIE_DEV_AUTH`, `KUKIE_DEV_USER` | server 없이 로컬 개발 — flat 엔드포인트(`/session` `/chat` `/approve` `/resume`)만 열린다. 계획은 `~/.kukie/plans` 의 `.md` 로만 남는다 |
| `KUKIE_ACCESS_COOKIE` | 웹 access 쿠키 이름 (기본 `kukie_access`, kukie-server 와 같아야 한다) |
| `KUKIE_MODEL`, `KUKIE_GUIDANCE_MODEL`, 각 제공사 API 키 | 메인 에이전트·판단 가이드 모델 |

없어진 변수: `KUKIE_DATABASE_URL`(agent 에 DB 가 없다), `KUKIE_SECRET_KEY`·`KUKIE_ALLOW_LOCAL_CLUSTER`(자격증명 암호화와 kubeconfig 검사가 kukie-server 로 옮겨 가 그쪽 `KUKIE_CREDENTIAL_KEY`·`KUKIE_ALLOW_LOCAL_CLUSTER` 가 됐다), `KUKIE_MEMBER_URL`(`KUKIE_SERVER_URL` 이 대신한다).

**저장할 것이 바뀔 때** — agent 에는 DB 도 마이그레이션도 없다. 표 모양과 상태 전이 규칙은 kukie-server(Flyway, `/internal/**`)에서 먼저 바꾸고,
agent 는 `kukie_agent/server_api.py` 에 함수를 더한다. 테스트용 메모리 가짜 `tests/fake_server.py` 에도 같은 이름의 함수를 더한다 (`server_api.API` 목록).
