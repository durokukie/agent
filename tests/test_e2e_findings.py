"""첫 로컬 E2E(DURO-85)에서 찾은 문제들 — issue #59, #60.

가짜 모델로는 재현되지 않는 종류라 그때 실제로 돌려 보고 발견했다. 여기서는 원인을 막는
장치가 실제로 붙어 있는지 확인한다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kukie import agent as agent_module
from kukie import conversations, server
from kukie.kubectl import KubectlResult
from kukie.skills import SKILLS
from kukie.store import get_store, reset_store_for_tests
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


def test_공백만_보내면_제목을_바꾸지_않는다(client):
    room = _room(client)
    _say(client, room, "   ")
    assert get_store().get_session(room).title == "새 대화"


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


def test_과거_모드의_거절을_따르지_말라고_못_박는다():
    """대화 기록에 남은 이전 모드의 거절 답변이 모델을 끌던 것이 #60 의 원인이었다."""
    text = _instruction("실습")
    assert "과거 답변을 근거로 거절하지 마라" in text
    assert "지나간 상태" in text


def test_모드를_바꾸면_지시문도_바뀐다(client):
    """`/mode` 는 LLM 을 부르지 않아 기록에 흔적이 없다 — 지시문이 유일한 통로다."""
    assert _instruction("학습") != _instruction("실습")
