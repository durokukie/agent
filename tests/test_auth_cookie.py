"""웹 로그인 — 헤더가 없으면 쿠키에서 토큰을 읽는다 (#79).

브라우저는 `Authorization` 헤더를 못 붙이고 Spring 이 구워 준 httpOnly 쿠키 `kukie_access` 만 자동으로 붙인다.
라우트에 얽히지 않게 `current_user` 만 붙인 작은 앱으로 검사한다. 가짜 Spring 은 test_membership 과 같은 방식.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from kukie.auth import User, current_user

MEMBER_URL = "http://member.test"


@pytest.fixture
def spring(monkeypatch):
    """Spring 의 GET /users/me 를 가짜로. 어떤 Authorization 으로 왔는지 calls 에 남긴다."""
    monkeypatch.setenv("KUKIE_MEMBER_URL", MEMBER_URL)
    state: dict = {"calls": []}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            state["calls"].append({"url": url, "headers": headers or {}})
            token = (headers or {}).get("Authorization", "")
            if token == "Bearer tok-cookie":
                return httpx.Response(200, json={"id": "u-cookie", "email": "c@b.c"})
            if token == "Bearer tok-header":
                return httpx.Response(200, json={"id": "u-header", "email": "h@b.c"})
            return httpx.Response(401, json={"code": "UNAUTHORIZED"})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return state


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: User = Depends(current_user)):
        return {"id": user.id, "token": user.token}

    return TestClient(app)


def _with_cookie(client: TestClient, name: str, value: str, site: str | None = "same-origin") -> TestClient:
    """브라우저처럼 쿠키를 들고 다니고, 우리 페이지에서 부른 것처럼 Sec-Fetch-Site 를 붙인다
    (httpx 는 요청마다 넘기는 cookies= 를 버리는 중이라 클라이언트에 심는다)."""
    client.cookies.clear()
    client.cookies.set(name, value)
    client.headers.pop("Sec-Fetch-Site", None)
    if site is not None:
        client.headers["Sec-Fetch-Site"] = site
    return client


def test_쿠키만_보내도_인증된다(client, spring):
    r = _with_cookie(client, "kukie_access", "tok-cookie").get("/whoami")
    assert r.status_code == 200
    assert r.json() == {"id": "u-cookie", "token": "tok-cookie"}
    # Spring 쪽엔 계속 Bearer 로 간다 — 쿠키를 그대로 넘기지 않는다
    assert spring["calls"][-1]["headers"] == {"Authorization": "Bearer tok-cookie"}


def test_헤더와_쿠키가_둘_다_있으면_헤더를_쓴다(client, spring):
    r = _with_cookie(client, "kukie_access", "tok-cookie").get(
        "/whoami", headers={"Authorization": "Bearer tok-header"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == "u-header"
    assert spring["calls"][-1]["headers"]["Authorization"] == "Bearer tok-header"


def test_헤더가_있으면_모양이_틀려도_쿠키로_넘어가지_않는다(client, spring):
    """Basic 이나 빈 Bearer 를 보낸 클라이언트가 조용히 남의 쿠키 세션으로 도는 일이 없게 — 헤더가 있으면 헤더만."""
    c = _with_cookie(client, "kukie_access", "tok-cookie")
    assert c.get("/whoami", headers={"Authorization": "Basic abc"}).status_code == 401
    assert c.get("/whoami", headers={"Authorization": "Bearer "}).status_code == 401
    assert spring["calls"] == []


def test_다른_사이트에서_시작됐거나_출처를_모르는_요청은_쿠키로_인증하지_않는다(client, spring):
    """SameSite=Lax 도 top-level GET 은 통과시킨다. 브라우저가 붙이는 Sec-Fetch-Site 로 한 번 더 거른다 —
    헤더가 없어도 거부(fail-closed). 통과시키면 그 브라우저에서는 검사가 없는 것과 같다."""
    for site in ("cross-site", None):
        r = _with_cookie(client, "kukie_access", "tok-cookie", site=site).get("/whoami")
        assert r.status_code == 403 and r.json()["detail"]["code"] == "CROSS_SITE_COOKIE", site
    assert spring["calls"] == []
    # 같은 사이트 · 주소창 직접 입력은 통과
    for site in ("same-origin", "same-site", "none"):
        assert _with_cookie(client, "kukie_access", "tok-cookie", site=site).get("/whoami").status_code == 200


def test_헤더_토큰은_다른_사이트에서_와도_받는다(client, spring):
    """cross-site 검사는 쿠키에만. 헤더는 부르는 쪽이 일부러 붙인 값이라 CSRF 와 무관하다."""
    client.cookies.clear()
    r = client.get("/whoami", headers={"Authorization": "Bearer tok-header", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    assert client.get("/whoami", headers={"Authorization": "Bearer tok-header"}).status_code == 200


def test_값이_빈_Authorization_헤더는_없는_것으로_친다(client, spring):
    """`Authorization:` 만 붙인 클라이언트가 쿠키 로그인을 통째로 잃지 않게 — 빈 값엔 존중할 뜻이 없다."""
    c = _with_cookie(client, "kukie_access", "tok-cookie")
    assert c.get("/whoami", headers={"Authorization": ""}).json()["id"] == "u-cookie"
    assert c.get("/whoami", headers={"Authorization": "   "}).json()["id"] == "u-cookie"


def test_둘_다_없으면_401(client, spring):
    r = client.get("/whoami")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "UNAUTHORIZED"
    assert spring["calls"] == []


def test_빈_쿠키는_없는_것과_같다(client, spring):
    r = _with_cookie(client, "kukie_access", "  ").get("/whoami")
    assert r.status_code == 401
    assert spring["calls"] == []


def test_쿠키_이름은_환경변수로_바꿀_수_있다(client, spring, monkeypatch):
    """Spring 의 auth.cookie.access-name 을 바꾸면 여기도 같이 바꾼다 — 옛 이름은 더 이상 읽지 않는다."""
    monkeypatch.setenv("KUKIE_ACCESS_COOKIE", "other_access")
    assert _with_cookie(client, "kukie_access", "tok-cookie").get("/whoami").status_code == 401
    assert _with_cookie(client, "other_access", "tok-cookie").get("/whoami").status_code == 200


def test_개발_모드는_쿠키를_무시한다(client, monkeypatch):
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")
    monkeypatch.delenv("KUKIE_DEV_USER", raising=False)
    assert _with_cookie(client, "kukie_access", "tok-cookie").get("/whoami").status_code == 401
    assert client.get("/whoami", headers={"X-User": "dev-1"}).json()["id"] == "dev-1"
