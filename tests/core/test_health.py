"""GET /health — 배포 확인용 (DURO-107). 인증도 Kukie API 서버도 필요 없다."""
from __future__ import annotations

from fastapi.testclient import TestClient

from kukie_agent.server import app


def test_health_is_open_without_auth():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_stays_open_in_server_mode(monkeypatch):
    """dev 전용 4개(/session 등)는 Kukie API 서버 모드에서 404 로 닫히지만(#75), /health 는 그대로 열려 있어야
    프록시·컨테이너 헬스체크가 본다. 내부 토큰이 없으면 기동 정리(interrupt-orphaned)는 부르지 않는다."""
    monkeypatch.setenv("KUKIE_SERVER_URL", "http://server.invalid")
    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")                     # 개발 스위치가 켜져 있어도 server 모드면 닫힌다
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/session", json={}).status_code == 404


def test_flat_endpoints_are_closed_unless_dev_mode_is_opted_in(monkeypatch):
    """server 주소가 빠졌다고 flat 4개가 인증 없이 열리면(fail-open) 설정 한 줄 실수가 무인증 에이전트 실행이 된다 (PR #82 리뷰).
    /conversations 의 503 과 같은 방향 — server 도 KUKIE_DEV_AUTH=1 도 없으면 닫힌다."""
    monkeypatch.delenv("KUKIE_SERVER_URL", raising=False)
    monkeypatch.delenv("KUKIE_DEV_AUTH", raising=False)
    with TestClient(app) as client:
        for method, path in [("post", "/session"), ("post", "/chat"), ("post", "/approve"), ("post", "/resume"), ("get", "/session")]:
            r = client.request(method, path, json={})
            assert r.status_code == 404, path
            assert "KUKIE_DEV_AUTH" in r.json()["detail"]["message"]   # 왜 닫혔는지 — server 모드 문구와 다르다
        assert client.get("/health").status_code == 200


def test_flat_endpoints_stay_open_in_dev_mode(monkeypatch, tmp_path):
    """개발 모드(server 없음)에서는 flat 4개가 그대로다 — 계획은 파일로만 남는다."""
    from kukie_agent import server

    monkeypatch.setenv("KUKIE_DEV_AUTH", "1")
    monkeypatch.setattr(server, "read_kubeconfig", lambda: ("kind-dev", "study"))
    monkeypatch.setattr(server, "_session", None)
    with TestClient(app) as client:
        assert client.post("/session").status_code == 200
    monkeypatch.setattr(server, "_session", None)


def test_startup_does_not_touch_a_database(monkeypatch, tmp_path):
    """agent 는 DB 를 갖지 않는다 (DURO-113) — DB 주소를 줘도 켜질 때 아무 파일도 만들지 않는다."""
    monkeypatch.setenv("KUKIE_DATABASE_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert list(tmp_path.iterdir()) == []
