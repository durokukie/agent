"""GET /health — 배포 확인용 (DURO-107). 인증도 회원 서버도 필요 없다."""
from __future__ import annotations

from fastapi.testclient import TestClient

from kukie.server import app


def test_health_is_open_without_auth():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_stays_open_in_member_server_mode(monkeypatch):
    """dev 전용 4개(/session 등)는 회원 서버 모드에서 404 로 닫히지만(#75), /health 는 그대로 열려 있어야
    프록시·컨테이너 헬스체크가 본다."""
    monkeypatch.setenv("KUKIE_MEMBER_URL", "http://member.test")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/session", json={}).status_code == 404


def test_flat_endpoints_are_closed_unless_dev_mode_is_opted_in(monkeypatch):
    """회원 서버 주소가 빠졌다고 flat 4개가 인증 없이 열리면(fail-open) 설정 한 줄 실수가 무인증 에이전트 실행이 된다 (PR #82 리뷰).
    /conversations 의 503 과 같은 방향 — 회원 서버도 KUKIE_DEV_AUTH=1 도 없으면 닫힌다."""
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.delenv("KUKIE_DEV_AUTH", raising=False)
    with TestClient(app) as client:
        for method, path in [("post", "/session"), ("post", "/chat"), ("post", "/approve"), ("post", "/resume"), ("get", "/session")]:
            assert client.request(method, path, json={}).status_code == 404, path
        assert client.get("/health").status_code == 200
