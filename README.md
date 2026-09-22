# Kukie Agent

**쿠버네티스를 배우면서 안전하게 쓰는 AI 에이전트.**
자연어로 클러스터를 조회하고 변경하되, 변경은 반드시 사람이 승인한 뒤에만 실행된다.

```
사용자: nginx 파드가 자꾸 죽는데 왜 그래?
Kukie : [진단] CrashLoopBackOff — 로그를 보니 설정 파일 경로가 잘못됐습니다.
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
  ├─ ② Action Plan 생성 ─ 변경 1건 = 계획 1장 (DB + Markdown 사본)
  ├─ ③ dry-run ─ 실패하면 승인 요청 없이 종료
  ├─ ④ 판단 가이드 ─ 별도 LLM 이 "이 변경을 승인해도 되는가" 검토문 작성
  ├─ ⑤ 승인 대기 ─ 앱에 명령·매니페스트 미리보기·위험도·가이드를 띄우고 사람을 기다림
  ├─ ⑥ 승인 → 승인 시점의 tool/args/command 가 실행 시점과 동일한지 검증 후 정확히 1회 실행
  └─ ⑦ 결과 기록 ─ APPLIED / FAILED / UNKNOWN 을 Plan 에 남김
```

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
- **fail-closed** — 인증 서버 미설정, 암호화 키 없음, 위험도 미등록 툴 등은 전부 "조용히 통과" 가 아니라 거부다.
- **자격증명 암호화** — 등록된 클러스터의 토큰·키는 Fernet 으로 암호화해 저장하고, 실행 직전에만 임시 kubeconfig 로 푼다.

## 구조

```
kukie-electron (데스크톱 앱)  ──HTTP──▶  kukie agent (이 레포)  ──subprocess──▶  kubectl ──▶ 클러스터
                                              │
                                              ├─ 토큰 검증(Bearer 헤더 · 웹은 kukie_access 쿠키) ──▶ kukie-server (Spring, 회원·팀·권한)
                                              └─ 대화·계획·클러스터 ──▶ SQLite / PostgreSQL
```

이 레포는 가운데 에이전트 서버다. 세 레포 중 [kukie-server](https://github.com/durokukie/kukie-server) 는 공개, 앱은 비공개.

| 영역 | 파일 |
|---|---|
| 에이전트 정의, 스킬별 툴 필터 | `kukie/agent.py`, `kukie/skills/` |
| 가드레일 훅, Action Plan, 판단 가이드 | `kukie/guardrail/` |
| 읽기 툴 4종, 변경 툴 4종 | `kukie/tools/` |
| kubectl 명령 조립·실행 (`shell=False`) | `kukie/kubectl/` |
| FastAPI 서버, 대화·계획·클러스터 API | `kukie/server.py`, `kukie/*_api.py` |
| 클러스터 등록·암호화·임시 kubeconfig | `kukie/clusters/` |
| 인증·팀 권한 (Spring 연동, fail-closed) | `kukie/auth.py`, `kukie/membership.py` |
| 저장소 (SQLAlchemy) | `kukie/store/` |

**스택**: Python 3.10+ · [pydantic-ai](https://ai.pydantic.dev/) (에이전트 루프, 승인 훅 `ApprovalRequired`/`DeferredToolRequests`) · FastAPI · SQLAlchemy · cryptography · pytest · kind (E2E)

## 실행

```bash
git clone https://github.com/durokukie/agent.git && cd agent
python -m pip install -e '.[dev]'
cp .env.example .env        # 모델·API 키 설정. 미설정 시 TestModel 로 키 없이 동작
python -m pytest -q         # 단위 테스트
uvicorn kukie.server:app --port 8000   # 로컬 서버 (Spring 없이 쓰려면 .env 에 KUKIE_DEV_AUTH=1)
```

kind 클러스터 E2E: `KUKIE_E2E_CONTEXT=kind-<name> python -m pytest -m e2e`

**DB 표 모양을 바꿀 때** — 서버는 켜질 때 `kukie/store/migrations/versions` 의 리비전을 순서대로 적용한다 (Alembic). 모델(`kukie/store/models.py`)을 고쳤으면 리비전을 **같이** 만든다. 안 만들면 새 DB 에서만 맞고 이미 있는 DB 에는 안 나가며, `tests/test_migrations.py` 가 그 어긋남을 잡는다.

```bash
KUKIE_DATABASE_URL=sqlite:////tmp/fresh.db alembic upgrade head            # 지금 리비전까지 만든 빈 DB
KUKIE_DATABASE_URL=sqlite:////tmp/fresh.db alembic revision --autogenerate -m "무엇을 바꿨나"   # 모델과의 차이를 리비전으로
```

만들어진 파일을 읽고 다듬은 뒤(자동 생성은 초안이다) 커밋한다. 데이터를 옮기는 리비전은 손으로 쓴다 — `0002` 가 예.

