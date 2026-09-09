"""이 사람이 그 팀에서 무엇인가 — Spring 회원 서버에 물어본다.

agent 는 팀을 저장하지 않는다. 팀의 원본은 Spring 하나이므로 (기획 02), 판단이 필요할 때마다
**사용자 자신의 토큰으로** `GET /teams` 를 불러 역할을 받는다. Spring 에 새 API 를 만들 필요가 없다.

fail-closed 다. 회원 서버에 닿지 못하면 "권한 없음" 이 아니라 503 으로 거절한다 — 못 물어봤는데
통과시키면 검사가 없는 것과 같다 (auth.py 와 같은 원칙).

개발 모드(KUKIE_MEMBER_URL 없음)에는 팀 서버가 없다. 그때는 available() 이 False 이고,
부르는 쪽이 예전의 임시 규칙(만든 사람만)으로 돌아간다.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import HTTPException

from kukie.auth import MEMBER_TIMEOUT, User, _member_url

#: 같은 요청 안에서도 여러 번 묻게 되므로 잠깐 기억한다. 역할 변경이 늦게 반영되는 대신 왕복이 준다.
CACHE_TTL = 15.0
CACHE_MAX = 200

_cache: dict[str, tuple[float, dict[str, str]]] = {}


def available() -> bool:
    """팀 판단이 가능한가. 개발 모드면 False — 부르는 쪽이 임시 규칙으로 돌아간다."""
    return _member_url() is not None


def clear_cache() -> None:
    _cache.clear()


def _cached(key: str) -> dict[str, str] | None:
    found = _cache.get(key)
    if found is None:
        return None
    expires, roles = found
    if expires < time.monotonic():
        _cache.pop(key, None)
        return None
    return roles


def _remember(key: str, roles: dict[str, str]) -> None:
    if len(_cache) >= CACHE_MAX:
        _cache.clear()          # 로컬 단일 사용자 MVP — 오래된 것만 골라내는 대신 통째로 비운다
    _cache[key] = (time.monotonic() + CACHE_TTL, roles)


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(503, {"code": "MEMBER_UNAVAILABLE", "message": detail})


async def team_roles(user: User) -> dict[str, str]:
    """{팀 id: 역할}. 역할은 Spring 그대로 대문자 (ADMIN / MEMBER)."""
    member_url = _member_url()
    if member_url is None:
        return {}
    if not user.token:
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "토큰이 없어 팀을 확인할 수 없다"})

    key = user.token
    hit = _cached(key)
    if hit is not None:
        return hit

    try:
        async with httpx.AsyncClient(timeout=MEMBER_TIMEOUT) as http:
            response = await http.get(
                f"{member_url}/teams", headers={"Authorization": f"Bearer {user.token}"}
            )
    except httpx.HTTPError as exc:
        raise _unavailable(f"회원 서버에 연결할 수 없다: {exc}") from exc
    if response.status_code in (401, 403):
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "토큰이 유효하지 않다"})
    if response.status_code != 200:
        raise _unavailable(f"회원 서버 응답 {response.status_code}")

    body: Any = response.json()
    if not isinstance(body, list):
        raise _unavailable("회원 서버의 팀 목록 형식이 올바르지 않다")
    roles = {
        str(item["id"]): str(item.get("role", "MEMBER")).upper()
        for item in body
        if isinstance(item, dict) and item.get("id") is not None
    }
    _remember(key, roles)
    return roles


async def role_in(user: User, team_id: str) -> str | None:
    """그 팀에서의 역할. 속하지 않으면 None."""
    return (await team_roles(user)).get(team_id)


async def is_member(user: User, team_id: str) -> bool:
    return await role_in(user, team_id) is not None


async def is_admin(user: User, team_id: str) -> bool:
    return await role_in(user, team_id) == "ADMIN"


async def require_member(user: User, team_id: str) -> str:
    role = await role_in(user, team_id)
    if role is None:
        raise HTTPException(403, {"code": "NOT_TEAM_MEMBER", "message": "이 팀의 구성원이 아니다"})
    return role


async def require_admin(user: User, team_id: str) -> None:
    if await require_member(user, team_id) != "ADMIN":
        raise HTTPException(403, {"code": "NOT_TEAM_ADMIN", "message": "이 팀의 Admin 만 할 수 있다"})
