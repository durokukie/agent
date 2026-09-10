"""첫 로컬 E2E(DURO-85)에서 찾은 문제들 — issue #59, #60.

가짜 모델로는 재현되지 않는 종류라 그때 실제로 돌려 보고 발견했다. 여기서는 원인을 막는
장치가 실제로 붙어 있는지 확인한다.
"""
from __future__ import annotations

from types import SimpleNamespace

from pydantic_ai.messages import ToolCallPart

import pytest
from fastapi.testclient import TestClient

from kukie import agent as agent_module
from kukie import conversations, server
from kukie.kubectl import KubectlResult
from kukie.agent import agent
from kukie.skills import SKILLS
from kukie.store import get_store, reset_store_for_tests
from kukie.store.models import DEFAULT_TITLE
from kukie.tools import read as read_tools

USER = {"X-User": "u-1"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.delenv("KUKIE_DEV_USER", raising=False)
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")
    reset_store_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    conversations.registry.clear()
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    monkeypatch.setattr(
        read_tools, "run_kubectl",
        lambda *a, **k: KubectlResult(command="kubectl …", stdout="nginx Running", stderr="",
                                      success=True, exit_code=0),
    )
    return TestClient(server.app)


def _room(client) -> str:
    return client.post("/conversations", json={}, headers=USER).json()["conversation"]["id"]


def _say(client, room: str, text: str):
    from pydantic_ai.models.test import TestModel
    from kukie.agent import agent

    with agent.override(model=TestModel(call_tools=[], custom_output_args={
        "narration": "답변입니다.", "suggested_next_action": None
    })):
        return client.post(f"/conversations/{room}/chat", json={"text": text}, headers=USER)


# ── #59 대화 제목 ──────────────────────────────────────────

def test_첫_마디가_방_제목이_된다(client):
    room = _room(client)
    assert get_store().get_session(room).title == "새 대화"

    _say(client, room, "운영 클러스터의 파드 상태를 보여줘")

    assert get_store().get_session(room).title == "운영 클러스터의 파드 상태를 보여줘"


def test_제목은_한_번만_정해진다(client):
    """두 번째 마디로 제목이 바뀌면 사이드바가 대화 도중에 계속 흔들린다."""
    room = _room(client)
    _say(client, room, "첫 번째 질문")
    _say(client, room, "두 번째 질문")

    assert get_store().get_session(room).title == "첫 번째 질문"


def test_긴_첫_마디는_앞_30자까지만(client):
    room = _room(client)
    _say(client, room, "가" * 100)
    assert get_store().get_session(room).title == "가" * 30


def test_모드_전환은_제목을_정하지_않는다(client):
    """`/mode 진단` 이 방 이름이 되면 안 된다."""
    room = _room(client)
    client.post(f"/conversations/{room}/chat", json={"text": "/mode 진단"}, headers=USER)
    assert get_store().get_session(room).title == "새 대화"


def test_공백만_보내면_제목을_바꾸지_않고_자리도_안_먹는다(client):
    room = _room(client)
    _say(client, room, "   ")
    assert get_store().get_session(room).title == "새 대화"

    _say(client, room, "파드 보여줘")
    assert get_store().get_session(room).title == "파드 보여줘"


def test_인자_없는_mode_도_제목이_되지_않고_자리도_안_먹는다(client):
    """`/mode` 만 보내면 kind 는 chat 이라 라우팅은 일반 대화지만, 방 이름이 되면 안 된다.

    1턴만 보면 구멍이 있는 채로 통과한다 — 그 run 이 "첫 턴" 자리를 먹고 사라지면 다음 마디도
    제목이 못 된다 (자동 리뷰 4차). 그래서 두 번째 마디까지 본다.
    """
    room = _room(client)
    _say(client, room, "/mode")
    assert get_store().get_session(room).title == "새 대화"

    _say(client, room, "파드 보여줘")
    assert get_store().get_session(room).title == "파드 보여줘"


def test_첫_마디가_새_대화면_다음_턴이_덮어쓰지_않는다(client):
    """제목 글자만 보고 판단하면 "한 번만 정해진다" 가 이 경우에 깨진다 (자동 리뷰 지적)."""
    room = _room(client)
    _say(client, room, "새 대화")
    _say(client, room, "두 번째 질문")
    assert get_store().get_session(room).title == "새 대화"


def test_제목_쓰기가_version_을_올린다(client):
    """세션 행을 고치는 다른 경로와 같아야 앱이 "바뀌었다" 를 알아챈다.

    매 chat 턴이 current_mode 갱신으로 이미 version 을 한 번 올리므로(자동 리뷰 지적), 단순히
    "올랐나" 만 보면 제목 쓰기를 지워도 통과한다. 제목이 바뀌는 첫 턴의 증가분과 안 바뀌는
    둘째 턴의 증가분을 비교한다.
    """
    room = _room(client)
    before = get_store().get_session(room).version

    _say(client, room, "첫 번째 질문")
    after_first = get_store().get_session(room).version

    _say(client, room, "두 번째 질문")            # 제목은 그대로다
    after_second = get_store().get_session(room).version

    assert after_first - before == 2              # 모드 갱신 + 제목
    assert after_second - after_first == 1        # 모드 갱신만


def test_모드를_먼저_바꿔도_첫_마디가_제목이_된다(client):
    """turn_no 로 판단하면 `/mode` 가 1번을 먹어 그 방은 제목을 영영 못 받는다 (자동 리뷰 지적).

    E2E 에서 실제로 나온 순서다 — 모드 바꾸고 나서 변경을 요청한다.
    """
    room = client.post("/conversations", json={"shared": True}, headers=USER).json()["conversation"]["id"]
    client.post(f"/conversations/{room}/chat", json={"text": "/mode 실습"}, headers=USER)

    _say(client, room, "nginx 를 4개로 늘려줘")

    assert get_store().get_session(room).title == "nginx 를 4개로 늘려줘"


def test_제목_저장이_실패해도_대화는_성공으로_끝난다(client, monkeypatch):
    """제목은 부가 정보다. 여기서 터지면 이미 completed 로 저장한 run 이 500 으로 뒤집힌다."""
    def boom(*a, **k):
        raise RuntimeError("DB 넘어짐")

    monkeypatch.setattr(get_store(), "name_from_first_message", boom)
    room = _room(client)
    assert _say(client, room, "안녕").status_code == 200


# ── #60 현재 모드 못 박기 ──────────────────────────────────

def _instruction(skill_name: str) -> str:
    ctx = SimpleNamespace(deps=SimpleNamespace(skill=SKILLS[skill_name]))
    return agent_module.add_current_mode(ctx)


@pytest.mark.parametrize("mode", ["학습", "진단", "실습"])
def test_매_턴_현재_모드를_이름으로_말한다(mode):
    assert f"[현재 모드: {mode}]" in _instruction(mode)


def test_지금_쓸_수_있는_툴을_함께_말한다():
    """모델이 문맥에서 추론하지 않게 한다 — 노출된 툴이 곧 가능한 일이다."""
    실습 = _instruction("실습")
    assert "scale_resource" in 실습 and "apply_manifest" in 실습
    학습 = _instruction("학습")
    assert "scale_resource" not in 학습


def test_과거_모드의_거절을_따르지_말라고_못_박되_다른_모드로_한정한다():
    """대화 기록에 남은 **이전 모드의** 거절 답변이 모델을 끌던 것이 #60 의 원인이었다.

    한정을 빼면 반대 방향으로 아프다 — 학습 모드에서 변경을 요청받았을 때의 **정당한 거절**까지
    눌러 버린다. 앱의 [실습 모드로] 버튼이 그 안내에서 나온다 (자동 리뷰 지적).
    """
    text = _instruction("실습")
    assert "다른 모드에서" in text and "지나간 상태" in text
    assert "**다른 모드의 과거 답변을** 근거로 거절하지 마라" in text
    assert "그건 올바른 답변이다" in text          # 지금 모드에 없는 일은 거절해도 된다


def test_출력_툴은_금지_목록에_걸리지_않는다():
    """allowed_tools 에는 응답을 마무리하는 출력 툴이 없다. "이것뿐" 이라고만 하면 모델이
    필수 출력 툴을 안 부를 수 있다 (자동 리뷰 P1)."""
    text = _instruction("학습")
    assert "클러스터에 쓸 수 있는 툴" in text
    assert "출력 툴은 여기 해당하지 않는다" in text


def test_모드를_바꾸면_다음_턴의_지시문이_실제로_바뀐다(client):
    """#60 은 문구가 아니라 **전환이 ctx.deps.skill 까지 오느냐** 의 문제였다.

    `/mode` 를 실제로 보낸 뒤, 다음 chat 턴이 도는 시점의 deps 를 잡아 지시문을 확인한다.
    """
    from pydantic_ai import ModelResponse
    from pydantic_ai.messages import TextPart
    from pydantic_ai.models.function import FunctionModel

    seen: list[str] = []

    def capture(messages, info):
        # @agent.instructions 의 결과는 ModelRequest.instructions 로 온다 (parts 가 아니다)
        for message in messages:
            text = getattr(message, "instructions", None)
            if isinstance(text, str) and "[현재 모드:" in text:
                seen.append(text[text.index("[현재 모드:"):][:20])
        out = info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(
            tool_name=out.name,
            args={"narration": "답변입니다.", "suggested_next_action": None},
        )])

    # 실습 모드는 shared 방에서만 된다 (기획 05 §3)
    room = client.post("/conversations", json={"shared": True}, headers=USER).json()["conversation"]["id"]
    with agent.override(model=FunctionModel(capture)):
        client.post(f"/conversations/{room}/chat", json={"text": "안녕"}, headers=USER)
    assert seen and "학습" in seen[-1]

    # 모드를 바꾼다 — 이 요청은 LLM 을 부르지 않는다
    r = client.post(f"/conversations/{room}/chat", json={"text": "/mode 실습"}, headers=USER)
    assert r.json()["skill"] == "실습"

    with agent.override(model=FunctionModel(capture)):
        client.post(f"/conversations/{room}/chat", json={"text": "nginx 를 늘려줘"}, headers=USER)
    assert "실습" in seen[-1]          # 전환이 다음 턴의 deps 까지 왔다


def test_빈_제목으로_만든_방도_첫_마디로_제목을_받는다(client):
    """title: "" 는 DEFAULT_TITLE 과 달라서 "아직 기본 제목" 검사를 영영 통과 못 한다."""
    room = client.post("/conversations", json={"title": "  ", "shared": True},
                       headers=USER).json()["conversation"]["id"]
    assert get_store().get_session(room).title == DEFAULT_TITLE

    _say(client, room, "파드 상태 알려줘")
    assert get_store().get_session(room).title == "파드 상태 알려줘"
