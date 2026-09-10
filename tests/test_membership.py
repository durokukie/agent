"""팀 권한 — agent 가 Spring 에 물어 판단한다 (기획 02 §3, 04 §3).

Spring 에 새 API 를 만들지 않았다. 사용자 자신의 토큰으로 이미 있는 GET /teams 를 부른다.
회원 서버가 없는 개발 모드에서는 예전 임시 규칙(만든 사람만)으로 돌아간다.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from kukie import conversations, membership, server
from kukie.auth import User
from kukie.clusters import crypto
from kukie.store import get_store, reset_store_for_tests

MEMBER_URL = "http://member.test"


def _user(token: str = "tok-1", user_id: str = "u-1") -> User:
    return User(id=user_id, token=token)


@pytest.fixture(autouse=True)
def clean():
    membership.clear_cache()
    yield
    membership.clear_cache()


@pytest.fixture
def spring(monkeypatch):
    """Spring 의 GET /teams 를 가짜로. calls 로 몇 번 불렀는지 센다."""
    monkeypatch.setenv("KUKIE_MEMBER_URL", MEMBER_URL)
    state: dict = {"teams": [], "status": 200, "calls": [], "boom": None}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            state["calls"].append({"url": url, "headers": headers or {}})
            if state["boom"]:
                raise state["boom"]
            if url.endswith("/users/me"):
                return httpx.Response(200, json={"id": "u-1", "name": "나", "email": "a@b.c"})
            return httpx.Response(state["status"], json=state["teams"])

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return state


# ── 물어보기 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_사용자_토큰_그대로_스프링에_묻는다(spring):
    spring["teams"] = [{"id": "t-1", "name": "듀로", "role": "ADMIN"}]
    assert await membership.role_in(_user(), "t-1") == "ADMIN"
    call = spring["calls"][-1]
    assert call["url"] == f"{MEMBER_URL}/teams"
    assert call["headers"]["Authorization"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_속하지_않은_팀은_None(spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    assert await membership.role_in(_user(), "t-2") is None
    assert await membership.is_member(_user(), "t-2") is False


@pytest.mark.asyncio
async def test_잠깐_기억해_왕복을_줄인다(spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    for _ in range(3):
        await membership.role_in(_user(), "t-1")
    assert len(spring["calls"]) == 1


@pytest.mark.asyncio
async def test_토큰이_다르면_따로_묻는다(spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    await membership.role_in(_user("tok-1"), "t-1")
    await membership.role_in(_user("tok-2"), "t-1")
    assert len(spring["calls"]) == 2


@pytest.mark.asyncio
async def test_회원_서버에_닿지_못하면_통과시키지_않는다(spring):
    """못 물어봤는데 통과시키면 검사가 없는 것과 같다 — fail-closed."""
    spring["boom"] = httpx.ConnectError("연결 실패")
    with pytest.raises(HTTPException) as raised:
        await membership.require_admin(_user(), "t-1")
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "MEMBER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_토큰이_거부되면_401(spring):
    spring["status"] = 401
    with pytest.raises(HTTPException) as raised:
        await membership.team_roles(_user())
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_Member_는_Admin_이_아니다(spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    await membership.require_member(_user(), "t-1")          # 통과
    with pytest.raises(HTTPException) as raised:
        await membership.require_admin(_user(), "t-1")
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "NOT_TEAM_ADMIN"


def test_개발_모드에서는_팀_판단을_하지_않는다(monkeypatch):
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    assert membership.available() is False


# ── 클러스터에 적용 ────────────────────────────────────────

@pytest.fixture
def client(monkeypatch, tmp_path, spring):
    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())
    reset_store_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    conversations.registry.clear()
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    return TestClient(server.app)


def _cluster(team_id: str | None, *, owner: str = "u-1") -> str:
    return get_store().create_cluster(
        registered_by=owner, team_id=team_id, name="c", api_server="https://k.example",
        ca_data="QQ==", insecure=False, credential_encrypted="x", context_name="ctx",
        default_namespace="default", fingerprint="fp",
    ).id


BEARER = {"Authorization": "Bearer tok-1"}


def test_팀_클러스터는_구성원이_보고_Admin_만_지운다(client, spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    cluster = _cluster("t-1", owner="다른사람")

    assert client.get(f"/clusters/{cluster}", headers=BEARER).status_code == 200
    r = client.delete(f"/clusters/{cluster}", headers=BEARER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_TEAM_ADMIN"

    membership.clear_cache()
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    assert client.delete(f"/clusters/{cluster}", headers=BEARER).status_code == 200


def test_남의_팀_클러스터는_없는_것처럼_404(client, spring):
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-9", owner="다른사람")
    assert client.get(f"/clusters/{cluster}", headers=BEARER).status_code == 404


def test_목록에_남의_팀_것이_섞이지_않는다(client, spring):
    """예전에는 팀이 붙은 클러스터를 전부 보여 줘서 남의 팀 것까지 새어 나갔다."""
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    mine = _cluster("t-1", owner="다른사람")
    personal = _cluster(None)
    _cluster("t-9", owner="다른사람")

    ids = {c["id"] for c in client.get("/clusters", headers=BEARER).json()}
    assert ids == {mine, personal}


def test_팀_클러스터_등록은_Admin_만(client, spring):
    import yaml, base64
    body = yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "c", "cluster": {"server": "https://api.example",
                                               "certificate-authority-data": base64.b64encode(b"ca").decode()}}],
        "users": [{"name": "u", "user": {"token": "t"}}],
        "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "u"}}],
        "current-context": "ctx",
    })
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    r = client.post("/clusters", json={"kubeconfig": body, "name": "운영", "team_id": "t-1"}, headers=BEARER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_TEAM_ADMIN"

    membership.clear_cache()
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    assert client.post("/clusters", json={"kubeconfig": body, "name": "운영", "team_id": "t-1"},
                       headers=BEARER).status_code == 200


# ── 승인 권한 ──────────────────────────────────────────────

def test_팀_Admin_은_남의_shared_방도_승인할_수_있다(client, spring):
    """기획 06 §3 — Admin 이면 승인할 수 있다. 예전에는 만든 사람만이었다."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    room = get_store().create_session(
        user_id="다른사람", context_name="ctx", namespace="default", mode="실습",
        team_id="t-1", shared=True,
    ).id
    # 대기 중인 승인이 없으니 NOT_PENDING 이어야 한다 — 403(권한)에서 막히지 않았다는 뜻
    r = client.post(f"/conversations/{room}/approve", json={"call_id": "c1", "approved": True},
                    headers=BEARER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


def test_팀_Member_는_남의_shared_방을_승인하지_못한다(client, spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    room = get_store().create_session(
        user_id="다른사람", context_name="ctx", namespace="default", mode="실습",
        team_id="t-1", shared=True,
    ).id
    r = client.post(f"/conversations/{room}/approve", json={"call_id": "c1", "approved": True},
                    headers=BEARER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_TEAM_ADMIN"


def test_팀이_없는_방은_예전대로_만든_사람만(client, spring):
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    room = get_store().create_session(
        user_id="다른사람", context_name="ctx", namespace="default", mode="실습", shared=True,
    ).id
    r = client.post(f"/conversations/{room}/approve", json={"call_id": "c1", "approved": True},
                    headers=BEARER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"


# ── 자동 리뷰 반영 ─────────────────────────────────────────

def test_팀에서_나가면_등록자여도_안_보인다(client, spring):
    """P1: 등록자라는 이유로 통과시키면 팀에서 쫓겨난 뒤에도 계속 만질 수 있다."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")          # 내가 등록한 팀 클러스터
    assert client.get(f"/clusters/{cluster}", headers=BEARER).status_code == 200

    membership.clear_cache()
    spring["teams"] = []                            # 팀에서 나갔다
    assert client.get(f"/clusters/{cluster}", headers=BEARER).status_code == 404
    assert client.get("/clusters", headers=BEARER).json() == []


def test_회원_서버가_죽으면_팀_클러스터에_닿지_못한다(client, spring):
    """등록자 우회가 있으면 서버가 죽었을 때도 통과했다 — fail-closed 가 깨진다."""
    import httpx

    _cluster("t-1", owner="u-1")
    spring["boom"] = httpx.ConnectError("연결 실패")
    assert client.get("/clusters", headers=BEARER).status_code == 503


def test_개인_클러스터는_회원_서버와_무관하게_보인다(client, spring):
    """팀이 없는 클러스터까지 막으면 혼자 쓰는 사람이 못 쓴다."""
    import httpx

    personal = _cluster(None, owner="u-1")
    spring["teams"] = []
    assert client.get(f"/clusters/{personal}", headers=BEARER).status_code == 200


@pytest.mark.asyncio
async def test_만료된_기억은_스스로_사라진다(spring, monkeypatch):
    """다시 오지 않는 토큰이 남아 있으면 "요청 처리 동안만" 보다 실제 수명이 길어진다."""
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    monkeypatch.setattr(membership, "CACHE_TTL", 0.0)
    await membership.team_roles(_user("tok-옛날"))
    await membership.team_roles(_user("tok-새것"))
    assert "tok-옛날" not in membership._cache


@pytest.mark.asyncio
async def test_200_인데_JSON_이_아니면_503(spring, monkeypatch):
    """500 으로 나가면 앱이 회원 서버 문제와 agent 버그를 구분하지 못한다."""
    import httpx

    def bad_json(self):
        raise ValueError("JSON 아님")

    monkeypatch.setattr(httpx.Response, "json", bad_json)
    with pytest.raises(HTTPException) as raised:
        await membership.team_roles(_user())
    assert raised.value.status_code == 503


def test_남의_팀_shared_방에는_들어갈_수_없다(client, spring):
    """🔴 소유권 검사를 방 만들 때만 걸면, shared 방으로 들어가 그 클러스터에 kubectl 을 돌릴 수 있다."""
    spring["teams"] = [{"id": "t-내팀", "role": "ADMIN"}]
    room = get_store().create_session(
        user_id="다른사람", context_name="ctx", namespace="default", mode="학습",
        team_id="t-남의팀", shared=True,
    ).id
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 404
    assert client.post(f"/conversations/{room}/chat", json={"text": "안녕"},
                       headers=BEARER).status_code == 404


def test_같은_팀_shared_방에는_들어간다(client, spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    room = get_store().create_session(
        user_id="다른사람", context_name="ctx", namespace="default", mode="학습",
        team_id="t-1", shared=True,
    ).id
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 200


def test_팀에서_나가면_그_팀_클러스터로_방을_못_만든다(client, spring):
    """🔴 방을 만드는 쪽이 registered_by 만 보면, /clusters 에서 막은 것이 방 경로로 샌다."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")          # 내가 등록한 팀 클러스터
    assert client.post("/conversations", json={"cluster_id": cluster},
                       headers=BEARER).status_code == 200

    membership.clear_cache()
    spring["teams"] = []                            # 팀에서 나갔다
    r = client.post("/conversations", json={"cluster_id": cluster}, headers=BEARER)
    assert r.status_code == 404


def test_같은_팀이면_남이_등록한_클러스터로도_방을_만든다(client, spring):
    """반대 방향 — 규칙이 어긋나면 팀원인데 방을 못 만든다."""
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    cluster = _cluster("t-1", owner="다른사람")
    assert client.post("/conversations", json={"cluster_id": cluster},
                       headers=BEARER).status_code == 200


def test_나가기_전에_만든_방도_막힌다(client, spring):
    """🔴 주인을 먼저 통과시키면 팀에서 나가기 전에 만든 방으로 계속 쓸 수 있다.

    새 방만 확인하던 이전 테스트가 이걸 놓쳤다.
    """
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")
    room = client.post("/conversations", json={"cluster_id": cluster, "shared": True},
                       headers=BEARER).json()["conversation"]["id"]
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 200

    membership.clear_cache()
    spring["teams"] = []                    # 팀에서 나갔다 — 방은 그대로 남아 있다
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 404
    assert client.post(f"/conversations/{room}/chat", json={"text": "늘려줘"},
                       headers=BEARER).status_code == 404
    assert client.post(f"/conversations/{room}/approve", json={"call_id": "c1", "approved": True},
                       headers=BEARER).status_code == 404


def test_회원_서버가_죽으면_옛_방도_못_쓴다(client, spring):
    """이 경로가 membership.available() 조차 안 보면 fail-closed 가 여기서만 깨진다."""
    import httpx

    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")
    room = client.post("/conversations", json={"cluster_id": cluster, "shared": True},
                       headers=BEARER).json()["conversation"]["id"]

    membership.clear_cache()
    spring["boom"] = httpx.ConnectError("연결 실패")
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 503


def test_팀원이면_자기가_만든_방은_승인할_수_있다(client, spring):
    """기획 06 §3 — 요청한 사용자 본인도 자신의 Plan 을 승인할 수 있다."""
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    room = get_store().create_session(
        user_id="u-1", context_name="ctx", namespace="default", mode="실습",
        team_id="t-1", shared=True,
    ).id
    # 권한에서 막히면 403, 통과하면 대기 카드가 없어 409 NOT_PENDING
    r = client.post(f"/conversations/{room}/approve", json={"call_id": "c1", "approved": True},
                    headers=BEARER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_PENDING"


def test_클러스터_없이_남의_팀에_방을_심을_수_없다(client, spring):
    """🔴 team_id 가 방을 여는 열쇠가 됐는데 클러스터 없는 경로는 그 값을 검사하지 않았다.

    외부인이 심은 방에 그 팀 구성원이 들어오고, 방의 context·namespace 는 외부인이 정한 값이다.
    """
    spring["teams"] = [{"id": "t-내팀", "role": "ADMIN"}]
    r = client.post("/conversations", json={"team_id": "t-남의팀", "shared": True}, headers=BEARER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_TEAM_MEMBER"


def test_없는_팀을_적으면_방이_만들어지지_않는다(client, spring):
    """만들어지고 나면 만든 사람도 못 들어가는데 지울 방법이 없다."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    r = client.post("/conversations", json={"team_id": "t-없음", "shared": True}, headers=BEARER)
    assert r.status_code == 403

    ids = [c["id"] for c in client.get("/conversations", headers=BEARER).json()]
    assert ids == []                      # 죽은 방이 목록에 남지 않는다


def test_내_팀이면_클러스터_없이도_방을_만든다(client, spring):
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    created = client.post("/conversations", json={"team_id": "t-1", "shared": True},
                          headers=BEARER)
    assert created.status_code == 200
    room = created.json()["conversation"]["id"]
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 200


def test_팀에서_나가도_내_private_방은_읽을_수_있다(client, spring):
    """자기 대화 기록인데 읽지도 못하면 되돌릴 방법이 없다 — 대화 삭제 API 도 없다 (자동 리뷰)."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")
    room = client.post("/conversations", json={"cluster_id": cluster}, headers=BEARER
                       ).json()["conversation"]["id"]

    membership.clear_cache()
    spring["teams"] = []
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 200


def test_팀에서_나가면_그_클러스터로_실행은_못_한다(client, spring):
    """읽기는 열고 실행은 닫는다."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")
    room = client.post("/conversations", json={"cluster_id": cluster}, headers=BEARER
                       ).json()["conversation"]["id"]

    membership.clear_cache()
    spring["teams"] = []
    r = client.post(f"/conversations/{room}/chat", json={"text": "파드 보여줘"}, headers=BEARER)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_TEAM_MEMBER"


def test_목록과_상세의_답이_같다(client, spring):
    """목록에 있는데 열 수 없는 방이 있으면 사용자가 막다른 길에 선다."""
    spring["teams"] = [{"id": "t-1", "role": "MEMBER"}]
    보이는방 = get_store().create_session(
        user_id="다른사람", context_name="c", namespace="n", mode="학습", team_id="t-1", shared=True
    ).id
    숨는방 = get_store().create_session(
        user_id="다른사람", context_name="c", namespace="n", mode="학습", team_id="t-9", shared=True
    ).id

    ids = {c["id"] for c in client.get("/conversations", headers=BEARER).json()}
    assert 보이는방 in ids and 숨는방 not in ids
    assert client.get(f"/conversations/{보이는방}", headers=BEARER).status_code == 200
    assert client.get(f"/conversations/{숨는방}", headers=BEARER).status_code == 404


def test_내가_만든_팀_shared_방도_목록과_상세가_같다(client, spring):
    """주인을 목록에서 예외로 두면 _load 와 갈려 "목록에 있는데 열 수 없는 방" 이 남는다."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")
    room = client.post("/conversations", json={"cluster_id": cluster, "shared": True},
                       headers=BEARER).json()["conversation"]["id"]

    membership.clear_cache()
    spring["teams"] = []
    ids = {c["id"] for c in client.get("/conversations", headers=BEARER).json()}
    assert room not in ids
    assert client.get(f"/conversations/{room}", headers=BEARER).status_code == 404


def test_권한_거절이_run_을_남기지_않는다(client, spring):
    """뒤에서 던지면 run 이 running 인 채 남고, 같은 request_id 재시도가 409 INTERRUPTED 로 샌다."""
    spring["teams"] = [{"id": "t-1", "role": "ADMIN"}]
    cluster = _cluster("t-1", owner="u-1")
    room = client.post("/conversations", json={"cluster_id": cluster}, headers=BEARER
                       ).json()["conversation"]["id"]

    membership.clear_cache()
    spring["teams"] = []
    body = {"text": "파드 보여줘", "request_id": "req-1"}
    first = client.post(f"/conversations/{room}/chat", json=body, headers=BEARER)
    assert first.status_code == 403

    assert get_store().list_runs(room) == []          # run 이 안 남는다
    again = client.post(f"/conversations/{room}/chat", json=body, headers=BEARER)
    assert again.status_code == 403                   # 재시도도 같은 안내


@pytest.mark.asyncio
async def test_잠금_검사와_획득_사이에_await_가_없다():
    """🔴 그 창에서 루프를 놓으면 두 요청이 나란히 통과해 같은 방에 run 이 둘 생긴다.

    소스에서 직접 확인한다 — 동시성 사고는 테스트로 재현하기 어렵고, 규칙은 "그 사이에 await 를
    두지 않는다" 하나다 (체크리스트 "잠금 틈").
    """
    import inspect
    import re

    from kukie import conversations_api

    source = inspect.getsource(conversations_api)
    for block in re.findall(
        r"if conversation\.lock\.locked\(\):(.*?)async with conversation\.lock:", source, re.S
    ):
        assert "await " not in block, f"잠금 검사와 획득 사이에 await 가 있다:\n{block}"
