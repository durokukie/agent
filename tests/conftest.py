import os

# kukie_agent 를 import 하면 .env 가 환경에 올라오고 server 는 그때 계측을 켠다. 테스트가 개발자의 계측 서버로
# 프롬프트를 보내지 않게 그보다 먼저 빈 값으로 막는다 — load_dotenv 는 이미 있는 값을 덮지 않는다.
os.environ["LOGFIRE_TOKEN"] = ""
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

import httpx
import pytest
from pydantic_ai import models

from helpers import FAKE_COMMAND, GUIDANCE, MEMBER_URL
from kukie_agent import conversations, server
from kukie_agent.clusters import crypto
from kukie_agent.guardrail import action_plan, hook
from kukie_agent.kubectl import KubectlResult
from kukie_agent.store import db, reset_store_for_tests
from kukie_agent.tools import read as read_tools


models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _member_server_off_by_default(monkeypatch):
    """kukie_agent 를 import 하면 load_dotenv(".env") 가 개발자 .env 의 KUKIE_MEMBER_URL 을 환경에 올린다.

    그대로 두면 같은 테스트가 .env 가 있는 컴퓨터에서만 회원 서버 모드로 돈다 (CI 에는 .env 가 없다).
    flat 엔드포인트가 회원 서버 모드에서 닫히면서(#75) 실제로 갈렸다. 회원 서버 모드가 필요한 테스트는
    직접 켠다 (test_membership.py 의 spring 픽스처).
    """
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.delenv("KUKIE_ACCESS_COOKIE", raising=False)  # 쿠키 이름도 .env 에서 올라올 수 있다 (#79)
    # 개발 모드 스위치도 .env 에서 올라온다. flat 엔드포인트가 이제 이 스위치로만 열리므로(#82) 필요한 테스트가 직접 켠다
    monkeypatch.delenv("KUKIE_DEV_AUTH", raising=False)


@pytest.fixture(autouse=True)
def _own_database(tmp_path, monkeypatch):
    """어떤 테스트도 개발자의 ~/.kukie/kukie.db 를 열지 않는다.

    서버가 기동 때(lifespan) DB 를 열어 마이그레이션을 돌리므로, `with TestClient(app)` 만 해도 DB 가 열린다.
    기본 주소를 임시 파일로 돌리고 전역 store 를 비워 둔다 — store 픽스처는 그 위에 따로 임시 DB 를 연다.
    """
    monkeypatch.setenv("KUKIE_DATABASE_URL", f"sqlite:///{tmp_path / 'kukie.db'}")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_factory", None)
    monkeypatch.setattr(db, "_store", None)


# ── 골라 쓰는 준비물 ─────────────────────────────────────────
# 서버 테스트의 client 픽스처는 아래에서 필요한 것만 받아 조립한다.


@pytest.fixture
def dev_auth(monkeypatch):
    """개발 모드 — 회원 서버 없이 X-User 헤더로 사용자를 가른다. flat 엔드포인트도 이때만 열린다 (#82)."""
    monkeypatch.delenv("KUKIE_DEV_USER", raising=False)
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")


@pytest.fixture
def secret_key(monkeypatch):
    """클러스터 자격증명 암호화 키 (기획 04 §8)."""
    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())


@pytest.fixture
def empty_store(tmp_path):
    """빈 DB 와 빈 대화 목록."""
    conversations.registry.clear()
    return reset_store_for_tests(f"sqlite:///{tmp_path / 'test.db'}")


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
    """회원 서버(Spring)를 켜고 httpx 를 가짜로 바꾼다. respond(url, headers) 를 넘기면 부른 기록 list 를 돌려준다."""
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

        monkeypatch.setenv("KUKIE_MEMBER_URL", MEMBER_URL)
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        return calls

    return install
