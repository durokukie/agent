"""PR 길 — propose_git_change → PR(승인 전) → PR 머지 승인 카드 → 머지 → sync·health (DURO-134).

가드레일 훅을 진짜로 돌린다. 모델은 FunctionModel, server 는 메모리 가짜(tests/fake_server.py),
GitHub 와 클러스터는 tests/fake_github.py. 레포 내용은 tests/fixtures/manifests (plain YAML 앱 apps/plain).
"""
from __future__ import annotations

import os
import shutil

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
from kukie_agent.manifests.render import kustomize_bin

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
        fake.merged_revision = gh.branches["main"]          # 머지되면 base 끝이 머지 커밋이다
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
    _card(client, room)                                    # 이 방의 카드는 아직 결정 전 (PR #1 열림)
    other = new_room(fake_server, shared=True, cluster_id=fake_server.conversation(room)["clusterId"])
    seen: list = []
    _chat(client, other, seen=seen)

    assert "같은 파일을 고치는 Kukie PR" in str(seen[0].content)
    assert len(gh.pulls) == 1 and gh.pull_of(1)["state"] == "open"


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


def test_승인하는_사이_base_브랜치가_앞서면_STALE이다(client, fake_server, room, gh, cluster):
    """PR 파일이 그대로여도 다른 파일(kustomization.yaml 등)이 바뀌면 렌더 결과가 달라질 수 있다 (agent#126 리뷰)."""
    _card(client, room)
    gh.push_to_base("apps/plain/service.yaml", "kind: Service\n")
    _decide(client, room)

    assert _plan(fake_server, room)["status"] == "STALE"
    assert gh.merges == [] and gh.pull_of(1)["state"] == "closed"
    assert "다른 커밋이 들어왔어요" in str(_tool_result(fake_server, room)["content"])



def _resume(client, room):
    with agent.override(model=answer_model()):
        return client.post(f"/conversations/{room}/resume", headers=USER)


