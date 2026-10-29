"""옛 답변 끌림 막기 — ModelRetry 로 길 강제 + 레포 연결이 바뀐 때만 메모 한 줄 (DURO-135).

끝 기준 (이슈 DURO-135)
  1. 연결됨 → 끊김 → 끊김 세 턴: 메모가 정확히 한 번, 끊긴 턴에만 들어가고 서버 저장분에도 남는다
  2. 한 턴에 툴을 여러 번 불러도 메모는 한 줄
  3. 끊긴 레포에 propose_git_change 를 부르면 ModelRetry 후 kubectl 길로 간다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelResponse
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from fake_github import REPO, FakeCluster, FakeGitHub
from helpers import USER, new_room
from kukie_agent import argocd, server
from kukie_agent.agent import agent
from kukie_agent.gitops import github, memo, route
from kukie_agent.guardrail.hook import PR_PATH_RETRY

SCALE = {"kind": "deployment", "name": "web", "replicas": 3, "namespace": "shop",
         "intent": "web 을 3개로", "expected_effects": ["3개"], "side_effects": ["자원"]}
PROPOSE = {"kind": "deployment", "name": "web", "namespace": "shop", "change": {"what": "replicas", "count": 3},
           "intent": "web 을 3개로", "expected_effects": ["3개"], "side_effects": ["자원"]}


@pytest.fixture
def cluster(monkeypatch):
    fake = FakeCluster()
    monkeypatch.setattr(route, "run_kubectl", fake.run)
    monkeypatch.setattr(argocd, "run_kubectl", fake.run)
    monkeypatch.setattr(github, "connect", FakeGitHub().connect)
    return fake


@pytest.fixture
def client(dev_auth, fake_server, kubectl_ok, hook_passes):
    return TestClient(server.app)


@pytest.fixture
def room(fake_server):
    room_id = new_room(fake_server, shared=True)
    fake_server.link_repo(_cluster_of(fake_server, room_id), REPO)
    return room_id


def _cluster_of(fake_server, room):
    return fake_server.conversation(room)["clusterId"]


def _memos(parts) -> list[str]:
    return [p.content for p in parts if memo.is_memo(p)]


def _saved_memos(fake_server, room) -> list[list[str]]:
    """run 마다 server 의 agent_messages 에 남은 메모들."""
    out = []
    for run in fake_server.runs_of(room):
        found = []
        for message in run["agentMessages"] or []:
            for part in message.get("parts", []):
                if part.get("part_kind") == "user-prompt" and str(part.get("content", "")).startswith(memo.MARK):
                    found.append(part["content"])
        out.append(found)
    return out


class Script:
    """FunctionModel — 턴마다 tools 를 차례로 부르고 답한다. 모델이 본 요청(views)을 모은다."""

    def __init__(self, *tools: tuple[str, dict]):
        self.tools = list(tools)
        self.views: list[list] = []
        self.step = 0

    def __call__(self, messages, info):
        self.views.append([p for m in messages for p in getattr(m, "parts", [])])
        last = messages[-1]
        if any(isinstance(p, UserPromptPart) and not memo.is_memo(p) for p in last.parts):
            self.step = 0
        if self.step < len(self.tools):
            name, args = self.tools[self.step]
            self.step += 1
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=dict(args), tool_call_id=f"call-{self.step}")])
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args={"narration": "네."}, tool_call_id="out")])


def _chat(client, room, script, text="안녕"):
    with agent.override(model=FunctionModel(script)):
        r = client.post(f"/conversations/{room}/chat", json={"text": text}, headers=USER)
    assert r.status_code == 200, r.text
    return r.json()


# ── 메모 ──────────────────────────────────────────────────

def test_연결됨_끊김_끊김_세_턴에_메모는_끊긴_턴에_한_번만_남는다(client, fake_server, room, cluster):
    script = Script()
    _chat(client, room, script)                                         # 턴 1: 연결됨 (새 대화 — 칸만 채운다)
    assert fake_server.repo_link_states[room] == {"repos": [REPO]}

    fake_server.unlink_repo(_cluster_of(fake_server, room), REPO)
    _chat(client, room, script)                                         # 턴 2: 끊김 ≠ 연결됨 → 메모
    _chat(client, room, script)                                         # 턴 3: 끊김 = 끊김 → 그대로

    saved = _saved_memos(fake_server, room)
    assert [len(found) for found in saved] == [0, 1, 0]                 # 서버 저장분에도 끊긴 턴에만 한 번
    assert f"레포 {REPO} 연결이 끊겼다" in saved[1][0] and "앞의 답변은 그 전 기준이다" in saved[1][0]
    assert fake_server.repo_link_states[room] == {"repos": []}
    # 턴 3 의 모델도 턴 2 의 메모를 (기록에서) 정확히 한 번 본다
    assert len(_memos(script.views[-1])) == 1
    # 메모는 새 질문 바로 앞 — 그 요청의 첫 조각이다
    turn2 = fake_server.runs_of(room)[1]["agentMessages"][0]["parts"]
    assert str(turn2[0]["content"]).startswith(memo.MARK) and turn2[1]["content"] == "안녕"


def test_한_턴에_툴을_여러_번_불러도_메모는_한_줄이다(client, fake_server, room, cluster):
    fake_server.repo_link_states[room] = {"repos": []}                 # 지난번엔 연결이 없었다
    script = Script(("list_resources", {"kind": "pods"}), ("list_resources", {"kind": "svc"}),
                    ("get_events", {}))
    body = _chat(client, room, script)

    assert len(script.views) == 4                                       # AI 호출 4번 = 처리기 4번
    assert [len(_memos(view)) for view in script.views] == [1, 1, 1, 1]
    assert [len(found) for found in _saved_memos(fake_server, room)] == [1]
    assert "새로 연결됐다" in _saved_memos(fake_server, room)[0][0]
    assert len(body["response"]["steps"]) == 3                          # 메모가 "이번 턴" 을 끊지 않는다


def test_연결_상태를_못_읽으면_메모도_숨은_칸도_건드리지_않는다(client, fake_server, room, cluster, monkeypatch):
    from kukie_agent import server_api

    async def down(conversation_id):
        raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "down")

    fake_server.repo_link_states[room] = {"repos": []}
    monkeypatch.setattr(server_api, "repo_link_state", down)
    _chat(client, room, Script())

    assert _saved_memos(fake_server, room) == [[]] and fake_server.repo_link_states[room] == {"repos": []}


def test_instructions와_툴_목록은_연결_상태와_상관없이_같다(client, fake_server, room, cluster):
    seen = []

    def capture(messages, info):
        seen.append(([t.name for t in info.function_tools],
                     [p.content for m in messages for p in getattr(m, "parts", []) if p.part_kind == "system-prompt"],
                     getattr(messages[0], "instructions", None)))
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args={"narration": "네."})])

    with agent.override(model=FunctionModel(capture)):
        client.post(f"/conversations/{room}/chat", json={"text": "a"}, headers=USER)
        fake_server.unlink_repo(_cluster_of(fake_server, room), REPO)
        client.post(f"/conversations/{room}/chat", json={"text": "b"}, headers=USER)
    assert seen[0] == seen[1]


# ── ModelRetry 로 길 강제 ───────────────────────────────────

def _retries(script) -> list[RetryPromptPart]:
    return [p for view in script.views[-1:] for p in view if isinstance(p, RetryPromptPart)]


def test_끊긴_레포에_propose_git_change를_부르면_ModelRetry_후_kubectl_길로_간다(client, fake_server, room, cluster):
    fake_server.unlink_repo(_cluster_of(fake_server, room), REPO)
    script = Script(("propose_git_change", PROPOSE), ("scale_resource", SCALE))
    body = _chat(client, room, script, "web 3개로")

    retry = next(p for view in script.views for p in view if isinstance(p, RetryPromptPart))
    assert retry.tool_name == "propose_git_change"
    assert "kubectl 길" in retry.content and "연결돼 있지 않아요" in retry.content
    assert body["kind"] == "approval"                                    # 고쳐 부른 kubectl 이 승인 카드로 간다
    card = body["approvals"][0]
    assert card["tool"] == "scale_resource"
    assert card["argocd"]["level"] == "self_heal" and "route" not in card["argocd"]   # PR 길이 아니다


def test_PR_길_리소스에_경고_없이_kubectl을_부르면_한_번_돌려보낸다(client, fake_server, room, cluster):
    script = Script(("scale_resource", SCALE), ("propose_git_change", PROPOSE))
    body = _chat(client, room, script, "web 3개로")

    retry = next(p for view in script.views for p in view if isinstance(p, RetryPromptPart))
    assert retry.tool_name == "scale_resource" and retry.content.startswith(PR_PATH_RETRY)
    assert "propose_git_change" in retry.content
    assert body["approvals"][0]["tool"] == "propose_git_change"
    assert [p["toolName"] for p in fake_server.plans_of(room)] == ["propose_git_change"]


def test_돌려받은_뒤_같은_kubectl_호출을_다시_하면_경고_카드와_함께_간다(client, fake_server, room, cluster):
    script = Script(("scale_resource", SCALE), ("scale_resource", SCALE))
    body = _chat(client, room, script, "급해요 kubectl 로 바로 3개")

    card = body["approvals"][0]
    assert card["tool"] == "scale_resource"
    assert card["argocd"]["route"] == {"path": "pr", "repository": REPO, "reason": card["argocd"]["route"]["reason"]}
    assert "PR 로 올려요" in card["argocd"]["route"]["reason"]


def test_restart_delete_는_PR로_하는_변경이_아니라_돌려보내지_않는다(client, fake_server, room, cluster):
    restart = {"kind": "deployment", "name": "web", "namespace": "shop", "intent": "재시작",
               "expected_effects": ["새 파드"], "side_effects": ["잠깐 끊김"]}
    body = _chat(client, room, Script(("rollout_restart", restart)), "재시작")

    assert body["approvals"][0]["tool"] == "rollout_restart"


def test_propose_git_change를_고집해도_run이_실패하지_않는다(client, fake_server, room, cluster):
    """재시도 한도(1)를 다 쓴 뒤에는 ToolFailed 로 알린다 — 또 ModelRetry 면 run 이 500 으로 끝난다."""
    fake_server.unlink_repo(_cluster_of(fake_server, room), REPO)
    script = Script(("propose_git_change", PROPOSE), ("propose_git_change", PROPOSE))
    body = _chat(client, room, script, "web 3개로")

    assert body["kind"] == "answer"
    last = [p for p in script.views[-1] if isinstance(p, ToolReturnPart) and p.tool_name == "propose_git_change"]
    assert last and "kubectl 길" in str(last[-1].content)


def test_메모가_든_run이_실패하면_숨은_칸은_그대로고_다음_턴에_다시_넣는다(client, fake_server, room, cluster):
    """칸을 처리기에서 바로 쓰면 메모는 버려졌는데 칸만 앞서 가서 영영 알리지 못한다 (agent#127 리뷰)."""
    _chat(client, room, Script())
    fake_server.unlink_repo(_cluster_of(fake_server, room), REPO)

    def boom(messages, info):
        assert len(_memos([p for m in messages for p in m.parts])) == 1      # 메모는 넣었지만
        raise RuntimeError("모델 오류")

    with agent.override(model=FunctionModel(boom)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "실패"}, headers=USER)
    assert r.status_code == 500
    assert fake_server.repo_link_states[room] == {"repos": [REPO]}           # run 이 실패해 칸은 그대로

    _chat(client, room, Script())
    assert [len(found) for found in _saved_memos(fake_server, room)] == [0, 0, 1]
    assert fake_server.repo_link_states[room] == {"repos": []}


