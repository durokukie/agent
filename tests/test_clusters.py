"""클러스터 등록과 kubeconfig 보관 — issue #61, 기획 04 §8.

거부해야 할 kubeconfig 를 거부하는지가 절반이다. 서버가 남의 kubeconfig 를 그대로 쓰면
임의 명령 실행(exec)과 서버 파일 읽기(경로 참조)가 열린다.
"""
from __future__ import annotations

import base64

import pytest
import yaml
from fastapi.testclient import TestClient

from kukie import conversations, server
from kukie.clusters import crypto, runtime
from kukie.clusters.kubeconfig import KubeconfigRejected, parse_kubeconfig
from kukie.kubectl import KubectlResult
from kukie.store import get_store, reset_store_for_tests
from kukie.tools import read as read_tools

USER = {"X-User": "u-1"}
OTHER = {"X-User": "u-2"}
CA = base64.b64encode(b"fake-ca").decode()


def kubeconfig(
    *, server_url="https://api.example.com", ca=CA, user=None, context="prod", extra_context=None
) -> str:
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c", "cluster": {"server": server_url, "certificate-authority-data": ca}}],
        "users": [{"name": "u", "user": user if user is not None else {"token": "secret-token"}}],
        "contexts": [{"name": context, "context": {"cluster": "c", "user": "u", "namespace": "web"}}],
        "current-context": context,
    }
    if extra_context:
        document["contexts"].append(
            {"name": extra_context, "context": {"cluster": "c", "user": "u"}}
        )
        document.pop("current-context")
    return yaml.safe_dump(document)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.delenv("KUKIE_DEV_USER", raising=False)
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")
    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())
    monkeypatch.delenv("KUKIE_ALLOW_LOCAL_CLUSTER", raising=False)
    reset_store_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    conversations.registry.clear()
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    return TestClient(server.app)


def _register(client, *, server_kubeconfig: str | None = None, **body) -> dict:
    body.setdefault("kubeconfig", server_kubeconfig or kubeconfig())
    body.setdefault("name", "운영")
    r = client.post("/clusters", json=body, headers=USER)
    assert r.status_code == 200, r.text
    return r.json()


# ── kubeconfig 검사 (기획 04 §8 "업로드 검증") ─────────────

def test_토큰_kubeconfig에서_필요한_값만_뽑는다():
    parsed = parse_kubeconfig(kubeconfig())
    assert parsed.api_server == "https://api.example.com"
    assert parsed.ca_data == CA
    assert parsed.context_name == "prod" and parsed.namespace == "web"
    assert parsed.credential == {"token": "secret-token"}
    assert len(parsed.fingerprint) == 64


def test_client_certificate_방식도_받는다():
    cert, key = base64.b64encode(b"cert").decode(), base64.b64encode(b"key").decode()
    parsed = parse_kubeconfig(
        kubeconfig(user={"client-certificate-data": cert, "client-key-data": key})
    )
    assert parsed.credential == {"client_certificate_data": cert, "client_key_data": key}


def test_exec_플러그인은_거부한다():
    """서버에서 임의 명령을 실행하게 되는 통로다 — Cloud Identity 지원 전까지 막는다."""
    body = kubeconfig(user={"exec": {"command": "aws", "args": ["eks", "get-token"]}})
    with pytest.raises(KubeconfigRejected, match="외부 인증 프로그램"):
        parse_kubeconfig(body)


@pytest.mark.parametrize("field", ["client-certificate", "client-key", "tokenFile"])
def test_파일_경로_참조는_거부한다(field):
    """서버의 파일을 읽으라는 뜻이 된다. 인라인 값만 받는다."""
    with pytest.raises(KubeconfigRejected, match="파일 경로"):
        parse_kubeconfig(kubeconfig(user={field: "/etc/secret"}))


def test_certificate_authority_경로도_거부한다():
    body = yaml.safe_load(kubeconfig())
    body["clusters"][0]["cluster"] = {
        "server": "https://api.example.com",
        "certificate-authority": "/etc/ca.crt",
    }
    with pytest.raises(KubeconfigRejected, match="파일 경로"):
        parse_kubeconfig(yaml.safe_dump(body))


def test_http_주소는_거부한다():
    with pytest.raises(KubeconfigRejected, match="https"):
        parse_kubeconfig(kubeconfig(server_url="http://api.example.com"))


