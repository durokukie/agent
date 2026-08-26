"""로컬 서버 — 앱이 agent.run 을 HTTP 로 부르는 진입점 (DURO-49).

사용자 컴에서 앱과 함께 돈다 (DURO-46 구조 1). 원격 아님, 인증 없음, 단일 사용자.
앱(Node)은 파이썬을 직접 못 부르므로 이 서버가 "에이전트의 입" 역할을 한다.

엔드포인트 3개:
  POST /session  kubeconfig 에서 대상(context/namespace)을 읽어 세션 시작
  POST /chat     한 턴 실행. 결과가 답변이면 answer, 승인 대기면 approval
  POST /approve  승인 버튼 결과로 run 재개 (2차). 결과 분기는 /chat 과 동일

run 이 끝나는 방식은 둘뿐이다 — 답변(KukieResponse) 또는 승인 대기 티켓(DeferredToolRequests).
어느 쪽이 왔는지는 _to_payload 가 한 번만 판단하고, 앱은 kind 로 갈라 그린다.
잠금은 두 겹이다: 승인 대기 중 /chat 409 (pending), run 실행 중 모든 진입 409 (processing).
단일 프로세스 MVP 라 플래그면 충분하다 — asyncio 는 await 지점에서만 끼어들 수 있으므로
"검사 → True 설정" 사이에 다른 요청이 낄 수 없다.

세션 상태(대화 기록·대기 티켓)는 메모리에만 있다. 서버가 꺼지면 대기 중인 승인은 만료된다 (MVP 결정).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.tools import DeferredToolRequests, ToolApproved, ToolDenied

from kukie.agent import agent
from kukie.deps import Deps
from kukie.guardrail.action_plan import ActionPlan
from kukie.guardrail.approval import ApprovalRequest, build_approval_request
from kukie.kubectl.config import KubeconfigError, read_kubeconfig
from kukie.router import pick_skill
from kukie.skills import DEFAULT_SKILL, SKILLS


# ── 세션 ─────────────────────────────────────────────────────

@dataclass
class Session:
    deps: Deps
    history: list[ModelMessage] = field(default_factory=list)   # 턴 간 대화 기록 (재개에도 필요)
    pending: DeferredToolRequests | None = None
    decisions: dict[str, ToolApproved | ToolDenied] = field(default_factory=dict)
    # 실행 잠금 — run 이 도는 동안(await) 두 번째 run 이 같은 history 를 쓰면 늦게 끝난
    # 쪽이 기록을 덮어쓴다. pending 은 승인 대기 상태만 표현하므로 실행 잠금을 겸할 수 없다.
    processing: bool = False

    @property
    def skill(self):
        return self.deps.skill

    @property
    def pending_ids(self) -> list[str]:
        if self.pending is None:
            return []
        return [
            call.tool_call_id
            for call in self.pending.approvals
            if call.tool_call_id not in self.decisions
        ]


_session: Session | None = None   # MVP: 프로세스당 세션 하나


def _require_session() -> Session:
    if _session is None:
        raise HTTPException(409, "세션이 없다. POST /session 먼저")
    return _session


# ── 요청 본문 ──────────────────────────────────────────────

class ChatIn(BaseModel):
    text: str


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    approved: bool


# ── 에이전트 호출과 결과 분기 ─────────────────────────────

async def _run_agent(session: Session, **kwargs: Any):
    """agent.run 호출 자리. 세션의 deps·스킬·기록을 항상 같이 싣는다 (테스트에서 바꿔치기하는 이음매)."""
    return await agent.run(
        deps=session.deps,
        output_type=[session.skill.output_fn, DeferredToolRequests],
        message_history=session.history or None,
        **kwargs,
    )


def _approval_payload(
    session: Session,
    requests: DeferredToolRequests,
    calls: list[ToolCallPart] | None = None,
) -> dict[str, Any]:
    calls = requests.approvals if calls is None else calls
    approvals: list[ApprovalRequest] = [
        build_approval_request(
            call,
            requests.metadata.get(call.tool_call_id, {}),
            default_namespace=session.deps.namespace,
        )
        for call in calls
    ]
    return {
        "kind": "approval",
        "skill": session.skill.name,
        "approvals": [approval.model_dump() for approval in approvals],
    }


def _to_payload(session: Session, result) -> dict[str, Any]:
    """run 결과를 앱이 그릴 형태로. 기록 보관과 대기 티켓 갱신도 여기서."""
    output = result.output

    if isinstance(output, DeferredToolRequests):
        payload = _approval_payload(session, output)
        session.history = result.all_messages()
        session.pending = output
        session.decisions.clear()
        return payload

    session.history = result.all_messages()
    session.pending = None
    session.decisions.clear()
    return {"kind": "answer", "skill": session.skill.name, "response": output.model_dump()}


# ── 엔드포인트 ──────────────────────────────────────────────

app = FastAPI(title="Kukie local server")


@app.post("/session")
async def start_session() -> dict[str, Any]:
    """kubeconfig 의 현재 대상을 읽어 세션을 연다. 앱은 이 값을 "맞나요?" 화면에 띄운다.

    async def 인 것이 동기화의 일부다: 동기 def 면 FastAPI 가 스레드풀에서 돌려서
    processing 검사와 _session 교체 사이에 이벤트 루프의 /chat 이 낄 수 있다 (리뷰 지적).
    async def 면 본문에 await 가 없어 검사→교체가 루프에서 통째로 원자적이다.
    read_kubeconfig 의 블로킹(수백 ms)은 그동안 루프를 세우지만, 로컬 단일 사용자 MVP 에서
    수용한다 — run_kubectl 이 동기인 것과 같은 결정.
    """
    global _session
    if _session is not None and _session.processing:
        raise HTTPException(409, "요청 처리 중 — 지금은 세션을 교체할 수 없다")
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
        "pending": session.pending_ids,
    }


@app.post("/chat")
async def chat(body: ChatIn) -> dict[str, Any]:
    session = _require_session()
    if session.processing:
        raise HTTPException(409, "이전 요청 처리 중 — 끝난 뒤 다시 보내라")
    if session.pending is not None:
        raise HTTPException(409, "승인 대기 중 — /approve 로 먼저 결정")

    skill = pick_skill(body.text, session.skill)
    if skill is not session.skill:
        session.deps = dataclasses.replace(session.deps, skill=skill)
    if body.text.startswith("/mode "):          # 모드 전환만 한 입력은 LLM 을 부르지 않는다
        requested = body.text.removeprefix("/mode ").strip()
        return {"kind": "mode", "skill": session.skill.name,
                "known": requested in SKILLS}   # 모르는 모드면 현재 스킬 유지 + known=False

    session.processing = True
    try:
        result = await _run_agent(session, user_prompt=body.text)
        return _to_payload(session, result)
    finally:
        session.processing = False


@app.post("/approve")
async def approve(body: ApproveIn) -> dict[str, Any]:
    global _session
    session = _require_session()
    if session.processing:
        raise HTTPException(409, "이전 요청 처리 중 — 끝난 뒤 다시 보내라")
    requests = session.pending
    if requests is None:
        raise HTTPException(409, f"대기 중인 승인 건이 아니다: {body.call_id}")
    if body.call_id in session.decisions:
        raise HTTPException(409, f"이미 결정한 승인 건이다: {body.call_id}")

    call = next(
        (
            item
            for item in requests.approvals
            if item.tool_call_id == body.call_id
        ),
        None,
    )
    if call is None:
        raise HTTPException(409, f"대기 중인 승인 건이 아니다: {body.call_id}")

    try:
        approval_request = build_approval_request(
            call,
            requests.metadata.get(body.call_id, {}),
            default_namespace=session.deps.namespace,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None

    session.processing = True
    try:
        decision = (
            ToolApproved()
            if body.approved
            else ToolDenied("사용자가 변경 요청을 거절했습니다.")
        )
        if not body.approved:
            ActionPlan.find_by_call_id(approval_request.tool_call_id).reject()
        session.decisions[body.call_id] = decision

        remaining = [
            call
            for call in requests.approvals
            if call.tool_call_id not in session.decisions
        ]
        if remaining:
            return _approval_payload(session, requests, remaining)

        results = requests.build_results(approvals=session.decisions)
        result = await _run_agent(
            session,
            deferred_tool_results=results,
        )
        return _to_payload(session, result)
    except Exception as exc:
        _session = None
        raise HTTPException(
            503,
            "승인 결과 처리에 실패했다. POST /session으로 새 세션을 시작해야 한다",
        ) from exc
    finally:
        session.processing = False