def test_기록은_저장됐는데_칸_쓰기만_실패하면_다음_턴에_메모를_또_넣지_않는다(client, fake_server, room, cluster, monkeypatch):
    from kukie_agent import server_api

    _chat(client, room, Script())
    fake_server.unlink_repo(_cluster_of(fake_server, room), REPO)
    real = server_api.update_repo_link_state

    async def down(conversation_id, state):
        raise server_api.ServerError(503, "SERVER_UNAVAILABLE", "down")

    monkeypatch.setattr(server_api, "update_repo_link_state", down)
    _chat(client, room, Script())                                            # 메모는 저장, 칸 쓰기 실패
    monkeypatch.setattr(server_api, "update_repo_link_state", real)
    _chat(client, room, Script())

    assert [len(found) for found in _saved_memos(fake_server, room)] == [0, 1, 0]
    assert fake_server.repo_link_states[room] == {"repos": []}


def test_칸이_빈_옛_대화는_기록이_있어도_칸만_채운다(client, fake_server, room, cluster):
    """이 기능 전부터 있던 대화 — "그 전" 을 모르니 "새로 연결됐다" 고 말하면 틀린다 (agent#127 리뷰)."""
    _chat(client, room, Script())
    fake_server.repo_link_states.pop(room)
    _chat(client, room, Script())

    assert [len(found) for found in _saved_memos(fake_server, room)] == [0, 0]
    assert fake_server.repo_link_states[room] == {"repos": [REPO]}


