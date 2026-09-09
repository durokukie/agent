"""로컬 서버 — 앱이 agent.run 을 HTTP 로 부르는 진입점 (DURO-49).

사용자 컴에서 앱과 함께 돈다 (DURO-46 구조 1). 원격 아님, 인증 없음, 단일 사용자.
앱(Node)은 파이썬을 직접 못 부르므로 이 서버가 "에이전트의 입" 역할을 한다.

엔드포인트 4개:
  POST /session  kubeconfig 에서 대상(context/namespace)을 읽어 세션 시작
  POST /chat     한 턴 실행. 결과가 답변이면 answer, 승인 대기면 approval
  POST /approve  승인 버튼 결과로 run 재개 (2차). 결과 분기는 /chat 과 동일
  POST /resume   재개가 실패했을 때 같은 결정으로 다시 시도 (DURO-66)

run 이 끝나는 방식은 둘뿐이다 — 답변(KukieResponse) 또는 승인 대기 티켓(DeferredToolRequests).
어느 쪽이 왔는지는 _to_payload 가 한 번만 판단하고, 앱은 kind 로 갈라 그린다.
잠금은 두 겹이다: 승인 대기 중 /chat 409 (pending), run 실행 중 모든 진입 409 (processing).
단일 프로세스 MVP 라 플래그면 충분하다 — asyncio 는 await 지점에서만 끼어들 수 있으므로
"검사 → True 설정" 사이에 다른 요청이 낄 수 없다.

재개 실패 정책 (DURO-66): 어떤 실패에도 세션을 버리지 않는다. 실패 시점에 history·pending·
decisions 는 온전하므로(전부 run 성공 후에만 갱신) 503 RESUME_RETRYABLE 을 돌려주고 /resume 으로
같은 결정을 다시 보낸다. kubectl 이 이미 돌았는지는 서버가 아니라 Plan 파일이 알고, 훅이 그걸 보고
저장된 결과를 돌려주므로(guardrail/hook.py) 재시도해도 변경이 두 번 적용되지 않는다.
계속 실패하면 사용자가 POST /session 으로 새로 여는 것이 탈출구다 — 버리는 결정은 코드가 아니라
사용자가 한다.

세션 상태(대화 기록·대기 티켓)는 메모리에만 있다. 서버가 꺼지면 대기 중인 승인은 만료된다 (MVP 결정).
"""
from __future__ import annotations

import dataclasses
import logging
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
from kukie.observability import setup as setup_observability
from kukie.router import pick_skill
from kukie.skills import DEFAULT_SKILL, SKILLS

logger = logging.getLogger(__name__)

# 계측은 opt-in — LOGFIRE_TOKEN·OTEL 엔드포인트가 없으면 아무 일도 하지 않는다.
# 테스트가 이 모듈을 import 해도 무해하다.
setup_observability()


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
            run_id=session.deps.run_id,
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
    payload, _ = await _chat_turn(session, body.text)
    return payload


async def _chat_turn(session: Session, text: str) -> tuple[dict[str, Any], Any]:
    """한 턴. flat /chat 과 /conversations/{id}/chat 이 같이 쓴다.

    반환 (payload, run 결과). 모드 전환만 한 입력은 LLM 을 안 부르므로 결과가 None 이다.
    잠금·티켓 검사는 부르는 쪽이 한다 (flat 은 processing 플래그, 대화 단위는 asyncio.Lock).
    """
    skill = pick_skill(text, session.skill)
    if skill is not session.skill:
        session.deps = dataclasses.replace(session.deps, skill=skill)
    if text.startswith("/mode "):          # 모드 전환만 한 입력은 LLM 을 부르지 않는다
        requested = text.removeprefix("/mode ").strip()
        return ({"kind": "mode", "skill": session.skill.name,
                 "known": requested in SKILLS}, None)   # 모르는 모드면 현재 스킬 유지 + known=False

    session.processing = True
    try:
        result = await _run_agent(session, user_prompt=text)
        return _to_payload(session, result), result
    finally:
        session.processing = False


@app.post("/approve")
async def approve(body: ApproveIn) -> dict[str, Any]:
    session = _require_session()
    if session.processing:
        raise HTTPException(409, "이전 요청 처리 중 — 끝난 뒤 다시 보내라")
    payload, _ = await _approve(session, body.call_id, body.approved)
    return payload


