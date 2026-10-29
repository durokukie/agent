# Kukie 에이전트 호출 흐름 (함수 단위)

> 기준: 가드레일 v2 코드 + PR #18/#19 (2026-08-18). "파드 뭐 떠있어?" 한 문장이 처리되는 전 과정.
> 범례: 실선 = 우리 코드 / 점선 = pydantic-ai 내부(우리가 안 짬) / 회색 = 미구현

## 1. 전체 흐름 (읽기 요청)

```mermaid
flowchart TD
    U["👤 사용자<br/>'파드 뭐 떠있어?'"] --> CLI

    subgraph CLI_["server.py — 로컬 FastAPI (DURO-49)"]
        CLI["POST /chat {text}<br/>(승인 대기 중이면 409)"]
        CLI --> R["session.skill<br/>→ 대화의 공개 범위로 정한 Private / Shared (대화 안에서 고정, LLM 아님)"]
        R --> RUN["agent.run(msg,<br/>deps=Deps(context, namespace, skill),<br/>output_type=[skill.output_fn, DeferredToolRequests],<br/>message_history=session.history)"]
    end

    RUN --> PA

    subgraph PA["pydantic-ai 내부 루프 (우리 코드 아님)"]
        direction TB
        P1["① 프롬프트 조립<br/>BASE_PROMPT + add_target() + add_skill_prompt()<br/>+ 툴 목록 (filtered → 스킬 허용 툴만)"]
        P1 --> L1["② LLM 1차 호출<br/>'어떤 툴 쓸까?'"]
        L1 -->|"list_resources(kind='pods')"| T
        T["③ 툴 실행"] --> L2["④ LLM 2차 호출<br/>(툴 결과 첨부)<br/>'결과 보고 설명 작성'"]
        L2 -->|"툴 더 쓸래"| T
        L2 -->|"해석 칸만: narration 등"| V["⑤ build_response (response.py)<br/>코드가 조립 — LLM 스키마엔 steps 없음<br/>· command/output/access ← ToolReturnPart(KubectlResult)<br/>· explanations ← FLAG_GLOSSARY 사전<br/>· 사전 미등록 플래그는 로그 (validators)"]
    end

    T -.->|"실제 호출"| RT

    subgraph RT["tools/read.py"]
        RT1["list_resources(ctx, kind, namespace)"]
        RT1 --> NS["_ns(ctx, namespace)<br/>-n 조립: 명시값 > 세션 기본값"]
        NS --> RK["kubectl/runner.run_kubectl(args, context)<br/>subprocess, shell=False"]
        RK --> KR["KubectlResult<br/>command / stdout / stderr / success"]
    end

    KR -.-> T
    V -->|"통과"| OUT["result.output (KukieResponse)<br/>narration + suggested_next_action<br/>+ steps[command, output, explanations]"]
    OUT --> CLI2["server._to_payload → {kind: answer}<br/>앱이 답변 + 명령 블록 + 설명 표 렌더링"]
    CLI2 --> U

    style CLI_ fill:#f5f5f5,stroke:#999,stroke-dasharray: 5 5
    style PA fill:#eef4ff,stroke:#7a9cc6
    style V fill:#fff3e0,stroke:#e0a040
```

## 2. 변경 요청일 때 — ③에 훅이 끼어듦

