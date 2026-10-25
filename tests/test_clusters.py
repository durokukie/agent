"""클러스터 연결 확인과 실행용 임시 kubeconfig — issue #61, 기획 04 §8.

등록·kubeconfig 검사·자격증명 암호화는 Kukie API 서버(kukie-server)로 옮겼다 (DURO-113) — 그 거부 사례들은
server 의 KubeconfigParserTest 가 본다. agent 에는 연결 확인(kubectl 이 여기서 돈다)과 임시 kubeconfig 만 남았다.
"""
from __future__ import annotations

import base64
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from helpers import OTHER, USER
from kukie_agent import server
from kukie_agent.clusters import runtime
from kukie_agent.kubectl import KubectlResult

CA = base64.b64encode(b"fake-ca").decode()


@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)


def _ok(stdout="{}"):
    return KubectlResult(command="kubectl …", stdout=stdout, stderr="", success=True, exit_code=0)


def test_인증_없으면_401(client, fake_server):
    cluster = fake_server.add_cluster()
    assert client.post(f"/clusters/{cluster}/test").status_code == 401


def test_볼_수_없는_클러스터는_server가_404로_답하고_kubectl은_돌지_않는다(client, fake_server, monkeypatch):
    """권한 판단은 연결 정보 요청에서 server 가 한다 — 못 보는 클러스터는 있는지도 알려 주지 않는다."""
    cluster = fake_server.add_cluster(owner="u-1")
    ran: list[object] = []
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", lambda *a, **k: ran.append(a) or _ok())
    r = client.post(f"/clusters/{cluster}/test", headers=OTHER)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"
    assert ran == [] and fake_server.clusters[cluster]["status"] == "disconnected"


def test_팀_클러스터는_구성원도_연결_확인을_한다(client, fake_server, monkeypatch):
    """연결 확인의 부산물(status)이라 "쓰기는 Admin" 경계에서 빼 뒀다."""
    fake_server.add_member("t-1", "u-2", "MEMBER")
    cluster = fake_server.add_cluster(owner="u-1", team_id="t-1")
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", lambda *a, **k: _ok())
    assert client.post(f"/clusters/{cluster}/test", headers=OTHER).json()["status"] == "connected"


def test_연결_확인은_임시_kubeconfig로_실행하고_상태를_server에_남긴다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(context="kind-team", namespace="web", credential={"token": "secret-token"})
    seen: list[dict] = []

    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        seen.append({"args": args, "context": context, "kubeconfig": kubeconfig,
                     "body": kubeconfig.read_text() if kubeconfig else None})
        return _ok("{}" if args[0] == "version" else "yes")

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.post(f"/clusters/{cluster}/test", headers=USER)

    assert r.status_code == 200
    assert r.json() == {"status": "connected", "reachable": True, "detail": "{}", "can_change": True,
                        "has_gpu": None}                              # 노드 목록을 못 읽었다 ("yes" 는 JSON 이 아니다)
    assert "secret-token" not in r.text                               # 자격증명은 응답에 없다
    # 서버의 기본 kubeconfig 가 아니라 server 에서 받은 값으로 만든 임시 파일로 실행했다
    assert all(call["kubeconfig"] is not None and "secret-token" in call["body"] for call in seen)
    assert all(not call["kubeconfig"].exists() for call in seen)       # 끝나면 지운다
    assert {call["context"] for call in seen} == {"kind-team"}
    assert seen[1]["args"] == ["auth", "can-i", "update", "deployments", "-n", "web"]
    assert fake_server.clusters[cluster]["status"] == "connected"
    assert fake_server.clusters[cluster]["lastCheckedAt"] is not None


def test_연결_실패는_이유에_따라_상태가_갈린다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster()
    monkeypatch.setattr(
        "kukie_agent.clusters_api.run_kubectl",
        lambda *a, **k: KubectlResult(command="kubectl …", stdout="", stderr="error: Unauthorized",
                                      success=False, exit_code=1),
    )
    assert client.post(f"/clusters/{cluster}/test", headers=USER).json()["status"] == "auth_expired"
    assert fake_server.clusters[cluster]["status"] == "auth_expired"


def test_연결_확인도_공용_스레드풀을_다_물지_않는다(client, fake_server, monkeypatch):
    """probe 는 kubectl 두 번 × 30초라 오래 잡는다 — 리미터가 붙어 있어야 한다."""
    from kukie_agent import clusters_api

    cluster = fake_server.add_cluster()
    seen: list[object] = []
    real = clusters_api.to_thread.run_sync

    async def watched(fn, *args, **kwargs):
        # FastAPI 가 동기 의존성을 돌릴 때도 이 함수를 쓴다 — 우리 호출만 골라 본다
        seen.append((getattr(fn, "__name__", ""), kwargs.get("limiter")))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(clusters_api.to_thread, "run_sync", watched)
    monkeypatch.setattr(clusters_api, "run_kubectl", lambda *a, **k: _ok())
    client.post(f"/clusters/{cluster}/test", headers=USER)

    ours = [limiter for name, limiter in seen if name == "probe"]
    assert ours and all(limiter is not None for limiter in ours)


