"""PR 길 — propose_git_change → PR(승인 전) → PR 머지 승인 카드 → 머지 → sync·health (DURO-134).

가드레일 훅을 진짜로 돌린다. 모델은 FunctionModel, server 는 메모리 가짜(tests/fake_server.py),
GitHub 와 클러스터는 tests/fake_github.py. 레포 내용은 tests/fixtures/manifests (plain YAML 앱 apps/plain).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelResponse
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from fake_github import REPO, FakeCluster, FakeGitHub
from helpers import GUIDANCE, USER, answer_model, new_room
from kukie_agent import server
from kukie_agent.agent import agent
from kukie_agent.gitops import github, route, watch
from kukie_agent.guardrail import gitops_hook

CHANGE_ARGS = {
    "kind": "deployment",
    "name": "web",
    "namespace": "shop",
    "change": {"what": "replicas", "count": 3},
    "intent": "web 레플리카를 3개로 늘린다.",
    "expected_effects": ["web 파드가 3개가 된다."],
    "side_effects": ["노드 자원을 더 쓴다."],
}


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github, "connect", fake.connect)
    return fake


@pytest.fixture
def cluster(monkeypatch, gh):
    fake = FakeCluster()

    def run(args, **kwargs):
        fake.merged_revision = gh.merges[-1]["sha"] if gh.merges else ""
        return fake.run(args, **kwargs)

    monkeypatch.setattr(route, "run_kubectl", run)
    monkeypatch.setattr(watch, "run_kubectl", run)
    monkeypatch.setattr(watch, "SYNC_WAIT", 0)
    return fake


@pytest.fixture
def client(dev_auth, fake_server, kubectl_ok, hook_passes, monkeypatch):
    async def guidance(plan, manifest_preview=None):
        return GUIDANCE

    monkeypatch.setattr(gitops_hook, "generate_decision_guidance", guidance)
    return TestClient(server.app)


@pytest.fixture
def room(fake_server):
    room_id = new_room(fake_server, shared=True)
    fake_server.link_repo(fake_server.conversation(room_id)["clusterId"], REPO)
    return room_id


def _model(args=None, seen=None):
    """첫 호출에 propose_git_change 를 부르고, 그 뒤엔 답만 낸다. seen 에 툴 결과(재시도 포함)를 모은다."""
    def call(messages, info):
        last = messages[-1]
        parts = [p for p in last.parts if isinstance(p, (ToolReturnPart, RetryPromptPart))
                 and p.tool_name == "propose_git_change"]
        if parts and not any(isinstance(p, UserPromptPart) for p in last.parts):
            if seen is not None:
                seen.extend(parts)
            return ModelResponse(parts=[ToolCallPart(tool_name="final_result",
                                                     args={"narration": "알겠습니다."}, tool_call_id="out")])
        return ModelResponse(parts=[ToolCallPart(tool_name="propose_git_change", args=dict(args or CHANGE_ARGS),
                                                 tool_call_id="call-1")])
    return FunctionModel(call)


def _chat(client, room, args=None, seen=None):
    with agent.override(model=_model(args, seen)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "web 3개로"}, headers=USER)
    assert r.status_code == 200, r.text
    return r.json()


def _card(client, room, args=None):
    body = _chat(client, room, args)
    assert body["kind"] == "approval", body
    return body["approvals"][0]


def _decide(client, room, approved=True):
    with agent.override(model=answer_model()):
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": approved},
                        headers=USER)
    assert r.status_code == 200, r.text
    return r.json()


def _plan(fake_server, room):
    plans = fake_server.plans_of(room)
    assert len(plans) == 1
    return plans[0]


def _tool_result(fake_server, room):
    """승인 뒤 재개 run 이 남긴 propose_git_change 의 툴 결과."""
    for run in fake_server.runs_of(room):
        for message in run["agentMessages"] or []:
            for part in message.get("parts", []):
                if part.get("part_kind") in ("tool-return", "retry-prompt") and part.get("tool_name") == "propose_git_change":
                    return part
    raise AssertionError("툴 결과가 없다")


# ── 승인 전: PR 을 열고 카드를 띄운다 ─────────────────────────

def test_PR을_승인_전에_열고_PR_머지_승인_카드를_띄운다(client, fake_server, room, gh, cluster):
    card = _card(client, room)

    pull = gh.pull_of(1)
    assert pull["state"] == "open" and pull["base"] == "main"
    assert pull["head_ref"].startswith("kukie/deployment-web-")
    assert gh.branches["main"] == "c0"                          # 기본 브랜치에는 직접 쓰지 않았다
    changed = gh.commits[pull["head_sha"]]["apps/plain/deployment.yaml"]
    assert "replicas: 3" in changed

    assert card["tool"] == "propose_git_change"
    assert card["command"] == ["github", "pr", "merge", REPO]
    assert card["decision_guidance"] == GUIDANCE
    view = card["gitops"]
    assert view["repository"] == REPO and view["base"] == "main" and view["branch"] == pull["head_ref"]
    assert view["pull_request"] == {"number": 1, "url": f"https://github.com/{REPO}/pull/1"}
    assert view["files"] == ["apps/plain/deployment.yaml"]
    assert "-  replicas: 1" in view["file_diff"] and "+  replicas: 3" in view["file_diff"]
    assert "replicas" in view["render_diff"]
    assert view["application"]["name"] == "shop-web" and view["application"]["self_heal"] is True
    assert view["notice"] == gitops_hook.NOTICE
    assert not {"base_sha", "route", "merge_methods", "conflict_keys"} & set(view)   # 내부 값은 카드에 없다

    plan = _plan(fake_server, room)
    assert plan["status"] == "WAITING_APPROVAL"
    assert plan["payload"]["pull_request"] == view["pull_request"]                  # offer 의 PR 칸 (DURO-131)
    assert plan["payload"]["gitops"]["conflict_keys"] == [f"git:{REPO}:apps/plain/deployment.yaml"]
    assert plan["payload"]["dry_run_result"]["status"] == "succeeded"
    assert "ghs_fake-token" not in str(plan) and "ghs_fake-token" not in str(card)   # 토큰은 어디에도 안 남는다


def test_ArgoCD가_관리하지_않으면_ModelRetry로_kubectl_길을_알려준다(client, fake_server, room, gh, cluster):
    cluster.managed = False
    seen: list = []
    body = _chat(client, room, seen=seen)

    assert body["kind"] == "answer"
    assert isinstance(seen[0], RetryPromptPart)
    assert "kubectl 길" in str(seen[0].content) and "ArgoCD 가 관리하지 않아요" in str(seen[0].content)
    assert fake_server.plans_of(room) == [] and gh.pulls == {}


def test_레포가_연결되지_않았으면_kubectl_길이다(client, fake_server, room, gh, cluster):
    fake_server.unlink_repo(fake_server.conversation(room)["clusterId"], REPO)
    seen: list = []
    _chat(client, room, seen=seen)

    assert "연결돼 있지 않아요" in str(seen[0].content)
    assert fake_server.plans_of(room) == [] and gh.pulls == {}


def test_HPA가_레플리카를_정하면_PR을_만들지_않는다(client, fake_server, room, gh, cluster):
    cluster.hpa = "web-hpa"
    seen: list = []
    _chat(client, room, seen=seen)

    assert "HPA(web-hpa)" in str(seen[0].content)
    assert gh.pulls == {}


def test_비밀_값이_든_diff는_PR로_만들지_않는다(client, fake_server, room, gh, cluster):
    args = {**CHANGE_ARGS, "change": {"what": "env", "values": {"GREETING": "Bearer abcdefghijklmnop"}}}
    seen: list = []
    _chat(client, room, args, seen=seen)

    assert "비밀 값" in str(seen[0].content)
    assert fake_server.plans_of(room) == [] and gh.pulls == {}


def test_같은_파일을_고치는_Kukie_PR이_열려_있으면_기다린다(client, fake_server, room, gh, cluster):
    _card(client, room)
    _decide(client, room, approved=False)                   # 카드는 닫혔지만
    gh.pulls[1]["state"] = "open"                          # 누가 PR 을 다시 열었다
    seen: list = []
    _chat(client, room, seen=seen)

    assert "같은 파일을 고치는 Kukie PR" in str(seen[0].content)
    assert len(gh.pulls) == 1


# ── 승인 · 거절 ───────────────────────────────────────────

def test_승인하면_머지하고_sync_health까지_보고한다(client, fake_server, room, gh, cluster):
    _card(client, room)
    head = gh.pull_of(1)["head_sha"]
    body = _decide(client, room)

    assert body["kind"] == "answer"
    assert gh.merges == [{"number": 1, "head_sha": head, "method": "squash",
                          "title": "[Kukie] web 레플리카를 3개로 늘린다. (#1)", "sha": gh.merges[0]["sha"]}]
    assert gh.pull_of(1)["head_ref"] not in gh.branches             # 머지한 브랜치는 지운다
    plan = _plan(fake_server, room)
    assert plan["status"] == "APPLIED"
    assert plan["executionResult"]["merge_commit"] == gh.merges[0]["sha"]
    assert plan["executionResult"]["sync_result"]["status"] == "healthy"
    actions = [c["action"] for name, c in fake_server.calls if name == "plan_action"]
    assert actions == ["dry-run", "offer", "decision", "start", "result"]

    step = body["response"]["steps"][0]
    assert step["step_label"] == "PR 머지" and step["access"] == "mutating"
    assert step["command"] == f"github pr merge {REPO}#1"
    assert "Healthy" in step["output"]


def test_sync가_실패하면_이유와_revert_제안을_싣는다(client, fake_server, room, gh, cluster):
    _card(client, room)
    cluster.app_status = {"sync": {"status": "Synced", "revision": "merge1" + "0" * 34},
                          "health": {"status": "Degraded", "message": "ImagePullBackOff"}}
    _decide(client, room)

    plan = _plan(fake_server, room)
    assert plan["status"] == "APPLIED"                      # 머지는 됐다
    assert plan["executionResult"]["sync_result"]["status"] == "degraded"
    result = _tool_result(fake_server, room)
    assert "ImagePullBackOff" in str(result["content"]) and "revert PR" in str(result["content"])


def test_자동_sync가_꺼진_앱은_기다리지_않고_안내한다(client, fake_server, room, gh, cluster):
    cluster.auto_sync = False
    _card(client, room)
    _decide(client, room)

    assert _plan(fake_server, room)["executionResult"]["sync_result"]["status"] == "manual"


def test_거절하면_PR을_닫고_브랜치를_지운다(client, fake_server, room, gh, cluster):
    _card(client, room)
    branch = gh.pull_of(1)["head_ref"]
    _decide(client, room, approved=False)

    assert gh.pull_of(1)["state"] == "closed" and not gh.pull_of(1)["merged"]
    assert branch not in gh.branches
    assert gh.comments and "거절" in gh.comments[0][1]
    assert _plan(fake_server, room)["status"] == "REJECTED"


def test_브랜치_보호로_머지가_막히면_PR을_남기고_안내한다(client, fake_server, room, gh, cluster):
    _card(client, room)
    gh.mergeable_state = "blocked"
    _decide(client, room)

    assert gh.merges == [] and gh.pull_of(1)["state"] == "open"   # PR 은 남는다 (우회하지 않는다)
    plan = _plan(fake_server, room)
    assert plan["status"] == "FAILED" and plan["failureReason"] == "EXECUTION_FAILED"
    assert "GitHub 에서 리뷰·머지가 필요해요" in plan["executionResult"]["stderr"]


def test_머지가_405로_거절돼도_PR을_남긴다(client, fake_server, room, gh, cluster):
    _card(client, room)
    gh.merge_error = (405, "At least 1 approving review is required by reviewers with write access.")
    _decide(client, room)

    assert gh.pull_of(1)["state"] == "open"
    assert "GitHub 에서 리뷰·머지가 필요해요" in _plan(fake_server, room)["executionResult"]["stderr"]


# ── 승인 순간 다시 판단 (STALE) ────────────────────────────────

def test_승인하는_사이_레포_연결이_끊기면_STALE로_닫고_머지하지_않는다(client, fake_server, room, gh, cluster):
    _card(client, room)
    fake_server.unlink_repo(fake_server.conversation(room)["clusterId"], REPO)
    _decide(client, room)

    plan = _plan(fake_server, room)
    assert plan["status"] == "STALE" and plan["decision"] is None
    assert gh.merges == []
    result = _tool_result(fake_server, room)
    assert "실행하지 않았다" in str(result["content"]) and "다시 제안" in str(result["content"])


def test_승인하는_사이_PR에_커밋이_더해지면_STALE로_닫고_PR을_닫는다(client, fake_server, room, gh, cluster):
    _card(client, room)
    gh.push_to_pull(1, "apps/plain/deployment.yaml", "kind: Deployment\n")
    _decide(client, room)

    assert _plan(fake_server, room)["status"] == "STALE"
    assert gh.merges == [] and gh.pull_of(1)["state"] == "closed"


def test_승인하는_사이_ArgoCD_앱의_경로가_바뀌면_STALE이다(client, fake_server, room, gh, cluster):
    _card(client, room)
    cluster.path = "apps/plain-v2"
    _decide(client, room)

    assert _plan(fake_server, room)["status"] == "STALE" and gh.merges == []


def test_PR_길이_아닌데_propose_git_change를_고집해도_run이_실패하지_않는다(client, fake_server, room, gh, cluster):
    """재시도 한도(1)를 쓴 뒤에는 ToolFailed 로 알린다 — 또 ModelRetry 면 run 이 500 으로 끝난다."""
    cluster.managed = False

    def stubborn(messages, info):
        calls = sum(isinstance(p, ToolCallPart) and p.tool_name == "propose_git_change"
                    for m in messages for p in getattr(m, "parts", []))
        if calls < 2:
            return ModelResponse(parts=[ToolCallPart(tool_name="propose_git_change", args=dict(CHANGE_ARGS),
                                                     tool_call_id=f"call-{calls}")])
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args={"narration": "네."})])

    with agent.override(model=FunctionModel(stubborn)):
        r = client.post(f"/conversations/{room}/chat", json={"text": "web 3개로"}, headers=USER)
    assert r.status_code == 200 and r.json()["kind"] == "answer"
