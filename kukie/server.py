"""로컬 서버 — 앱이 agent.run 을 HTTP 로 부르는 진입점 (DURO-49).

사용자 컴에서 앱과 함께 돈다 (DURO-46 구조 1). 원격 아님, 인증 없음, 단일 사용자.
앱(Node)은 파이썬을 직접 못 부르므로 이 서버가 "에이전트의 입" 역할을 한다.

엔드포인트 3개:
  POST /session  kubeconfig 에서 대상(context/namespace)을 읽어 세션 시작
  POST /chat     한 턴 실행. 결과가 답변이면 answer, 승인 대기면 approval
  POST /approve  승인 버튼 결과로 run 재개 (2차). 결과 분기는 /chat 과 동일

run 이 끝나는 방식은 둘뿐이다 — 답변(KukieResponse) 또는 승인 대기 티켓(DeferredToolRequests).
어느 쪽이 왔는지는 _to_payload 가 한 번만 판단하고, 앱은 kind 로 갈라 그린다.
승인 대기 중에는 /chat 을 409 로 막는다 — 입력 잠금을 서버가 보장한다.

세션 상태(대화 기록·대기 티켓)는 메모리에만 있다. 서버가 꺼지면 대기 중인 승인은 만료된다 (MVP 결정).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from kukie.agent import agent
from kukie.deps import Deps
from kukie.guardrail.action_plan import ActionPlan
from kukie.kubectl.config import KubeconfigError, read_kubeconfig
from kukie.router import pick_skill
from kukie.skills import DEFAULT_SKILL, SKILLS


# ── 세션 ─────────────────────────────────────────────────────

@dataclass
class Session:
    deps: Deps
    history: list[ModelMessage] = field(default_factory=list)   # 턴 간 대화 기록 (재개에도 필요)
    pending: list[str] = field(default_factory=list)            # 승인 대기 중인 call_id

    @property
    def skill(self):
        return self.deps.skill


_session: Session | None = None   # MVP: 프로세스당 세션 하나


def _require_session() -> Session:
    if _session is None:
        raise HTTPException(409, "세션이 없다. POST /session 먼저")
    return _session


# ── 요청 본문 ──────────────────────────────────────────────

class ChatIn(BaseModel):
    text: str


class ApproveIn(BaseModel):
    call_id: str
    approved: bool


# ── 에이전트 호출과 결과 분기 ─────────────────────────────

async def _run_agent(session: Session, **kwargs: Any):
    """agent.run 호출 자리. 세션의 deps·스킬·기록을 항상 같이 싣는다 (테스트에서 바꿔치기하는 이음매)."""
    return await agent.run(
        deps=session.deps,
        output_type=session.skill.output_fn,
        message_history=session.history or None,
        **kwargs,
    )


def _plan_payload(call_id: str) -> dict[str, Any] | None:
    """승인 카드에 실을 계획서 내용. 훅이 저장한 파일을 call_id 로 찾는다."""
    try:
        plan = ActionPlan.find_by_call_id(call_id)
    except FileNotFoundError:
        return None
    return {
        "id": plan.id,
        "tool": plan.tool,
        "command": plan.command,
        "risk_level": plan.risk_level,
        "target": plan.target,
        "intent": plan.intent,
        "expected_effects": plan.expected_effects,
        "side_effects": plan.side_effects,
        "dry_run_result": plan.dry_run_result,
        "decision_guidance": plan.decision_guidance,
        "status": plan.status,
    }


def _to_payload(session: Session, result) -> dict[str, Any]:
    """run 결과를 앱이 그릴 형태로. 기록 보관과 대기 티켓 갱신도 여기서."""
    session.history = result.all_messages()
    output = result.output

    if isinstance(output, DeferredToolRequests):
        session.pending = [call.tool_call_id for call in output.approvals]
        return {
            "kind": "approval",
            "skill": session.skill.name,
            "approvals": [
                {
                    "call_id": call.tool_call_id,
                    "tool": call.tool_name,
                    "args": call.args,
                    "plan": _plan_payload(call.tool_call_id),
                }
                for call in output.approvals
            ],
        }

    session.pending = []
    return {"kind": "answer", "skill": session.skill.name, "response": output.model_dump()}


# ── 엔드포인트 ──────────────────────────────────────────────

app = FastAPI(title="Kukie local server")


@app.post("/session")
def start_session() -> dict[str, Any]:
    """kubeconfig 의 현재 대상을 읽어 세션을 연다. 앱은 이 값을 "맞나요?" 화면에 띄운다."""
    global _session
    try:
        context, namespace = read_kubeconfig()
    except KubeconfigError as exc:
        raise HTTPException(503, f"kubeconfig 를 읽을 수 없다: {exc}") from exc
    _session = Session(deps=Deps(context=context, namespace=namespace, skill=DEFAULT_SKILL))
    return _session_view(_session)


@app.get("/session")
def get_session() -> dict[str, Any]:
    return _session_view(_require_session())


def _session_view(session: Session) -> dict[str, Any]:
    return {
        "context": session.deps.context,
        "namespace": session.deps.namespace,
        "skill": session.skill.name,
        "pending": list(session.pending),
    }


@app.post("/chat")
async def chat(body: ChatIn) -> dict[str, Any]:
    session = _require_session()
    if session.pending:
        raise HTTPException(409, "승인 대기 중 — /approve 로 먼저 결정")

    skill = pick_skill(body.text, session.skill)
    if skill is not session.skill:
        session.deps = dataclasses.replace(session.deps, skill=skill)
    if body.text.startswith("/mode "):          # 모드 전환만 한 입력은 LLM 을 부르지 않는다
        requested = body.text.removeprefix("/mode ").strip()
        return {"kind": "mode", "skill": session.skill.name,
                "known": requested in SKILLS}   # 모르는 모드면 현재 스킬 유지 + known=False

    result = await _run_agent(session, user_prompt=body.text)
    return _to_payload(session, result)


@app.post("/approve")
async def approve(body: ApproveIn) -> dict[str, Any]:
    session = _require_session()
    if body.call_id not in session.pending:
        raise HTTPException(409, f"대기 중인 승인 건이 아니다: {body.call_id}")

    result = await _run_agent(
        session,
        deferred_tool_results=DeferredToolResults(approvals={body.call_id: body.approved}),
    )
    return _to_payload(session, result)
