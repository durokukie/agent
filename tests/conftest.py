import os

# kukie_agent 를 import 하면 .env 가 환경에 올라오고 server 는 그때 계측을 켠다. 테스트가 개발자의 계측 서버로
# 프롬프트를 보내지 않게 그보다 먼저 빈 값으로 막는다 — load_dotenv 는 이미 있는 값을 덮지 않는다.
os.environ["LOGFIRE_TOKEN"] = ""
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

import httpx
import pytest
from pydantic_ai import models

import fake_server as fake_server_module
from helpers import FAKE_COMMAND, GUIDANCE, SERVER_URL
from kukie_agent import conversations, server
from kukie_agent.guardrail import action_plan, hook
from kukie_agent.kubectl import KubectlResult
from kukie_agent.tools import read as read_tools


models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _forget_readable_namespaces(monkeypatch):
    """GPU 개요가 클러스터마다 기억하는 namespace 를 테스트마다 비운다 — 같은 클러스터 id 가 다른 테스트로 새지 않게."""
    from kukie_agent import clusters_api
    monkeypatch.setattr(clusters_api, "_readable_namespaces", {})
    monkeypatch.setattr(clusters_api, "_gpu_absent_checked", {})
    # 나눠 쓰는 개요도 비우고 끈다(TTL 0 이면 진행 중인 것도 나눠 쓰지 않는다) — 같은 개요를 두 번 불러 다른 kubectl
    # 결과를 보는 테스트가 많다. 나눠 쓰기를 보는 테스트(test_cluster_overview_share.py)만 켠다
    monkeypatch.setattr(clusters_api, "_shared_overviews", {})
    monkeypatch.setattr(clusters_api, "OVERVIEW_SHARE_TTL", 0)


#: ArgoCD 가 없는 클러스터의 kubectl 답 — 리소스도 Application 도 못 읽는다
NO_ARGOCD = KubectlResult(command="kubectl get", stdout="", success=False, exit_code=1,
                          stderr='error: the server doesn\'t have a resource type "applications"')


@pytest.fixture(autouse=True)
def _no_argocd_by_default(monkeypatch):
    """ArgoCD 판별(kukie_agent/argocd.py)의 kubectl 은 늘 실패한다 — 진짜 kubectl 을 부르지 않고, 다른 테스트는 지금처럼
    ArgoCD 없는 클러스터로 돈다. 판별을 보는 테스트(test_argocd.py)가 직접 바꿔 끼운다."""
    from kukie_agent import argocd
    monkeypatch.setattr(argocd, "run_kubectl", lambda *a, **k: NO_ARGOCD)
    # PR 길 판단·sync 지켜보기(kukie_agent/gitops)도 같은 클러스터를 본다 — 테스트가 진짜 kubectl·GitHub 를 부르지 않게
    from kukie_agent.gitops import github, route, watch
    monkeypatch.setattr(route, "run_kubectl", lambda *a, **k: NO_ARGOCD)
    monkeypatch.setattr(watch, "run_kubectl", lambda *a, **k: NO_ARGOCD)
    from kukie_agent.tools import repo as repo_tools
    monkeypatch.setattr(repo_tools, "run_kubectl", lambda *a, **k: NO_ARGOCD)    # 레포 읽기 툴의 Application 목록 (DURO-151)

    def no_github(*a, **k):
        raise AssertionError("테스트가 진짜 GitHub 를 부르려 했다 — tests/fake_github.py 를 끼워라")

    monkeypatch.setattr(github, "connect", no_github)