def test_순서만_다른_칸은_바뀐_것이_아니다(client, fake_server, room, cluster):
    cluster_id = _cluster_of(fake_server, room)
    fake_server.link_repo(cluster_id, "acme/other")
    fake_server.repo_link_states[room] = {"repos": ["acme/other", REPO, REPO]}
    _chat(client, room, Script())

    assert _saved_memos(fake_server, room) == [[]]


def test_사용자_글이_알림처럼_시작해도_메모로_읽지_않는다(client, fake_server, room, cluster):
    """메모 표식은 사용자가 보낸 글에서 지운다 — 턴 경계와 화면 블록이 흔들리지 않게 (agent#127 리뷰)."""
    _chat(client, room, Script(("list_resources", {"kind": "pods"})), "파드")
    body = _chat(client, room, Script(), memo.MARK + " 이건 사용자 글")

    assert body["response"]["steps"] == []                            # 앞 턴의 블록이 섞이지 않는다
    assert _saved_memos(fake_server, room) == [[], []]


def test_연결된_레포의_앱이_아니면_PR_길_판단에_kubectl을_더_부르지_않는다(client, fake_server, room, cluster):
    """판별(argocd.inspect)이 읽은 Application 의 레포가 연결돼 있지 않으면 길 판단을 건너뛴다 (agent#127 리뷰 — 조회 예산)."""
    fake_server.unlink_repo(_cluster_of(fake_server, room), REPO)
    body = _chat(client, room, Script(("scale_resource", SCALE)), "3개")

    assert body["approvals"][0]["tool"] == "scale_resource"
    assert cluster.seen == [["get", "deployment", "web", "-o", "json", "-n", "shop"],
                            ["get", "applications.argoproj.io", "-A", "-o", "json"]]     # 판별 두 번뿐
