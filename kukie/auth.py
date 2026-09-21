"""요청이 누구 것인지 — Spring 회원 서버(kukie-server) 에 토큰을 확인한다.

토큰은 두 길로 온다. 앱(Electron)은 `Authorization: Bearer` 헤더로 (api-spec 5절), 웹 브라우저는 Spring 이 구워 준
httpOnly 쿠키 `kukie_access` 로 (kukie-server #26 — 브라우저는 헤더를 못 붙이고 쿠키만 자동으로 붙인다).
헤더가 있으면 헤더만(모양이 틀리면 401, 값이 비면 없는 것), 없을 때만 쿠키. 쿠키로만 인증된 요청은 브라우저가
`Sec-Fetch-Site` 로 same-origin(우리 페이지) 또는 none(주소창 직접 입력)이라고 알려 줄 때만 받는다 — 없거나
cross-site/same-site 면 403 CROSS_SITE_COOKIE. 브라우저는 이 헤더를 **HTTPS(또는 localhost)에서만** 붙이므로
웹은 HTTPS 로 배포한다는 전제다 (쿠키도 Secure 라 평문 HTTP 에는 애초에 안 실린다).
agent 는 그 토큰으로 Spring `GET /users/me` 를 불러 회원 id 를 얻는다. 팀·권한 판단도 Spring 몫이라
같은 토큰으로 `GET /teams` 를 물어 역할을 받는다 (kukie/membership.py).

fail-closed: 설정이 빠지면 거부한다.
  - KUKIE_MEMBER_URL 이 있으면 토큰(헤더 또는 쿠키)만 받는다. 개발용 헤더·변수는 무시.
  - 없으면 KUKIE_DEV_AUTH=1 일 때만 개발 모드 — `X-User: <id>` 헤더나 KUKIE_DEV_USER 를 회원 id 로 쓴다.
  - 둘 다 없으면 503 AUTH_NOT_CONFIGURED. (변수 하나 빠뜨렸다고 무인증 서버가 되지 않게.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException, Request

MEMBER_TIMEOUT = 5.0
# Spring 이 굽는 access 쿠키 이름 (kukie-server `auth.cookie.access-name`). 바꾸면 양쪽을 같이 바꾼다.
DEFAULT_ACCESS_COOKIE = "kukie_access"


@dataclass(frozen=True)
class User:
    id: str
    email: str | None = None
    name: str | None = None
    # 이 요청의 Bearer 토큰. 팀 소속을 물을 때 같은 토큰을 그대로 쓴다 (kukie/membership.py).
    # 요청 처리 동안 메모리에만 있고 저장·기록되지 않는다. 개발 모드면 None.
    token: str | None = None


def _member_url() -> str | None:
    return os.environ.get("KUKIE_MEMBER_URL", "").rstrip("/") or None


def _dev_auth() -> bool:
    return os.environ.get("KUKIE_DEV_AUTH", "") == "1"


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


def _access_cookie_name() -> str:
    return os.environ.get("KUKIE_ACCESS_COOKIE", "").strip() or DEFAULT_ACCESS_COOKIE


def _cookie_token(request: Request) -> str | None:
    value = request.cookies.get(_access_cookie_name())
    return value.strip() if value and value.strip() else None


# 브라우저가 붙이는 Sec-Fetch-Site 중 "우리 페이지에서 시작됐다" 로 볼 수 있는 값. same-origin 은 우리 페이지가 부른 것
# (웹은 리버스 프록시로 agent 와 같은 오리진이다 — DURO-102 결정 2), none 은 사용자가 주소창에 직접 친 것.
# same-site(같은 도메인의 다른 서브도메인)·cross-site(남의 사이트 링크·폼·이미지 태그)는 받지 않는다 — 서브도메인 하나에
# XSS 가 있으면 그 경로로 들어오기 때문에, 한 오리진이면 same-origin 만으로 충분하다 (자동 리뷰 지적).
SAME_ORIGIN_VALUES = frozenset({"same-origin", "none"})


def _from_same_origin(request: Request) -> bool:
    """쿠키 인증은 브라우저가 출처를 알려 줄 때만 받는다. 헤더가 없으면 통과가 아니라 거부다 — 통과시키면 그 경우엔
    검사가 없는 것과 같다 (fail-closed). 브라우저는 HTTPS·localhost 에서만 이 헤더를 붙인다 (Fetch Metadata 스펙의
    potentially trustworthy 조건). 평문 HTTP 로 띄우면 웹 로그인이 전부 403 이 되는데, 그건 배포가 HTTPS 여야 한다는 뜻이다."""
    return request.headers.get("sec-fetch-site", "").strip().lower() in SAME_ORIGIN_VALUES


async def _lookup(token: str, member_url: str) -> User:
    try:
        async with httpx.AsyncClient(timeout=MEMBER_TIMEOUT) as http:
            response = await http.get(f"{member_url}/users/me", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(503, {"code": "MEMBER_UNAVAILABLE", "message": f"회원 서버에 연결할 수 없다: {exc}"}) from exc
    if response.status_code in (401, 403):
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "토큰이 유효하지 않다"})
    if response.status_code != 200:
        raise HTTPException(503, {"code": "MEMBER_UNAVAILABLE", "message": f"회원 서버 응답 {response.status_code}"})
    body = response.json()
    return User(id=str(body["id"]), email=body.get("email"), name=body.get("name"), token=token)


async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_user: str | None = Header(default=None),
) -> User:
    """FastAPI 의존성. 라우트 인자에 `user: User = Depends(current_user)`."""
    member_url = _member_url()
    if member_url:
        # 헤더가 **있으면** 헤더만 본다 — 모양이 틀려도 쿠키로 넘어가지 않는다. 헤더는 부르는 쪽이 이번 요청에
        # 일부러 붙인 값이라 그 뜻을 존중한다 (Basic 을 보낸 클라이언트가 조용히 남의 쿠키 세션으로 도는 일이 없게).
        # 값이 빈 헤더(`Authorization:`)는 존중할 뜻이 없으니 없는 것으로 친다. 헤더가 **없을 때만** 쿠키.
        # kukie-server AuthenticationInterceptor 와 같은 규칙.
        if authorization is not None and authorization.strip():
            token = _bearer(authorization)
            if token is None:
                raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "Authorization 헤더는 Bearer <토큰> 모양이어야 한다"})
            return await _lookup(token, member_url)

        token = _cookie_token(request)
        if token is None:
            raise HTTPException(401, {
                "code": "UNAUTHORIZED",
                "message": f"Authorization: Bearer <토큰> 헤더나 {_access_cookie_name()} 쿠키가 필요하다",
            })
        # 쿠키는 브라우저가 알아서 붙이므로 다른 사이트가 시킨 요청에도 실릴 수 있다 (SameSite=Lax 도 top-level GET 은
        # 통과시킨다). 쿠키로만 인증된 요청은 브라우저가 "같은 사이트에서 시작됐다" 고 알려 줄 때만 받는다.
        # 헤더 토큰은 이 검사를 안 거친다.
        if not _from_same_origin(request):
            raise HTTPException(403, {
                "code": "CROSS_SITE_COOKIE",
                "message": "다른 오리진에서 시작됐거나 출처(Sec-Fetch-Site)를 알 수 없는 요청은 쿠키로 인증하지 않는다 "
                           "(브라우저는 HTTPS·localhost 에서만 이 헤더를 보낸다)",
            })
        return await _lookup(token, member_url)

    if not _dev_auth():
        raise HTTPException(503, {
            "code": "AUTH_NOT_CONFIGURED",
            "message": "KUKIE_MEMBER_URL(회원 서버) 또는 KUKIE_DEV_AUTH=1(개발 모드) 중 하나가 필요하다",
        })
    if x_user and x_user.strip():
        return User(id=x_user.strip())
    dev_user = os.environ.get("KUKIE_DEV_USER")
    if dev_user:
        return User(id=dev_user)
    raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "개발 모드: X-User 헤더나 KUKIE_DEV_USER 가 필요하다"})
