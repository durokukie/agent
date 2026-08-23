# Kukie 에이전트 호출 흐름 (함수 단위)

> 기준: 가드레일 v2 코드 + PR #18/#19 (2026-08-18). "파드 뭐 떠있어?" 한 문장이 처리되는 전 과정.
> 범례: 실선 = 우리 코드 / 점선 = pydantic-ai 내부(우리가 안 짬) / 회색 = 미구현

## 1. 전체 흐름 (읽기 요청)

```mermaid
flowchart TD
    U["👤 사용자<br/>'파드 뭐 떠있어?'"] --> CLI

    subgraph CLI_["cli.py (미구현)"]
        CLI["main() 대화 루프<br/>입력 받기"]
        CLI --> R["router.pick_skill(msg, current)<br/>→ 학습 스킬 (코드가 결정, LLM 아님)"]
        R --> RUN["agent.run_sync(msg,<br/>deps=Deps(context, namespace, skill),<br/>output_type=skill.output_fn)"]
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
    V -->|"통과"| OUT["result.output (KukieResponse)<br/>narration + steps[command, output, explanations]"]
    OUT --> CLI2["cli.py 렌더링<br/>[학습 모드] + 명령 블록 + 설명 표"]
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

    subgraph H["guardrail/hook.py :: guardrail() — wrap 훅 (미구현)"]
        direction TB
        H1["① kubectl/assemble.assemble(tool_name, args)<br/>→ ['delete','deployment','nginx','-n','study']<br/>조립은 여기 1벌만 (재조립 금지)"]
        H1 --> H2["② RISK_STICKERS[tool_name]<br/>→ DESTRUCTIVE (미등록이면 fail-closed)"]
        H2 --> H3["③ ActionPlan.create_draft(tool, command, risk,<br/>skill, target, intent, effects, side_effects)<br/>→ ~/.kukie/plans/ap-YYMMDD-HHMM-tool.md (draft)"]
        H3 --> H4["④ run_kubectl(args, dry_run=True)<br/>plan.record_dry_run(out, ok)"]
        H4 -->|"실패"| H4F["plan.mark('failed')<br/>raise ToolFailed"]
        H4 -->|"성공"| H5["⑤ approval.cli_approve(plan, double=DESTRUCTIVE)<br/>승인 화면: 명령/왜/영향/부작용/등급"]
        H5 -->|"거절"| H5R["plan.mark('rejected')<br/>raise SkipToolExecution"]
        H5 -->|"승인 (사람 입력)"| H5A["plan.record_approval(mode)"]
        H5A --> H6["⑥ handler(조립 args)<br/>= 툴 본체 실행 (delete_resource)"]
        H6 --> H7["plan.record_result(out, ok)<br/>plan.mark('executed' | 'failed')"]
    end

    H7 --> L2["④ LLM 2차<br/>결과 보고 설명"]
    H4F --> L2
    H5R --> L2

    style H fill:#fff0f0,stroke:#c66
    style H5 fill:#ffe0e0,stroke:#c00,stroke-width:2px
```

훅 안에서 LLM 호출: **0번.** LLM은 훅 전(툴 호출)과 후(결과 설명)에만 등장.

## 3. 세션 시작 (cli.py, 미구현)

```mermaid
flowchart LR
    S["kukie chat"] --> K["kubeconfig 읽기<br/>context / namespace"]
    K --> C["사용자 확인<br/>'kind-dev / study 맞나요?'"]
    C -->|"y"| D["Deps(context, namespace,<br/>skill=DEFAULT_SKILL)"]
    D --> LOOP["대화 루프 진입<br/>(그림 1)"]
```

## 4. 함수 → 파일 매핑

| 단계 | 함수 | 파일 | 상태 |
|---|---|---|---|
| 세션·루프·렌더 | `main()` | `cli.py` | ❌ |
| 스킬 결정 | `pick_skill()` | `router.py` | ✅ (/mode + sticky) |
| 에이전트 설정 | `Agent(...)`, `add_target()`, `add_skill_prompt()` | `agent.py` | ✅ |
| 툴 등록 | `FunctionToolset(READ_TOOLS).filtered(...)` | `agent.py` | ✅ PR #19 (읽기 5종만) |
| 읽기 툴 5종 | `list_resources` 등 | `tools/read.py` | ✅ |
| 변경 툴 4종 | `delete_resource` 등 | `tools/mutate.py` | ❌ (훅 이후) |
| 명령 조립 | `assemble()` | `kubectl/assemble.py` | 부분 (apply TODO) |
| 실행 | `run_kubectl()` | `kubectl/runner.py` | ✅ |
| 훅 파이프라인 | `guardrail()` | `guardrail/hook.py` | ❌ (팀원) |
| Action Plan | `create_draft()`, `record_*()`, `mark()` | `guardrail/action_plan.py` | ✅ PR #18 머지 |
| 승인 | `cli_approve()` | `guardrail/approval.py` | ❌ (앱 전환 시 ApprovalRequired 재설계) |
| 응답 조립 (steps·설명) | `build_response_for()`, `collect_steps()` + `FLAG_GLOSSARY` | `response.py`, `glossary.py` | ✅ DURO-44 |
| 사전 미등록 플래그 로그 | `log_unregistered_flags()` | `validators.py` | ✅ DURO-44 |
| LLM 왕복 루프 | — | pydantic-ai 내부 | (우리 코드 아님) |

## 5. 누가 뭘 결정하나

| 결정 | 주체 | 시점 |
|---|---|---|
| 어떤 스킬 | 코드 (`router`) / 사용자 (`/mode`, 전환 y) | run 전 |
| 어떤 툴, 어떤 값, intent | LLM | ② (1차 호출) |
| kubectl 명령 문장 | 코드 (`assemble` / 툴 내부) | ③ |
| 위험도 | 코드 (`RISK_STICKERS`) | 훅 ② |
| 실행 여부 | 사람 (승인 화면) | 훅 ⑤ |
| 명령·결과 (steps 사실 칸) | 코드 (`collect_steps` — 실행 기록 그대로) | ⑤ |
| 설명 (explanations) | 코드 (`FLAG_GLOSSARY` 사전) | ⑤ |
| 해석 (narration, 전환 제안) | LLM | ④ (2차 호출) |