async def _approve(session: Session, call_id: str, approved: bool) -> tuple[dict[str, Any], Any]:
    """승인 카드 한 장의 결정. flat /approve 와 /conversations/{id}/approve 가 같이 쓴다.

    반환 (payload, run 결과). 남은 카드가 있어 재전송만 하면 결과가 None 이다.
    """
    requests = session.pending
    if requests is None:
        raise HTTPException(409, f"대기 중인 승인 건이 아니다: {call_id}")
    if call_id in session.decisions:
        raise HTTPException(409, f"이미 결정한 승인 건이다: {call_id}")

    call = next(
        (
            item
            for item in requests.approvals
            if item.tool_call_id == call_id
        ),
        None,
    )
    if call is None:
        raise HTTPException(409, f"대기 중인 승인 건이 아니다: {call_id}")

    try:
        approval_request = build_approval_request(
            call,
            requests.metadata.get(call_id, {}),
            default_namespace=session.deps.namespace,
            run_id=session.deps.run_id,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None

    # 거절 기록은 run 밖에서 — 로컬 파일 쓰기일 뿐이라 실패해도 LLM 도 클러스터도 건드리지 않았다.
    # 결정을 남기지 않고 티켓을 그대로 두면 사용자가 같은 카드를 다시 누를 수 있다 (DURO-66 결정 불필요 1).
    if not approved:
        try:
            ActionPlan.find_by_call_id(
                approval_request.tool_call_id, run_id=session.deps.run_id,
            ).reject(session.deps.user_id)
        except Exception as exc:
            # DB 가 원본이 된 뒤로는 SQLAlchemyError 도 온다 (OSError·ValueError 만 잡으면
            # DB 장애일 때만 안내가 사라지고 코드 없는 500 이 나간다 — 자동 리뷰 지적)
            logger.exception("거절 기록 실패 — 티켓 유지 (call_id=%s, plan_id=%s)",
                             call_id, approval_request.plan_id)
            raise HTTPException(503, f"거절을 기록하지 못했다. 같은 카드를 다시 결정하라: {exc}") from exc
    session.decisions[call_id] = (
        ToolApproved()
        if approved
        else ToolDenied("사용자가 변경 요청을 거절했습니다.")
    )

    remaining = [
        call
        for call in requests.approvals
        if call.tool_call_id not in session.decisions
    ]
    if remaining:
        return _approval_payload(session, requests, remaining), None
    return await _resume(session)


@app.post("/resume")
async def resume() -> dict[str, Any]:
    """재개 실패(503 RESUME_RETRYABLE) 뒤 같은 결정으로 다시 시도한다.

    결정이 전부 내려진 티켓이 있어야 한다 — 새 결정을 받는 자리가 아니다. 그건 /approve.
    """
    session = _require_session()
    if session.processing:
        raise HTTPException(409, "이전 요청 처리 중 — 끝난 뒤 다시 보내라")
    if session.pending is None:
        raise HTTPException(409, "재개할 승인 건이 없다")
    if session.pending_ids:
        raise HTTPException(409, f"아직 결정하지 않은 승인 건이 있다: {session.pending_ids}")
    payload, _ = await _resume(session)
    return payload


def _pending_plan_ids(session: Session) -> list[str]:
    """티켓의 카드들이 가리키는 Plan id — 실패 응답에 실어 앱이 Action Plan 화면으로 안내하게."""
    assert session.pending is not None
    return [
        str(plan_id)
        for call in session.pending.approvals
        if (plan_id := session.pending.metadata.get(call.tool_call_id, {}).get("plan_id"))
    ]


async def _resume(session: Session) -> tuple[dict[str, Any], Any]:
    """저장된 결정으로 run 을 재개한다. /approve(마지막 결정)와 /resume(재시도)이 함께 쓴다.

    실패해도 session.pending / decisions 를 건드리지 않는다 — 그대로 남아 있어야 /resume 이
    같은 결정을 재조립할 수 있다. 세션을 버리지 않는 이유는 모듈 docstring 참고.
    """
    assert session.pending is not None
    plan_ids = _pending_plan_ids(session)
    session.processing = True
    try:
        results = session.pending.build_results(approvals=session.decisions)
        result = await _run_agent(session, deferred_tool_results=results)
        return _to_payload(session, result), result
    except Exception as exc:
        logger.exception(
            "승인 재개 실패 — 세션·티켓 유지, /resume 으로 재시도 가능 (call_ids=%s, plan_ids=%s)",
            list(session.decisions),
            plan_ids,
        )
        raise HTTPException(
            503,
            {
                "code": "RESUME_RETRYABLE",
                "message": (
                    "승인 결과 처리에 실패했다. 변경은 이미 적용됐을 수 있다 — "
                    "POST /resume 으로 다시 시도하거나 Action Plan 을 확인하라"
                ),
                "plan_ids": plan_ids,
            },
        ) from exc
    finally:
        session.processing = False


# ── 대화(채팅방) 단위 엔드포인트 — kukie/conversations_api.py, Action Plan 조회 — kukie/plans_api.py ──
# flat 엔드포인트와 같은 _chat_turn / _approve / _resume 을 쓴다. 그 모듈이 이 모듈을 참조하므로
# 맨 아래에서 plain import 만 한다 (from-import 는 순환 시 이름이 아직 없어 깨진다).
# 라우터 등록은 conversations_api 가 자기 맨 아래에서 app.include_router 로 한다.
import kukie.conversations_api  # noqa: E402, F401
import kukie.plans_api  # noqa: E402, F401
