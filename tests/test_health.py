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