@pytest.mark.parametrize(("method", "path"), [
    ("post", "/clusters"), ("get", "/clusters"), ("get", "/clusters/c-1"),
    ("patch", "/clusters/c-1"), ("delete", "/clusters/c-1"),
])
def test_등록_목록_수정_삭제는_agent에_없다(client, method, path):
    """Kukie API 서버의 공개 API 다 — agent 에 남으면 DB 없는 옆문이 된다."""
    assert getattr(client, method)(path, headers=USER).status_code in (404, 405)


# ── 임시 파일 ──────────────────────────────────────────────

def test_임시_kubeconfig는_주인만_읽고_블록을_벗어나면_사라진다():
    body = runtime.build(context_name="c", api_server="https://x", ca_data=CA,
                         namespace="web", credential={"token": "t"})
    with runtime.temporary_kubeconfig(body) as path:
        assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["current-context"] == "c"
    assert not path.exists()


def test_인증서_자격증명과_insecure도_그대로_옮긴다():
    body = yaml.safe_load(runtime.build(
        context_name="c", api_server="https://x", ca_data=None, namespace="web",
        credential={"client_certificate_data": "CERT", "client_key_data": "KEY"}, insecure=True,
    ))
    assert body["users"][0]["user"] == {"client-certificate-data": "CERT", "client-key-data": "KEY"}
    assert body["clusters"][0]["cluster"] == {"server": "https://x", "insecure-skip-tls-verify": True}


# ── 강제 종료가 남긴 임시 파일 ─────────────────────────────
# temporary_kubeconfig 의 finally 는 SIGKILL 에 돌지 않는다. 자식 프로세스로 그 상황을 만든다.

posix_only = pytest.mark.skipif(os.name != "posix", reason="SIGKILL·pid 확인은 POSIX 에서만 한다")

KILLED_INSIDE_BLOCK = """
import os, signal, sys
from pathlib import Path
from kukie_agent.clusters import runtime
runtime.TEMP_DIR = Path(sys.argv[1])
with runtime.temporary_kubeconfig("users: [{user: {token: secret-token}}]\\n"):
    os.kill(os.getpid(), signal.SIGKILL)
"""

WAITING_INSIDE_BLOCK = """
import sys
from pathlib import Path
from kukie_agent.clusters import runtime
runtime.TEMP_DIR = Path(sys.argv[1])
with runtime.temporary_kubeconfig("users: [{user: {token: secret-token}}]\\n") as path:
    print(path, flush=True)
    sys.stdin.read()
"""


@posix_only
def test_실행_중_강제_종료로_남은_임시_kubeconfig는_다시_뜰_때_지운다(tmp_path, monkeypatch):
    """kubectl 이 도는(막힌) 중에 `docker compose restart`·재시작 정책으로 죽으면 평문 토큰이 든 파일이 남고,
    컨테이너의 /tmp 는 다시 떠도 그대로다 (DURO-113 e2e). 다음 기동(lifespan)이 지운다."""
    monkeypatch.setattr(runtime, "TEMP_DIR", tmp_path)
    child = subprocess.run([sys.executable, "-c", KILLED_INSIDE_BLOCK, str(tmp_path)],
                           capture_output=True, text=True, timeout=60)
    assert child.returncode == -signal.SIGKILL, child.stderr
    [left] = tmp_path.glob("kukie-kubeconfig-*.yaml")
    assert "secret-token" in left.read_text(encoding="utf-8")         # finally 가 못 돌아 남았다

    with TestClient(server.app):
        pass
    assert not left.exists()


@posix_only
def test_기동_정리는_살아_있는_다른_프로세스가_쓰는_중인_파일을_지우지_않는다(tmp_path, monkeypatch):
    """같은 컴에서 도는 다른 agent·테스트의 파일은 kubectl 이 읽는 중일 수 있다 — 이름의 pid 로 주인을 가린다."""
    monkeypatch.setattr(runtime, "TEMP_DIR", tmp_path)
    with subprocess.Popen([sys.executable, "-c", WAITING_INSIDE_BLOCK, str(tmp_path)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        path = Path(child.stdout.readline().strip())
        assert path.parent == tmp_path and path.name.startswith(f"kukie-kubeconfig-{child.pid}-")
        assert runtime.remove_orphaned() == 0
        assert path.exists()
        child.communicate("", timeout=60)                              # 블록을 끝낸다
    assert not path.exists()


def test_기동_정리는_이_프로세스_pid와_pid_없는_예전_이름도_지우고_남의_파일은_둔다(tmp_path, monkeypatch):
    """기동 때는 이 프로세스가 아직 아무 파일도 만들지 않았다 — 같은 pid 의 파일은 그 pid 를 받았던 이전 프로세스의 것이다
    (컨테이너의 uvicorn 은 다시 떠도 PID 1). pid 를 넣기 전 이름(kukie-kubeconfig-<임의>.yaml)은 주인을 알 수 없어 지운다."""
    monkeypatch.setattr(runtime, "TEMP_DIR", tmp_path)
    same_pid = tmp_path / f"kukie-kubeconfig-{os.getpid()}-ab_c1234.yaml"
    old_format = tmp_path / "kukie-kubeconfig-h77x7kuz.yaml"
    others = [tmp_path / "kubeconfig.yaml", tmp_path / "kukie-kubeconfig-notes.txt"]
    for path in [same_pid, old_format, *others]:
        path.write_text("token: t\n", encoding="utf-8")

    assert runtime.remove_orphaned() == 2
    assert sorted(tmp_path.iterdir()) == sorted(others)
