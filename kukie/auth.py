"""요청이 누구 것인지 — Spring 회원 서버(kukie-server) 에 토큰을 확인한다.

앱은 Spring 에서 받은 JWT 를 모든 요청에 `Authorization: Bearer` 로 붙인다 (api-spec 5절).
agent 는 그 토큰으로 Spring `GET /users/me` 를 불러 회원 id 를 얻는다. 팀·권한 판단도 Spring 몫이라
같은 토큰으로 `GET /teams` 를 물어 역할을 받는다 (kukie/membership.py).

fail-closed: 설정이 빠지면 거부한다.
  - KUKIE_MEMBER_URL 이 있으면 Bearer 토큰만 받는다. 개발용 헤더·변수는 무시.
  - 없으면 KUKIE_DEV_AUTH=1 일 때만 개발 모드 — `X-User: <id>` 헤더나 KUKIE_DEV_USER 를 회원 id 로 쓴다.
  - 둘 다 없으면 503 AUTH_NOT_CONFIGURED. (변수 하나 빠뜨렸다고 무인증 서버가 되지 않게.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException

MEMBER_TIMEOUT = 5.0


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
    authorization: str | None = Header(default=None),
    x_user: str | None = Header(default=None),
) -> User:
    """FastAPI 의존성. 라우트 인자에 `user: User = Depends(current_user)`."""
    member_url = _member_url()
    if member_url:
        token = _bearer(authorization)
        if token is None:
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "Authorization: Bearer <토큰> 이 필요하다"})
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