def test_사설_주소는_기본으로_거부하고_허용하면_받는다():
    """kind 는 127.0.0.1 이다. 서버가 노트북 밖으로 나가면 닿지 않으므로 기본은 막는다."""
    body = kubeconfig(server_url="https://127.0.0.1:6443")
    with pytest.raises(KubeconfigRejected, match="사설"):
        parse_kubeconfig(body)
    assert parse_kubeconfig(body, allow_local=True).api_server == "https://127.0.0.1:6443"


def test_context가_여럿이고_current가_없으면_고르라고_한다():
    body = kubeconfig(extra_context="staging")
    with pytest.raises(KubeconfigRejected, match="하나를 골라"):
        parse_kubeconfig(body)
    assert parse_kubeconfig(body, context_name="staging").context_name == "staging"


def test_아이디_비밀번호_방식은_거부한다():
    with pytest.raises(KubeconfigRejected, match="아이디"):
        parse_kubeconfig(kubeconfig(user={"username": "a", "password": "b"}))


def test_base64가_아닌_CA는_거부한다():
    with pytest.raises(KubeconfigRejected, match="base64"):
        parse_kubeconfig(kubeconfig(ca="이건 base64 가 아니다"))


# ── 암호화 ─────────────────────────────────────────────────

def test_자격증명은_암호문으로만_저장된다(client):
    registered = _register(client)
    stored = get_store().cluster_credential(registered["id"])
    assert stored is not None and "secret-token" not in stored
    assert crypto.decrypt(stored) == {"token": "secret-token"}


def test_키가_없으면_등록을_거부한다(client, monkeypatch):
    monkeypatch.delenv(crypto.KEY_ENV)
    r = client.post("/clusters", json={"kubeconfig": kubeconfig(), "name": "운영"}, headers=USER)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SECRET_KEY_MISSING"


# ── 엔드포인트 ─────────────────────────────────────────────

def test_등록_응답에_자격증명이_없다(client):
    """기획 04 §4 — Credential 값은 어떤 API 응답에도 다시 내보내지 않는다."""
    registered = _register(client)
    assert "secret-token" not in str(registered)
    assert set(registered) == {
        "id", "team_id", "registered_by", "name", "provider", "api_server", "context",
        "namespace", "fingerprint", "status", "insecure", "last_checked_at",
        "created_at", "updated_at",
    }
    assert registered["status"] == "disconnected"   # 아직 확인 전


