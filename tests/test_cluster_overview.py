"""클러스터 개요 GET /clusters/{id}/overview — issue #102.

파싱은 순수 함수(clusters/overview.py)라 JSON 픽스처(tests/fixtures/overview)로 바로 본다.
엔드포인트는 연결 확인(test_clusters.py)과 같이 가짜 server 와 가짜 kubectl 로 본다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import OTHER, USER
from kukie_agent import server
from kukie_agent.clusters import overview
from kukie_agent.kubectl import KubectlResult

FIXTURES = Path(__file__).parent / "fixtures" / "overview"
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


def built() -> dict:
    return overview.build(context="eks-shop", namespace="shop", now=NOW,
                          workloads=fixture("workloads"), pods=fixture("pods"), events=fixture("events"))


# ── 순수 함수 ──────────────────────────────────────────────

def test_응답_머리는_context_namespace_수집시각이다():
    body = built()
    assert (body["context"], body["namespace"], body["at"]) == ("eks-shop", "shop", "2026-10-19T12:00:00Z")
    assert set(body) == {"context", "namespace", "at", "workloads", "pods", "events"}


def test_워크로드_상태_판정():
    rows = {(r["kind"], r["name"]): r for r in built()["workloads"]}
    assert {key: (r["ready"], r["desired"], r["status"]) for key, r in rows.items()} == {
        ("Deployment", "web"): (3, 3, "Available"),
        ("Deployment", "api"): (2, 2, "Progressing"),      # 다 떠 있지만 새 버전으로 바꾸는 중 (updated 1 < 2)
        ("Deployment", "worker"): (0, 2, "Degraded"),      # 새 버전으로 다 바뀌었는데 안 뜬다
        ("Deployment", "batch"): (0, 1, "Degraded"),       # ProgressDeadlineExceeded
        ("StatefulSet", "db"): (2, 3, "Degraded"),         # 롤아웃이 없는데 하나가 안 뜬다
        ("StatefulSet", "cache"): (1, 2, "Progressing"),   # currentRevision ≠ updateRevision
        ("DaemonSet", "logs"): (2, 2, "Available"),
    }
    assert [r["kind"] for r in built()["workloads"]] == ["Deployment"] * 4 + ["StatefulSet"] * 2 + ["DaemonSet"]
    assert {r["name"]: r["age"] for r in built()["workloads"]} == {
        "web": "2d", "api": "3h", "worker": "25m", "batch": "30s", "db": "7d", "cache": "4h", "logs": "1d",
    }


def test_컨트롤러가_새_세대를_아직_못_봤으면_진행_중이다():
    item = {"kind": "Deployment", "metadata": {"name": "x", "generation": 2}, "spec": {"replicas": 1},
            "status": {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 0}}
    assert overview.workload_status(item) == (0, 1, "Progressing")


def test_replicas_를_생략하면_1이고_0이면_Available():
    # 막 만들어 아직 아무 파드도 새 버전이 아니다 (updated 0 < 1) — 진행 중
    assert overview.workload_status({"kind": "Deployment", "spec": {}, "status": {}}) == (0, 1, "Progressing")
    assert overview.workload_status({"kind": "Deployment", "spec": {"replicas": 0}, "status": {}}) == (0, 0, "Available")


def test_파드는_대기_사유가_고장이면_Failed로_센다():
    pods = {p["name"]: p for p in built()["pods"]}
    assert {name: p["phase"] for name, p in pods.items()} == {
        "api-1": "Failed",       # Running 이지만 CrashLoopBackOff
        "api-2": "Failed",       # Pending 이지만 ImagePullBackOff
        "job-1": "Succeeded",
        "web-1": "Running",
        "worker-1": "Pending",   # 그냥 스케줄 대기
    }
    assert pods["web-1"]["restarts"] == 3 and pods["api-1"]["restarts"] == 7
    assert pods["web-1"]["node"] == "ip-10-0-1-5" and pods["api-2"]["node"] == ""
    assert (pods["web-1"]["age"], pods["api-2"]["age"], pods["job-1"]["age"]) == ("3h", "45s", "4d")
    assert [p["name"] for p in built()["pods"]] == sorted(pods)


def test_init_컨테이너가_고장이어도_Failed_모르는_phase는_Unknown():
    assert overview.pod_phase({"phase": "Pending", "initContainerStatuses": [
        {"state": {"waiting": {"reason": "ErrImagePull"}}}]}) == "Failed"
    assert overview.pod_phase({"phase": "Weird"}) == "Unknown"
    assert overview.pod_phase({}) == "Unknown"


def test_이벤트는_최근_것부터_20개():
    events = built()["events"]
    assert len(events) == overview.MAX_EVENTS
    assert events[0] == {"type": "Warning", "reason": "BackOff", "object": "pod/api-1",
                         "message": "Back-off restarting failed container", "at": "2026-10-19T11:59:00Z"}
    # lastTimestamp 가 없는 새 API 모양은 eventTime 으로, 초 단위로 자른다
    assert events[1]["object"] == "deployment/api" and events[1]["at"] == "2026-10-19T11:58:30Z"
    assert events[2]["message"] == "pulled 24"                               # 끝 줄바꿈은 뗀다
    ats = [e["at"] for e in events]
    assert ats == sorted(ats, reverse=True)
    assert "pulled 6" not in {e["message"] for e in events}                  # 오래된 것부터 잘린다


def test_빈_목록과_깨진_출력():
    empty = overview.build(context="c", namespace="n", now=NOW,
                           workloads='{"items": []}', pods='{"kind": "List"}', events="")
    assert (empty["workloads"], empty["pods"], empty["events"]) == ([], [], [])
    with pytest.raises(ValueError):
        overview.build(context="c", namespace="n", now=NOW, workloads="not json", pods="{}", events="{}")


@pytest.mark.parametrize(("seconds", "expected"), [(0, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"),
                                                   (3600, "1h"), (86399, "23h"), (86400, "1d"), (-5, "0s")])
def test_age는_가장_큰_단위_하나(seconds, expected):
    created = datetime.fromtimestamp(NOW.timestamp() - seconds, tz=timezone.utc).isoformat()
    assert overview.age(created, NOW) == expected
    assert overview.age(None, NOW) == ""


# ── 엔드포인트 ─────────────────────────────────────────────

@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)


def _result(stdout: str = "", *, stderr: str = "", success: bool = True) -> KubectlResult:
    return KubectlResult(command="kubectl …", stdout=stdout, stderr=stderr, success=success,
                         exit_code=0 if success else 1)


def _by_resource(args: list[str]) -> KubectlResult:
    return _result(fixture({"deployments,statefulsets,daemonsets": "workloads", "pods": "pods",
                            "events": "events"}[args[1]]))


def test_인증_없으면_401(client, fake_server):
    assert client.get(f"/clusters/{fake_server.add_cluster()}/overview").status_code == 401


def test_볼_수_없는_클러스터는_404이고_kubectl은_돌지_않는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(owner="u-1")
    ran: list[object] = []
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", lambda *a, **k: ran.append(a) or _result("{}"))
    r = client.get(f"/clusters/{cluster}/overview", headers=OTHER)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"
    assert ran == []
    assert client.get("/clusters/없는-id/overview", headers=USER).status_code == 404


def test_팀_구성원은_개요를_읽고_namespace_범위로만_조회한다(client, fake_server, monkeypatch):
    fake_server.add_member("t-1", "u-2", "MEMBER")
    cluster = fake_server.add_cluster(owner="u-1", team_id="t-1", context="eks-shop", namespace="shop",
                                      credential={"token": "secret-token"})
    seen: list[dict] = []

    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        seen.append({"args": args, "context": context, "dry_run": dry_run, "kubeconfig": kubeconfig,
                     "body": kubeconfig.read_text() if kubeconfig else None})
        return _by_resource(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=OTHER)

    assert r.status_code == 200
    body = r.json()
    assert (body["context"], body["namespace"]) == ("eks-shop", "shop")
    assert len(body["workloads"]) == 7 and len(body["pods"]) == 5 and len(body["events"]) == 20
    assert datetime.fromisoformat(body["at"]).tzinfo is not None
    assert "secret-token" not in r.text
    assert [call["args"] for call in seen] == [
        ["get", "deployments,statefulsets,daemonsets", "-o", "json", "-n", "shop"],
        ["get", "pods", "-o", "json", "-n", "shop"],
        ["get", "events", "-o", "json", "-n", "shop"],
    ]
    # 클러스터 범위 조회는 하지 않는다 — EKS 고객 클러스터는 namespace Edit 만 준다
    assert not any(flag in call["args"] for call in seen for flag in ("-A", "--all-namespaces", "nodes"))
    assert {call["context"] for call in seen} == {"eks-shop"} and not any(call["dry_run"] for call in seen)
    # server 에서 받은 값으로 만든 임시 kubeconfig 하나로 세 번 돌리고, 끝나면 지운다
    assert all("secret-token" in call["body"] for call in seen)
    assert len({call["kubeconfig"] for call in seen}) == 1 and not seen[0]["kubeconfig"].exists()
    # 읽기 전용 — 상태를 남기지 않는다
    assert all(name != "update_cluster_status" for name, _ in fake_server.calls)


@pytest.mark.parametrize(("stderr", "code"), [
    ("error: You must be logged in to the server (Unauthorized)", "CLUSTER_AUTH_EXPIRED"),
    ('Error from server (Forbidden): pods is forbidden: User "x" cannot list resource "pods"', "CLUSTER_AUTH_EXPIRED"),
    ("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout", "CLUSTER_UNREACHABLE"),
])
def test_kubectl_실패는_연결_확인과_같은_기준으로_갈라_502(client, fake_server, monkeypatch, stderr, code):
    cluster = fake_server.add_cluster()
    ran: list[object] = []

    def fail(args, **kwargs):
        ran.append(args)
        return _result(stderr=stderr, success=False)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fail)
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == code and stderr in r.json()["detail"]["message"]
    assert len(ran) == 1                                                     # 첫 실패에서 멈춘다


def test_kubectl_출력이_JSON이_아니면_502_CLUSTER_BAD_RESPONSE(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster()
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", lambda *a, **k: _result("not json"))
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "CLUSTER_BAD_RESPONSE"


def test_개요도_스레드_상한을_쓴다(client, fake_server, monkeypatch):
    from kukie_agent import clusters_api

    cluster = fake_server.add_cluster()
    seen: list[object] = []
    real = clusters_api.to_thread.run_sync

    async def watched(fn, *args, **kwargs):
        seen.append((getattr(fn, "__name__", ""), kwargs.get("limiter")))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(clusters_api.to_thread, "run_sync", watched)
    monkeypatch.setattr(clusters_api, "run_kubectl", lambda args, **k: _by_resource(args))
    assert client.get(f"/clusters/{cluster}/overview", headers=USER).status_code == 200
    ours = [limiter for name, limiter in seen if name == "collect"]
    assert ours and all(limiter is not None for limiter in ours)