def test_승인_순간_조회가_실패하면_STALE이_아니라_다시_시도할_수_있다(client, fake_server, room, gh, cluster):
    """길을 정하지 못한 것은 길이 바뀐 증거가 아니다 — 승인·PR 을 버리지 않는다 (agent#126 리뷰)."""
    _card(client, room)
    cluster.unreachable = True
    with agent.override(model=answer_model()):
        r = client.post(f"/conversations/{room}/approve", json={"call_id": "call-1", "approved": True}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "RESUME_RETRYABLE"
    assert _plan(fake_server, room)["status"] == "WAITING_APPROVAL" and gh.pull_of(1)["state"] == "open"

    cluster.unreachable = False
    assert _resume(client, room).status_code == 200
    assert _plan(fake_server, room)["status"] == "APPLIED" and len(gh.merges) == 1


@pytest.mark.parametrize("method", ["squash", "rebase", "merge"])
def test_GitHub에서_먼저_머지됐으면_반영된_것으로_보고한다(client, fake_server, room, gh, cluster, method):
    """승인한 내용 그대로 머지됐다 — STALE("실행 안 함")은 사실과 다르다. 머지 방식 셋 다 (agent#126 리뷰)."""
    _card(client, room)
    sha = gh.merge_elsewhere(1, method=method)
    _decide(client, room)

    plan = _plan(fake_server, room)
    assert plan["status"] == "APPLIED" and plan["executionResult"]["merge_commit"] == sha
    assert plan["executionResult"]["sync_result"]["status"] == "healthy"
    assert gh.merges == [] and gh.comments == []                  # Kukie 는 머지하지도, "닫았다" 고 남기지도 않았다
    assert "이미 머지됐어요" in plan["executionResult"]["stdout"]


def test_결정_없이_만료된_카드의_PR은_다음_제안이_닫는다(client, fake_server, room, gh, cluster):
    """아무도 안 닫으면 그 파일의 PR 길이 영영 막힌다 (agent#126 리뷰)."""
    _card(client, room)
    first = _plan(fake_server, room)
    assert f"<!-- kukie-plan: {first['id']} -->" in gh.pull_of(1)["body"]
    fake_server.plans[first["id"]]["status"] = "EXPIRED"          # 카드가 결정 없이 만료됐다
    fake_server.runs_of(room)[0]["status"] = "interrupted"
    from kukie_agent import conversations
    conversations.registry.clear()                                 # 재시작 — 메모리 티켓도 없다

    _card(client, room)
    assert gh.pull_of(1)["state"] == "closed" and gh.pull_of(1)["head_ref"] not in gh.branches
    assert "만료" in gh.comments[0][1]
    assert gh.pull_of(2)["state"] == "open"


def test_판단_가이드에는_렌더_diff_한_벌만_간다(client, fake_server, room, gh, cluster, monkeypatch):
    seen = []

    async def guidance(plan, manifest_preview=None):
        seen.append(plan.render_markdown(include_decision_guidance=False))
        return GUIDANCE

    monkeypatch.setattr(gitops_hook, "generate_decision_guidance", guidance)
    _card(client, room)

    text = seen[0]
    assert text.count("replicas: 3") == 1                          # render_diff 하나
    assert "file_diff" not in text and "base_sha" not in text and "route" not in text
    assert "render_diff" in text


def test_먼저_머지됐어도_다른_base_위면_반영으로_보지_않는다(client, fake_server, room, gh, cluster):
    """그 사이 base 에 다른 커밋이 들어왔으면 렌더 결과가 승인 카드와 다를 수 있다 (agent#126 리뷰)."""
    _card(client, room)
    gh.push_to_base("apps/plain/service.yaml", "kind: Service\n")
    gh.merge_elsewhere(1, on=gh.branches["main"], method="merge")
    _decide(client, room)

    plan = _plan(fake_server, room)
    assert plan["status"] == "STALE" and gh.comments == []           # 이미 머지된 PR 에 "닫았다" 고 남기지 않는다
    assert "클러스터 상태를 직접 확인" in str(_tool_result(fake_server, room)["content"])


@pytest.mark.parametrize("status,closed", [
    ("EXPIRED", True), ("STALE", True), ("FAILED", False), ("UNKNOWN", False), ("APPLIED", False),
])
def test_고아_PR은_승인_없이_끝난_계획의_것만_닫는다(client, fake_server, room, gh, cluster, status, closed):
    """브랜치 보호로 남긴 PR(FAILED)·머지 여부 모름(UNKNOWN)을 닫으면 승인된 변경이 사라진다 (agent#126 리뷰)."""
    _card(client, room)
    first = _plan(fake_server, room)
    fake_server.plans[first["id"]]["status"] = status
    if status != "EXPIRED" and status != "STALE":
        fake_server.plans[first["id"]]["decision"] = {"approved": True, "user_id": "u-1", "at": "…"}
    other = new_room(fake_server, shared=True, cluster_id=fake_server.conversation(room)["clusterId"])
    _chat(client, other)

    assert (gh.pull_of(1)["state"] == "closed") is closed
    assert len(gh.pulls) == (2 if closed else 1)


def test_고아_PR을_닫은_뒤_정리가_실패해도_막지_않는다(client, fake_server, room, gh, cluster, monkeypatch):
    """잠금을 푸는 것은 닫기 하나다 — 코멘트·브랜치 삭제 실패로 "이미 열려 있어요" 가 되면 안 된다 (agent#126 리뷰)."""
    from kukie_agent.gitops.github import GitHubError

    _card(client, room)
    first = _plan(fake_server, room)
    fake_server.plans[first["id"]]["status"] = "EXPIRED"

    async def broken(number, body):
        raise GitHubError(502, "Bad Gateway")

    monkeypatch.setattr(gh, "comment", broken)
    other = new_room(fake_server, shared=True, cluster_id=fake_server.conversation(room)["clusterId"])
    body = _chat(client, other)

    assert gh.pull_of(1)["state"] == "closed"
    assert body["kind"] == "approval" and gh.pull_of(2)["state"] == "open"


def test_고아_PR을_닫지_못하면_막는다(client, fake_server, room, gh, cluster, monkeypatch):
    from kukie_agent.gitops.github import GitHubError

    _card(client, room)
    fake_server.plans[_plan(fake_server, room)["id"]]["status"] = "EXPIRED"

    async def broken(number):
        raise GitHubError(502, "Bad Gateway")

    monkeypatch.setattr(gh, "close_pull", broken)
    other = new_room(fake_server, shared=True, cluster_id=fake_server.conversation(room)["clusterId"])
    seen: list = []
    _chat(client, other, seen=seen)

    assert "같은 파일을 고치는 Kukie PR" in str(seen[0].content) and len(gh.pulls) == 1


# ── 새 리소스 (DURO-154) ──────────────────────────────────

CADDY_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: caddy
spec:
  selector:
    matchLabels: {app: caddy}
  template:
    metadata:
      labels: {app: caddy}
    spec:
      containers:
        - name: caddy
          image: caddy:2.8.4
---
apiVersion: v1
kind: Service
metadata:
  name: caddy
spec:
  selector: {app: caddy}
  ports:
    - port: 80
"""
NEW_ARGS = {
    "kind": "Deployment",
    "name": "caddy",
    "change": {"what": "new_resource", "application": "shop-web", "manifest_yaml": CADDY_YAML},
    "intent": "caddy 를 새로 띄운다.",
    "expected_effects": ["caddy 파드와 Service 가 생긴다."],
    "side_effects": ["노드 자원을 더 쓴다."],
}


def _new_args(**change):
    return {**NEW_ARGS, "change": {**NEW_ARGS["change"], **change}}


needs_kustomize = pytest.mark.skipif(
    shutil.which(kustomize_bin()) is None and not os.environ.get("CI"),
    reason="kustomize 가 없다 (CI 에서는 깔고 돈다)")


def test_새_리소스는_클러스터에_없어도_고른_앱으로_PR을_열고_경고_없는_카드를_띄운다(client, fake_server, room, gh, cluster):
    card = _card(client, room, NEW_ARGS)

    pull = gh.pull_of(1)
    assert pull["head_ref"].startswith("kukie/deployment-caddy-") and gh.branches["main"] == "c0"
    assert gh.commits[pull["head_sha"]]["apps/plain/caddy.yaml"] == CADDY_YAML
    assert gh.commits[pull["head_sha"]]["apps/plain/service.yaml"] == gh.commits["c0"]["apps/plain/service.yaml"]

    assert card["tool"] == "propose_git_change"                 # 앱은 이 이름으로 PR 카드를 그린다
    assert card["target"]["namespace"] == "shop" and card["target"]["kind"] == "Deployment"
    view = card["gitops"]
    assert view["files"] == ["apps/plain/caddy.yaml"]
    assert view["file_diff"].startswith("--- /dev/null")
    assert "Service caddy (머지 후)" in view["render_diff"]
    assert view["risk_warnings"] == [] and view["warnings"] == []
    assert view["new_resources"] == [
        {"api_version": "apps/v1", "kind": "Deployment", "name": "caddy", "namespace": "shop", "file": "apps/plain/caddy.yaml"},
        {"api_version": "v1", "kind": "Service", "name": "caddy", "namespace": "shop", "file": "apps/plain/caddy.yaml"},
    ]
    assert "확인할 점" not in pull["body"]

    plan = _plan(fake_server, room)
    change = plan["payload"]["args"]["change"]
    assert "manifest_yaml" not in change and len(change["manifest_sha256"]) == 64     # Plan 의 args 에는 해시만
    assert plan["payload"]["gitops"]["risk_warnings"] == []


def test_경고가_있는_새_리소스는_카드와_Plan과_PR_본문에_같은_경고를_싣는다(client, fake_server, room, gh, cluster):
    manifest = CADDY_YAML.replace("caddy:2.8.4", "caddy:latest")
    card = _card(client, room, _new_args(manifest_yaml=manifest))

    [risk] = card["gitops"]["risk_warnings"]
    assert risk["code"] == "latest_image"
    assert risk["location"] == {"file": "apps/plain/caddy.yaml", "resource": "Deployment shop/caddy",
                                "field": "spec.template.spec.containers[caddy].image", "value": "caddy:latest"}
    assert risk["risk"] and risk["suggestion"]
    assert card["gitops"]["warnings"] == [risk["text"]]         # 옛 앱은 이 줄로 같은 경고를 본다
    assert _plan(fake_server, room)["payload"]["gitops"]["risk_warnings"] == [risk]
    assert risk["text"] in gh.pull_of(1)["body"]


def test_클러스터에_이미_있는_이름이면_덮어쓴다고_경고한다(client, fake_server, room, gh, cluster):
    cluster.live = {"caddy"}
    card = _card(client, room, NEW_ARGS)

    risks = card["gitops"]["risk_warnings"]
    assert [risk["code"] for risk in risks] == ["already_in_cluster", "already_in_cluster"]
    assert risks[0]["location"]["resource"] == "Deployment shop/caddy"
    assert risks[0]["text"] in card["gitops"]["warnings"]


def test_Cluster로_시작해도_네임스페이스가_있는_종류면_그_네임스페이스에서_이미_있는지_본다(client, fake_server, room, gh, cluster):
    cluster.live = {"caddy"}
    manifest = "apiVersion: operators.coreos.com/v1alpha1\nkind: ClusterServiceVersion\nmetadata: {name: caddy}\n"
    card = _card(client, room, _new_args(manifest_yaml=manifest) | {"kind": "ClusterServiceVersion"})

    assert [risk["code"] for risk in card["gitops"]["risk_warnings"]] == ["already_in_cluster"]
    assert ["get", "ClusterServiceVersion", "caddy", "-o", "json", "-n", "shop"] in cluster.seen


def test_앱의_include_exclude는_판단에서_읽어_경고로_올린다(client, fake_server, room, gh, cluster):
    cluster.directory = {"include": "*.yaml"}
    card = _card(client, room, NEW_ARGS)
    assert [risk["code"] for risk in card["gitops"]["risk_warnings"]] == ["directory_filter"]


def test_새_리소스를_승인하면_머지하고_sync까지_지켜본다(client, fake_server, room, gh, cluster):
    _card(client, room, NEW_ARGS)
    head = gh.pull_of(1)["head_sha"]
    _decide(client, room)

    assert gh.merges[0]["head_sha"] == head
    assert gh.commits[gh.merges[0]["sha"]]["apps/plain/caddy.yaml"] == CADDY_YAML
    plan = _plan(fake_server, room)
    assert plan["status"] == "APPLIED" and plan["executionResult"]["sync_result"]["status"] == "healthy"
    assert [c["action"] for name, c in fake_server.calls if name == "plan_action"] == ["dry-run", "offer", "decision", "start", "result"]


def test_새_리소스의_sync가_실패하면_없는_길을_제안하지_않는다(client, fake_server, room, gh, cluster):
    _card(client, room, NEW_ARGS)
    cluster.app_status = {"sync": {"status": "OutOfSync"},
                          "operationState": {"phase": "Failed", "message": "not permitted in project",
                                             "syncResult": {"revision": "merge1" + "0" * 34}}}
    _decide(client, room)

    assert _plan(fake_server, room)["executionResult"]["sync_result"]["status"] == "sync_failed"
    content = str(_tool_result(fake_server, room)["content"])
    assert "not permitted in project" in content                # ArgoCD 프로젝트가 막은 이유가 채팅까지 간다
    assert "GitHub 에서 이 PR 을 revert" in content and "propose_git_change" not in content


def test_새_리소스도_거절하면_PR을_닫는다(client, fake_server, room, gh, cluster):
    _card(client, room, NEW_ARGS)
    _decide(client, room, approved=False)

    assert gh.pull_of(1)["state"] == "closed" and _plan(fake_server, room)["status"] == "REJECTED"


def test_새_리소스도_승인하는_사이_레포_연결이_끊기면_STALE이다(client, fake_server, room, gh, cluster):
    _card(client, room, NEW_ARGS)
    fake_server.unlink_repo(fake_server.conversation(room)["clusterId"], REPO)
    _decide(client, room)

    assert _plan(fake_server, room)["status"] == "STALE" and gh.merges == []


def test_새_리소스도_승인하는_사이_base가_앞서면_STALE이다(client, fake_server, room, gh, cluster):
    _card(client, room, NEW_ARGS)
    gh.push_to_base("apps/plain/caddy.yaml", "# 누가 먼저 넣었다\n")
    _decide(client, room)

    assert _plan(fake_server, room)["status"] == "STALE" and gh.merges == []


@pytest.mark.parametrize("field, value", [("destination", "other"), ("destination_server", "https://other.example")])
@pytest.mark.parametrize("args", [NEW_ARGS, CHANGE_ARGS], ids=["새_리소스", "기존_변경"])
def test_승인하는_사이_앱의_destination이_바뀌면_STALE이고_머지하지_않는다(client, fake_server, room, gh, cluster, args, field, value):
    _card(client, room, args)
    setattr(cluster, field, value)
    _decide(client, room)

    assert _plan(fake_server, room)["status"] == "STALE" and gh.merges == []
    assert gh.pull_of(1)["state"] == "closed"
    result = str(_tool_result(fake_server, room)["content"])
    assert "실행하지 않았다" in result and "다시 제안" in result


def test_새_리소스도_승인_대조에서_context가_달라지면_멈춘다(client, fake_server, room, gh, cluster):
    _card(client, room, NEW_ARGS)
    fake_server.conversation(room)["context"] = "다른-클러스터"
    from kukie_agent import conversations
    conversations.registry.clear()                                 # 재시작 — 새 context 로 방을 다시 읽는다
    _decide(client, room)

    plan = _plan(fake_server, room)
    assert gh.merges == [] and plan["decision"] is None and plan["payload"]["target"]["namespace"] == "shop"
    assert "target" in str(_tool_result(fake_server, room)["content"])


@needs_kustomize
def test_Kustomize_앱은_overlay에_새_파일과_resources_한_줄을_올린다(client, fake_server, room, gh, cluster):
    cluster.source_type, cluster.path, cluster.destination = "Kustomize", "apps/kustomize/overlays/prod", "shop-prod"
    card = _card(client, room, NEW_ARGS)

    head = gh.commits[gh.pull_of(1)["head_sha"]]
    assert head["apps/kustomize/overlays/prod/caddy.yaml"] == CADDY_YAML
    assert "  - caddy.yaml\n" in head["apps/kustomize/overlays/prod/kustomization.yaml"]
    assert head["apps/kustomize/base/kustomization.yaml"] == gh.commits["c0"]["apps/kustomize/base/kustomization.yaml"]
    view = card["gitops"]
    assert view["files"] == ["apps/kustomize/overlays/prod/caddy.yaml", "apps/kustomize/overlays/prod/kustomization.yaml"]
    assert view["risk_warnings"] == [] and "Deployment shop-prod/caddy (머지 후)" in view["render_diff"]
    assert card["target"]["namespace"] == "shop-prod"


@pytest.mark.parametrize("setup, why", [
    (lambda c: setattr(c, "source_type", "Helm"), "Helm 앱"),
    (lambda c: setattr(c, "no_argocd", True), "ArgoCD 가 없어요"),
    (lambda c: setattr(c, "same_name_in", "argocd-2"), "application_namespace"),
    (lambda c: setattr(c, "repo_url", "https://gitlab.com/acme/manifests.git"), "GitHub 레포가 아니라"),
])
def test_PR로_못_더하는_앱이면_이유와_다른_길을_알려준다(client, fake_server, room, gh, cluster, setup, why):
    setup(cluster)
    seen: list = []
    _chat(client, room, NEW_ARGS, seen=seen)

    content = str(seen[0].content)
    assert why in content and "apply_manifest" in content and "kubectl 길" not in content
    assert fake_server.plans_of(room) == [] and gh.pulls == {}


def test_레포가_연결되지_않은_앱이면_연결하라고_알려준다(client, fake_server, room, gh, cluster):
    fake_server.unlink_repo(fake_server.conversation(room)["clusterId"], REPO)
    seen: list = []
    _chat(client, room, NEW_ARGS, seen=seen)

    assert "연결돼 있지 않아요" in str(seen[0].content) and "레포를 연결" in str(seen[0].content)
    assert gh.pulls == {}


def test_앱을_못_찾으면_있는_앱을_알려준다(client, fake_server, room, gh, cluster):
    seen: list = []
    _chat(client, room, _new_args(application="nope"), seen=seen)

    assert "'nope' 를 이 클러스터에서 찾지 못했어요" in str(seen[0].content) and "shop-web" in str(seen[0].content)


def test_같은_이름의_앱이_여럿이어도_네임스페이스를_주면_고른다(client, fake_server, room, gh, cluster):
    cluster.same_name_in = "argocd-2"
    assert _card(client, room, _new_args(application_namespace="argocd"))["gitops"]["application"]["namespace"] == "argocd"


def test_막히면_다음_방법을_담아_다시_하게_하고_PR을_만들지_않는다(client, fake_server, room, gh, cluster):
    manifest = "apiVersion: v1\nkind: Secret\nmetadata: {name: caddy}\nstringData: {password: hunter2}\n"
    seen: list = []
    _chat(client, room, _new_args(manifest_yaml=manifest) | {"kind": "Secret"}, seen=seen)

    assert isinstance(seen[0], RetryPromptPart) and "ExternalSecret" in str(seen[0].content)
    assert fake_server.plans_of(room) == [] and gh.pulls == {}


def test_매니페스트_값에_토큰이_보이면_PR을_만들지_않는다(client, fake_server, room, gh, cluster):
    manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: caddy}\ndata: {auth: 'Bearer abcdefghijklmnop'}\n"
    seen: list = []
    _chat(client, room, _new_args(manifest_yaml=manifest) | {"kind": "ConfigMap"}, seen=seen)

    assert "비밀 값" in str(seen[0].content) and gh.pulls == {}


def test_Secret을_가리키기만_하는_줄은_막지_않는다(client, fake_server, room, gh, cluster):
    manifest = ("apiVersion: acme.example/v1\nkind: Widget\nmetadata: {name: caddy}\nspec:\n  credentials:\n"
                "    kind: Secret\n    name: db\n")
    card = _card(client, room, _new_args(manifest_yaml=manifest) | {"kind": "Widget"})
    assert card["gitops"]["files"] == ["apps/plain/caddy.yaml"]


def test_기존_네_가지_변경의_카드에는_새_칸이_없다(client, fake_server, room, gh, cluster):
    view = _card(client, room)["gitops"]
    assert "risk_warnings" not in view and "new_resources" not in view


def test_클러스터_조회에는_옵션처럼_보이는_이름을_넘기지_않는다(cluster):
    """namePrefix·namespace 는 고객 레포의 값이다 — kubectl 인자로 그대로 넘기면 옵션으로 읽힐 수 있다."""
    resources = [
        {"kind": "Deployment", "name": "--kubeconfig=/x", "namespace": "shop"},
        {"kind": "Deployment", "name": "ok", "namespace": "--context=x"},
        {"kind": "--flag", "name": "ok", "namespace": "shop"},
    ]
    assert route.live_resources(resources, context="ctx", kubeconfig=None) == []
    assert cluster.seen == []