```mermaid
flowchart TD
    L1["② LLM 1차<br/>delete_resource(kind, name, ns,<br/>intent, expected_effects, side_effects)"]
    L1 -->|"툴 호출 = 트리거 🔔"| H

    subgraph H["guardrail/hook.py :: guardrail() — ApprovalRequired 2-pass"]
        direction TB
        H1["① 실행 args 정규화·보관<br/>apply_manifest는 원문 대신 manifest_sha256"]
        H1 --> H2["② RISK_STICKERS[tool_name]<br/>→ DESTRUCTIVE (미등록이면 fail-closed)"]
        H2 --> H2A["ArgoCD 판별 argocd.inspect(target)<br/>관리 리소스면 경고(level·warning) — 못 하면 None, 막지 않는다"]
        H2A --> H3["③ ActionPlan.create_draft(tool, args, command, risk,<br/>skill, target, intent, effects, side_effects, argocd)<br/>→ 대화 경로: kukie-server POST /internal/runs/{runId}/plans (DRAFT, id=UUID·code=ap-YYMMDD-HHMM-tool)<br/>→ flat 개발 엔드포인트: ~/.kukie/plans/ap-YYMMDD-HHMM-tool.md"]
        H3 --> H4["④ run_kubectl(command, context, dry_run=True)<br/>plan.record_dry_run(status, stdout, stderr)"]
        H4 -->|"실패"| H4F["기록하는 자리에서 FAILED + DRY_RUN_FAILED<br/>raise ToolFailed"]
        H4 -->|"성공·미지원"| H5["⑤ plan.offer(decision guidance) → WAITING_APPROVAL<br/>raise ApprovalRequired(plan_id)"]
        H5 --> P["DeferredToolRequests<br/>metadata(plan_id)"]
        P --> E["Electron 승인 화면<br/>apply_manifest: 민감값을 가린 구조적 preview + SHA-256"]
        E --> A["POST /approve<br/>{call_id, approved}"]
        A --> D["DeferredToolResults<br/>ToolApproved | ToolDenied"]
        D -->|"ToolApproved"| H5P["⑥ call_id로 기존 Plan 조회"]
        H5P -->|"execution_result 있음<br/>(재개 실패 뒤 /resume 재시도)"| H5X["저장된 결과를 KubectlResult로 반환<br/>kubectl 다시 안 돌림 (DURO-66)"]
        H5P -->|"decision만 있고 실행 기록 없음"| H5U["plan.mark_unknown() → UNKNOWN<br/>ToolFailed: 실행 여부 불명<br/>클러스터 직접 확인"]
        H5P -->|"첫 승인"| H5A["tool/args/command/risk/target 검증<br/>single decision 기록"]
        D -->|"ToolDenied"| H5R["Plan REJECTED (1회)<br/>handler 실행 안 함"]
        H5A --> H6["⑦ plan.mark_executing() → EXECUTING<br/>기존 handler(args) 정확히 1회"]
        H6 --> H7["⑧ execution_result와<br/>APPLIED | FAILED(+failure_reason) 상태를 함께 기록"]
    end

    H7 --> L2["④ LLM 2차<br/>결과 보고 설명"]
    H5X --> L2
    H5U --> L2
    H4F --> L2
    H5R --> L2
    L2 -.->|"run 실패 시 세션·티켓 유지<br/>503 RESUME_RETRYABLE → POST /resume"| A

    style H fill:#fff0f0,stroke:#c66
    style H5 fill:#ffe0e0,stroke:#c00,stroke-width:2px
```

성공한 dry-run 뒤 승인 전에 decision guidance용 LLM 호출이 한 번 발생한다.

## 3. 세션 시작 (server.py)

```mermaid
flowchart LR
    S["POST /session"] --> K["kubectl/config.read_kubeconfig()<br/>current-context / namespace"]
    K --> C["사용자 확인<br/>'kind-dev / study 맞나요?'"]
    C -->|"y"| D["Deps(context, namespace,<br/>skill=SHARED)<br/>로컬 개발엔 방이 없다"]
    D --> LOOP["대화 루프 진입<br/>(그림 1)"]
```

## 4. 함수 → 파일 매핑