@pytest.fixture(autouse=True)
def _server_off_by_default(monkeypatch):
    """kukie_agent 를 import 하면 load_dotenv(".env") 가 개발자 .env 의 KUKIE_SERVER_URL 을 환경에 올린다.

    그대로 두면 같은 테스트가 .env 가 있는 컴퓨터에서만 Kukie API 서버 모드로 돈다 (CI 에는 .env 가 없다).
    flat 엔드포인트가 server 모드에서 닫히면서(#75) 실제로 갈렸다. server 모드가 필요한 테스트는 직접 켠다
    (member_server 픽스처). 내부 토큰도 지워 둔다 — 실수로 진짜 server 를 부르지 않게 (설정이 없으면 503).
    """
    monkeypatch.delenv("KUKIE_SERVER_URL", raising=False)
    monkeypatch.delenv("KUKIE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("KUKIE_ACCESS_COOKIE", raising=False)  # 쿠키 이름도 .env 에서 올라올 수 있다 (#79)
    # 개발 모드 스위치도 .env 에서 올라온다. flat 엔드포인트가 이제 이 스위치로만 열리므로(#82) 필요한 테스트가 직접 켠다
    monkeypatch.delenv("KUKIE_DEV_AUTH", raising=False)


# ── 골라 쓰는 준비물 ─────────────────────────────────────────
# 서버 테스트의 client 픽스처는 아래에서 필요한 것만 받아 조립한다.


@pytest.fixture
def dev_auth(monkeypatch):
    """개발 모드 — Kukie API 서버 없이 X-User 헤더로 사용자를 가른다. flat 엔드포인트도 이때만 열린다 (#82)."""
    monkeypatch.delenv("KUKIE_DEV_USER", raising=False)
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")


@pytest.fixture
def fake_server(monkeypatch):
    """메모리 가짜 Kukie API 서버 (tests/fake_server.py) 와 빈 대화 목록.

    server_api 의 함수들을 가짜로 바꿔 끼운다. 인증은 따로다 — 대화 테스트는 dev_auth(X-User) 로 사용자를 가르고,
    가짜는 그 id 로 권한을 판단한다 (진짜 server 는 Bearer 토큰으로 판단한다).
    """
    conversations.registry.clear()
    fake = fake_server_module.install(monkeypatch)
    yield fake
    conversations.registry.clear()


@pytest.fixture
def fake_kubeconfig(monkeypatch):
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))


@pytest.fixture
def plan_dir(monkeypatch, tmp_path):
    """홈의 실제 계획서를 읽지 않게."""
    monkeypatch.setattr(action_plan, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def fresh_session():
    """flat 엔드포인트의 세션은 프로세스에 하나라 테스트끼리 넘어가지 않게 비운다."""
    server._session = None
    yield
    server._session = None


@pytest.fixture
def kubectl_ok(monkeypatch):
    """읽기 툴의 kubectl 이 늘 성공한다. 인자 모양을 실제와 같게 둬서 호출이 바뀌면 깨진다."""
    def run(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        return KubectlResult(command=FAKE_COMMAND, stdout="nginx Running", stderr="", success=True)

    monkeypatch.setattr(read_tools, "run_kubectl", run)


@pytest.fixture
def hook_passes(monkeypatch, plan_dir):
    """변경 툴이 승인 카드까지 간다 — dry-run 은 성공, 판단 도움말은 helpers.GUIDANCE. 실제 실행은 테스트마다 정한다."""
    monkeypatch.setattr(hook, "run_kubectl", lambda *a, **k: KubectlResult(
        command="kubectl dry-run", stdout="ok\n", stderr="", success=True, exit_code=0))

    async def guidance(plan, manifest_preview=None):
        return GUIDANCE

    monkeypatch.setattr(hook, "generate_decision_guidance", guidance)


@pytest.fixture
def member_server(monkeypatch):
    """Kukie API 서버의 인증(GET /users/me)을 켜고 httpx 를 가짜로 바꾼다. respond(url, headers) 를 넘기면 부른 기록 list 를 돌려준다."""
    def install(respond):
        calls: list[dict] = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                calls.append({"url": url, "headers": headers or {}})
                return respond(url, headers or {})

        monkeypatch.setenv("KUKIE_SERVER_URL", SERVER_URL)
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        return calls

    return install
