"""요청이 누구 것인지 — Spring 회원 서버(kukie-server) 에 토큰을 확인한다.

앱은 Spring 에서 받은 JWT 를 모든 요청에 `Authorization: Bearer` 로 붙인다 (api-spec 5절).
agent 는 그 토큰으로 Spring `GET /users/me` 를 불러 회원 id 를 얻는다. 팀·권한 판단도 Spring 몫이다.

개발 중 Spring 이 없을 때:
  - `X-User: <아무 문자열>` 헤더를 회원 id 로 쓴다 (KUKIE_MEMBER_URL 이 비어 있을 때만)
  - 둘 다 없으면 KUKIE_DEV_USER 환경변수, 그것도 없으면 401
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


def _member_url() -> str | None:
    return os.environ.get("KUKIE_MEMBER_URL", "").rstrip("/") or None


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
    return User(id=str(body["id"]), email=body.get("email"), name=body.get("name"))


async def current_user(
    authorization: str | None = Header(default=None),
    x_user: str | None = Header(default=None),
) -> User:
    """FastAPI 의존성. 라우트 인자에 `user: User = Depends(current_user)`."""
    member_url = _member_url()
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if member_url:
            return await _lookup(token, member_url)
        # Spring 없이 토큰만 온 경우 — 개발 편의로 토큰 문자열 자체를 id 로 쓴다
        return User(id=token)
    if x_user and not member_url:
        return User(id=x_user.strip())
    dev_user = os.environ.get("KUKIE_DEV_USER")
    if dev_user:
        return User(id=dev_user)
    raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "Authorization: Bearer <토큰> 이 필요하다"})