def test_잘못된_kubeconfig는_400과_이유를_준다(client):
    r = client.post(
        "/clusters",
        json={"kubeconfig": kubeconfig(user={"exec": {"command": "aws"}}), "name": "운영"},
        headers=USER,
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "KUBECONFIG_REJECTED"
    assert "외부 인증 프로그램" in r.json()["detail"]["message"]


def test_남의_클러스터는_보이지_않는다(client):
    registered = _register(client)
    assert client.get(f"/clusters/{registered['id']}", headers=OTHER).status_code == 404
    assert client.get("/clusters", headers=OTHER).json() == []
    assert [c["id"] for c in client.get("/clusters", headers=USER).json()] == [registered["id"]]


def test_인증_없으면_401(client):
    assert client.get("/clusters").status_code == 401


def test_연결_확인은_상태를_갱신한다(client, monkeypatch):
    registered = _register(client)
    seen: list[dict] = []

    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        seen.append({"args": args, "kubeconfig": kubeconfig})
        ok = args[0] == "version"
        return KubectlResult(command="kubectl …", stdout="yes" if not ok else "{}", stderr="",
                             success=True, exit_code=0)

    monkeypatch.setattr("kukie.clusters_api.run_kubectl", fake)
    r = client.post(f"/clusters/{registered['id']}/test", headers=USER)

    assert r.status_code == 200
    assert r.json()["status"] == "connected" and r.json()["can_change"] is True
    # 서버의 기본 kubeconfig 가 아니라 임시 파일로 실행했다
    assert all(call["kubeconfig"] is not None for call in seen)
    assert client.get(f"/clusters/{registered['id']}", headers=USER).json()["status"] == "connected"


def test_연결_실패는_이유에_따라_상태가_갈린다(client, monkeypatch):
    registered = _register(client)
    monkeypatch.setattr(
        "kukie.clusters_api.run_kubectl",
        lambda *a, **k: KubectlResult(command="kubectl …", stdout="", stderr="error: Unauthorized",
                                      success=False, exit_code=1),
    )
    assert client.post(f"/clusters/{registered['id']}/test", headers=USER).json()["status"] == "auth_expired"


def test_자격증명_교체는_같은_클러스터만_된다(client):
    registered = _register(client)
    다른곳 = kubeconfig(server_url="https://other.example.com")
    r = client.patch(f"/clusters/{registered['id']}", json={"kubeconfig": 다른곳}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CLUSTER_MISMATCH"

    같은곳 = kubeconfig(user={"token": "새-토큰"})
    r = client.patch(f"/clusters/{registered['id']}", json={"kubeconfig": 같은곳}, headers=USER)
    assert r.status_code == 200 and r.json()["status"] == "disconnected"   # 바꿨으니 다시 확인해야
    assert crypto.decrypt(get_store().cluster_credential(registered["id"])) == {"token": "새-토큰"}


def test_삭제하면_자격증명도_사라진다(client):
    registered = _register(client)
    assert client.delete(f"/clusters/{registered['id']}", headers=USER).status_code == 200
    assert get_store().cluster_credential(registered["id"]) is None


def test_쓰는_대화가_있으면_삭제를_막는다(client):
    registered = _register(client)
    client.post("/conversations", json={"cluster_id": registered["id"]}, headers=USER)
    r = client.delete(f"/clusters/{registered['id']}", headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CLUSTER_IN_USE"


# ── 대화와의 연결 ──────────────────────────────────────────

def test_방을_만들_때_클러스터의_대상과_지문을_복사한다(client):
    """기획 04 §8 — 방이 지문을 들고 있어야 "승인한 대상 = 실행 대상" 을 확인할 수 있다."""
    registered = _register(client)
    created = client.post(
        "/conversations", json={"cluster_id": registered["id"], "context": "무시됨"}, headers=USER
    ).json()

    assert created["session"]["context"] == "prod"       # 화면이 보낸 값이 아니라 등록된 값
    assert created["session"]["namespace"] == "web"
    row = get_store().get_session(created["conversation"]["id"])
    assert row is not None and row.cluster_fingerprint == registered["fingerprint"]


def test_없는_클러스터를_고르면_404(client):
    r = client.post("/conversations", json={"cluster_id": "없음"}, headers=USER)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"


def test_등록된_클러스터로_대화하면_임시_kubeconfig로_실행한다(client, monkeypatch):
    registered = _register(client)
    room = client.post("/conversations", json={"cluster_id": registered["id"]},
                       headers=USER).json()["conversation"]["id"]
    seen: list[object] = []

    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        seen.append(kubeconfig)
        # 실행 순간에는 파일이 살아 있어야 한다
        assert kubeconfig is not None and kubeconfig.exists()
        assert "secret-token" in kubeconfig.read_text(encoding="utf-8")
        return KubectlResult(command="kubectl …", stdout="nginx Running", stderr="",
                             success=True, exit_code=0)

    monkeypatch.setattr(read_tools, "run_kubectl", fake)
    from pydantic_ai.models.test import TestModel
    from kukie.agent import agent

    with agent.override(model=TestModel(call_tools=["list_resources"],
                                        custom_output_args={"narration": "봤습니다.",
                                                            "suggested_next_action": None})):
        r = client.post(f"/conversations/{room}/chat", json={"text": "파드 보여줘"}, headers=USER)

    assert r.status_code == 200, r.text
    assert seen and all(path is not None for path in seen)
    assert not seen[0].exists()      # 요청이 끝나면 파일은 사라진다


def test_클러스터가_지워진_대화는_안내를_준다(client):
    registered = _register(client)
    room = client.post("/conversations", json={"cluster_id": registered["id"]},
                       headers=USER).json()["conversation"]["id"]
    get_store().update_session(room, cluster_id="사라진-클러스터")
    conversations.registry.clear()

    r = client.post(f"/conversations/{room}/chat", json={"text": "안녕"}, headers=USER)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "CLUSTER_GONE"


# ── 임시 파일 ──────────────────────────────────────────────

def test_임시_kubeconfig는_주인만_읽고_블록을_벗어나면_사라진다():
    body = runtime.build(context_name="c", api_server="https://x", ca_data=CA,
                         namespace="web", credential={"token": "t"})
    with runtime.temporary_kubeconfig(body) as path:
        assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["current-context"] == "c"
    assert not path.exists()


# ── 자동 리뷰 반영 ─────────────────────────────────────────

def test_남의_클러스터를_방에_붙일_수_없다(client):
    """P1: 이 검사가 없으면 남의 클러스터 id 로 방을 만들어 그 클러스터를 바꿀 수 있다."""
    남의것 = get_store().create_cluster(
        registered_by="다른사람", name="피해자", api_server="https://victim.example",
        ca_data=CA, insecure=False, credential_encrypted=crypto.encrypt({"token": "비밀"}),
        context_name="victim", default_namespace="default", fingerprint="fp-victim",
    ).id
    r = client.post("/conversations", json={"cluster_id": 남의것, "shared": True}, headers=USER)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"


def test_방의_팀은_클러스터가_정한다(client):
    """🔴 화면이 보낸 team_id 를 그대로 믿으면 "승인 권한은 A팀, 실제 대상은 B팀 클러스터" 인 방이 생긴다."""
    cluster = get_store().create_cluster(
        registered_by="u-1", team_id="t-진짜", name="c", api_server="https://k.example",
        ca_data=CA, insecure=False, credential_encrypted=crypto.encrypt({"token": "t"}),
        context_name="ctx", default_namespace="default", fingerprint="fp",
    ).id
    created = client.post("/conversations", json={"cluster_id": cluster}, headers=USER).json()
    assert created["conversation"]["team_id"] == "t-진짜"


def test_클러스터의_팀과_다른_team_id_는_거절한다(client):
    """조용히 덮으면 요청보다 더 열린 방이 된다 — 개인 클러스터면 team_id 가 사라진다 (자동 리뷰)."""
    personal = get_store().create_cluster(
        registered_by="u-1", name="개인", api_server="https://k.example",
        ca_data=CA, insecure=False, credential_encrypted=crypto.encrypt({"token": "t"}),
        context_name="ctx", default_namespace="default", fingerprint="fp2",
    ).id
    r = client.post("/conversations", json={"cluster_id": personal, "team_id": "t-1"}, headers=USER)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "CLUSTER_TEAM_MISMATCH"


def test_CA_와_insecure_를_함께_쓰면_거부한다():
    """kubectl 이 그런 kubeconfig 를 거부한다 — 등록만 되고 실행이 실패하면 원인을 알기 어렵다."""
    body = yaml.safe_load(kubeconfig())
    body["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    with pytest.raises(KubeconfigRejected, match="함께 쓸 수 없습니다"):
        parse_kubeconfig(yaml.safe_dump(body))


def test_사설_주소로_풀리는_이름도_거부한다(monkeypatch):
    """P1: 이름만 보면 127.0.0.1 로 풀리는 이름을 등록해 서버가 대신 접속하게 만들 수 있다."""
    from kukie.clusters import kubeconfig as module

    monkeypatch.setattr(module, "_resolved", lambda host: ["127.0.0.1"])
    with pytest.raises(KubeconfigRejected, match="풀리는 이름"):
        parse_kubeconfig(kubeconfig(server_url="https://sneaky.example"))


def test_같은_이름을_두_번_등록하면_409(client):
    """team_id 가 NULL 이면 예전 UNIQUE 는 아무것도 막지 못했다.

    제약을 실제로 걸고 나니 이번엔 500 이 나갔다 — 사용자 실수는 500 이 아니라 안내여야 한다.
    """
    _register(client, name="운영")
    r = client.post("/clusters", json={"kubeconfig": kubeconfig(), "name": "운영"}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CLUSTER_NAME_TAKEN"


def test_이름_변경도_충돌하면_409(client):
    first = _register(client, name="운영")
    _register(client, name="스테이징", server_kubeconfig=kubeconfig(server_url="https://b.example"))
    r = client.patch(f"/clusters/{first['id']}", json={"name": "스테이징"}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CLUSTER_NAME_TAKEN"


def test_자격증명을_바꿔도_context_이름은_그대로다(client):
    """방마다 복사해 둔 context 이름이 바뀌면 기존 방이 전부 끊긴다."""
    registered = _register(client)
    다른이름 = kubeconfig(context="renamed", user={"token": "새-토큰"})
    r = client.patch(f"/clusters/{registered['id']}", json={"kubeconfig": 다른이름}, headers=USER)
    assert r.status_code == 200 and r.json()["context"] == "prod"


def test_클러스터의_접속_대상이_바뀌면_기존_방은_거절한다(client):
    """지문을 방에 복사해 두는 이유 — 승인한 대상과 실행 대상이 같은지 실행 직전에 확인한다."""
    registered = _register(client)
    room = client.post("/conversations", json={"cluster_id": registered["id"]},
                       headers=USER).json()["conversation"]["id"]
    get_store().update_cluster(registered["id"], fingerprint="다른-지문")

    r = client.post(f"/conversations/{room}/chat", json={"text": "안녕"}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CLUSTER_CHANGED"