| 단계 | 함수 | 파일 | 상태 |
|---|---|---|---|
| 세션 시작 | `start_session()`, `read_kubeconfig()` | `server.py`, `kubectl/config.py` | ✅ DURO-49 |
| 채팅·결과 분기·승인 재개 | `chat()`, `_to_payload()`, `approve()` | `server.py` | ✅ DURO-49 + #27 승인 연결 |
| 렌더링 | — | 앱 (2단계) | ❌ |
| 스킬 결정 | `skill_for(room.shared)` | `skills/__init__.py`, `conversations.py` | ✅ DURO-100 (공개 범위로 고정, 모드 없음) |
| 에이전트 설정 | `Agent(...)`, `add_target()`, `add_skill_prompt()` | `agent.py` | ✅ |
| 툴 등록 | `FunctionToolset([*READ_TOOLS, *MUTATE_TOOLS, *GITOPS_TOOLS]).filtered(_only_skill_tools)` | `agent.py` | ✅ 스킬별 필터; 변경 4종·PR 길 1종은 Shared 만 + Hook |
| 읽기 툴 5종 | `list_resources` 등 | `tools/read.py` | ✅ |
| 변경 툴 4종 | `delete_resource` 등 | `tools/mutate.py` | ✅ Shared 대화에만 노출, 가드레일 훅 대상 |
| PR 길 변경 툴 | `propose_git_change` — 승인 전 PR, 승인 = 머지, sync·health 지켜보기, 승인 순간 STALE 판단 | `tools/gitops.py`, `guardrail/gitops_hook.py`, `gitops/` | ✅ DURO-134 |
| 명령 조립 | `assemble()` | `kubectl/assemble.py` | 부분 (apply TODO) |
| 실행 | `run_kubectl()` | `kubectl/runner.py` | ✅ |
| 훅 파이프라인 | `guardrail()` | `guardrail/hook.py` | ✅ 2-pass 승인·실행·기록 |
| Action Plan | `create_draft()`, `offer()`, `approve_for_execution()`, `mark_executing()`, `record_execution()` — 대화 경로는 kukie-server 의 plan 동작, 다음 상태는 server 가 정한다 | `guardrail/action_plan.py` → `server_api.py` | ✅ DURO-113 |
| 저장·권한 판단 | 대화 컨텍스트·run·plan·클러스터 연결 정보 — kukie-server `/internal/**` (agent 는 DB 없음) | `server_api.py` | ✅ DURO-113 |
| 클러스터 | 실행 직전 `GET /internal/clusters/{id}/connection` → 임시 kubeconfig (등록·암호화는 kukie-server) | `clusters/` | ✅ |
| 승인 | `DeferredToolRequests → Electron → /approve → DeferredToolResults` | `server.py`, `guardrail/approval.py` | ✅ #27 |
| 응답 조립 (steps·설명) | `build_response_for()`, `collect_steps()` + `FLAG_GLOSSARY` | `response.py`, `glossary.py` | ✅ DURO-44 |
| 사전 미등록 플래그 로그 | `log_unregistered_flags()` | `validators.py` | ✅ DURO-44 |
| LLM 왕복 루프 | — | pydantic-ai 내부 | (우리 코드 아님) |

## 5. 누가 뭘 결정하나

| 결정 | 주체 | 시점 |
|---|---|---|
| 어떤 스킬 | 코드 (`skill_for` — 방의 공개 범위) | 방을 열 때 |
| 어떤 툴, 어떤 값, intent | LLM | ② (1차 호출) |
| kubectl 명령 문장 | 코드 (`assemble` / 툴 내부) | ③ |
| 위험도 | 코드 (`RISK_STICKERS`) | 훅 ② |
| 실행 여부 | 사람 (승인 화면) | 훅 ⑤ |
| 명령·결과 (steps 사실 칸) | 코드 (`collect_steps` — 실행 기록 그대로) | ⑤ |
| 설명 (explanations) | 코드 (`FLAG_GLOSSARY` 사전) | ⑤ |
| 해석 (narration, 다음 행동 안내) | LLM | ④ (2차 호출) |

`suggested_next_action: str | None = None`은 현재 답변과 실행 결과에 근거한 짧은 다음 행동 안내다.
추가 행동이 필요하지 않으면 `null`이다. 제안 자체는 작업을 실행하거나 사용자 승인을 대신하지 않는다.
변경 작업의 승인·거절은 기존 `/approve` 절차를 따른다.

학습/진단/실습 모드는 없앴다 (DURO-100). Private 대화는 읽기 5종만, Shared 대화는 읽기 5종 + 변경 4종을 받는다.
공개 범위는 대화 안에서 바뀌지 않으므로 한 대화의 툴 목록·응답 스키마·지시문은 턴마다 같다 (프롬프트 캐시).
`/mode …` 입력은 모드 버튼이 남은 옛 앱을 위해서만 받아 `{kind: "mode", known: false}` 로 답하고 아무것도 바꾸지 않는다.

API 변경(#48): 기존 `suggested_transition`을 참조하는 소비자는 `suggested_next_action`으로 변경해야 한다.
구 필드는 응답에 함께 제공하지 않는다.
