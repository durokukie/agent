"""클러스터 개요 GET /clusters/{id}/overview — issue #102.

파싱은 순수 함수(clusters/overview.py)라 JSON 픽스처(tests/fixtures/overview)로 바로 본다.
엔드포인트는 연결 확인(test_clusters.py)과 같이 가짜 server 와 가짜 kubectl 로 본다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import OTHER, USER
from kukie_agent import clusters_api, server
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
    assert set(body) == {"context", "namespace", "at", "workloads", "pods", "events", "network", "gpu"}
    assert body["network"] is None                                           # CNI 조회 출력을 안 넘기면 null
    assert body["gpu"] is None                                               # GPU 칸을 안 넘기면 null (clusters/gpu.py)


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


def test_막_만든_Deployment는_파드가_다_생겼어도_뜨는_동안_진행_중이다():
    """updatedReplicas 는 Ready 와 무관하게 파드가 생기면 오른다 — Progressing 조건의 사유로 가른다 (PR #103 리뷰)."""
    item = {"kind": "Deployment", "metadata": {"name": "x", "generation": 1}, "spec": {"replicas": 2},
            "status": {"observedGeneration": 1, "updatedReplicas": 2, "readyReplicas": 0, "conditions": [
                {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"},
                {"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated"}]}}
    assert overview.workload_status(item) == (0, 2, "Progressing")


@pytest.mark.parametrize(("strategy", "status", "expected"), [
    # partition 2 — 순번 0·1 은 옛 리비전에 두는 게 정상이다
    ({"type": "RollingUpdate", "rollingUpdate": {"partition": 2}},
     {"updatedReplicas": 1, "readyReplicas": 3, "currentRevision": "a", "updateRevision": "b"}, "Available"),
    ({"type": "RollingUpdate", "rollingUpdate": {"partition": 2}},
     {"updatedReplicas": 0, "readyReplicas": 3, "currentRevision": "a", "updateRevision": "b"}, "Progressing"),
    # OnDelete — 사람이 지울 때까지 옛 리비전이 정상이다
    ({"type": "OnDelete"},
     {"updatedReplicas": 0, "readyReplicas": 3, "currentRevision": "a", "updateRevision": "b"}, "Available"),
    ({"type": "OnDelete"},
     {"updatedReplicas": 0, "readyReplicas": 2, "currentRevision": "a", "updateRevision": "b"}, "Degraded"),
])
def test_StatefulSet_partition_과_OnDelete는_옛_리비전이_남아도_정상이다(strategy, status, expected):
    item = {"kind": "StatefulSet", "metadata": {"name": "db", "generation": 1},
            "spec": {"replicas": 3, "updateStrategy": strategy}, "status": {"observedGeneration": 1, **status}}
    assert overview.workload_status(item)[2] == expected


def test_DaemonSet_OnDelete도_옛_리비전이_남아도_정상이다():
    item = {"kind": "DaemonSet", "metadata": {"name": "cni", "generation": 2},
            "spec": {"updateStrategy": {"type": "OnDelete"}},
            "status": {"observedGeneration": 2, "desiredNumberScheduled": 3, "numberReady": 3, "updatedNumberScheduled": 0}}
    assert overview.workload_status(item) == (3, 3, "Available")


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


def test_restarts_는_init_컨테이너도_센다():
    [pod] = overview.parse_pods([{"metadata": {"name": "p"}, "status": {
        "phase": "Pending",
        "initContainerStatuses": [{"restartCount": 4, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}],
        "containerStatuses": [{"restartCount": 1, "state": {"waiting": {"reason": "PodInitializing"}}}]}}], NOW)
    assert (pod["phase"], pod["restarts"]) == ("Failed", 5)


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


# ── 네트워크(CNI) ──────────────────────────────────────────

def _daemonsets(*names_and_images: tuple[str, str]) -> str:
    return json.dumps({"kind": "List", "items": [
        {"kind": "DaemonSet", "metadata": {"name": name, "namespace": "kube-system"},
         "spec": {"template": {"spec": {"containers": [{"name": "main", "image": image}]}}}}
        for name, image in names_and_images]})


def test_Helm_으로_깐_Cilium을_kube_system_DaemonSet으로_알아본다():
    assert overview.parse_network(fixture("daemonsets")) == {"name": "Cilium", "version": "1.20.3", "source": "daemonset"}


@pytest.mark.parametrize(("daemonset", "name"), [
    ("cilium", "Cilium"), ("calico-node", "Calico"), ("aws-node", "Amazon VPC CNI"),
    ("kube-flannel-ds", "Flannel"), ("kube-flannel", "Flannel"), ("weave-net", "Weave"), ("antrea-agent", "Antrea"),
])
def test_CNI_DaemonSet_이름표(daemonset, name):
    found = overview.parse_network(_daemonsets(("kube-proxy", "k/kube-proxy:v1.34.0"), (daemonset, "r/x:v1.2.3")))
    assert found == {"name": name, "version": "1.2.3", "source": "daemonset"}


def test_Cilium과_aws_node가_같이_있으면_Cilium이다():
    """Cilium 을 vpc-cni 위에 얹어(chaining) 돌리면 aws-node 도 남는다 — 모듈 설명 "네트워크"."""
    found = overview.parse_network(_daemonsets(("aws-node", "r/amazon-k8s-cni:v1.20.4-eksbuild.1"),
                                               ("cilium", "quay.io/cilium/cilium:v1.20.3")))
    assert found is not None and (found["name"], found["version"]) == ("Cilium", "1.20.3")
    found = overview.parse_network(_daemonsets(("aws-node", "r/amazon-k8s-cni:v1.20.4-eksbuild.1"),
                                               ("calico-node", "docker.io/calico/node:v3.30.0")))
    assert found is not None and found["name"] == "Calico"


def test_CNI_DaemonSet이_없거나_출력을_못_읽으면_None():
    assert overview.parse_network(_daemonsets(("kube-proxy", "k/kube-proxy:v1.34.0"))) is None
    assert overview.parse_network('{"items": []}') is None
    assert overview.parse_network(None) is None
    assert overview.parse_network("not json") is None                        # 다른 칸과 달리 502 로 올리지 않는다
    assert overview.parse_network('{"items": {}}') is None
    # metadata·spec 의 모양이 이상해도 개요 전체를 500 으로 만들지 않는다 (PR #117 리뷰)
    assert overview.parse_network('{"items": [{"metadata": "cilium"}]}') is None
    assert overview.parse_network('{"items": [{"metadata": {"name": ["x"]}}]}') is None
    assert overview.parse_network('{"items": [{"metadata": {"name": "cilium"}, "spec": "x"}]}') is None
    # 컨테이너가 없으면 이름만 안다
    assert overview.parse_network(json.dumps({"items": [{"metadata": {"name": "cilium"}, "spec": {}}]})) == {
        "name": "Cilium", "version": None, "source": "daemonset"}


@pytest.mark.parametrize(("image", "version"), [
    ("quay.io/cilium/cilium:v1.20.3@sha256:abc", "1.20.3"),
    ("quay.io/cilium/cilium:v1.20.3", "1.20.3"),
    ("602401143452.dkr.ecr.ap-northeast-2.amazonaws.com/amazon-k8s-cni:v1.20.4-eksbuild.1", "1.20.4-eksbuild.1"),
    ("docker.io/calico/node:3.30.0", "3.30.0"),
    ("registry.local:5000/cilium/cilium:v1.19.0", "1.19.0"),     # 레지스트리 포트는 태그가 아니다
    ("registry.local:5000/cilium/cilium", None),
    ("quay.io/cilium/cilium@sha256:abc", None),                  # 다이제스트만 있다
    ("quay.io/cilium/cilium:latest", None),
    ("cilium", None),
    ("weaveworks/weave-kube:vnext", "vnext"),                    # 숫자가 아니면 v 를 떼지 않는다
    ("", None),
    (None, None),
])
def test_버전은_이미지_태그에서_뗀다(image, version):
    assert overview.image_version(image) == version


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
                            "events": "events", "daemonsets": "daemonsets", "nodes": "nodes"}[args[1]]))


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
    # GPU 가 없다고 이미 아는 클러스터 — 처음 보는 클러스터는 꺼진 GPU 클러스터인지 파드를 한 번 더 본다 (test_cluster_gpu.py)
    cluster = fake_server.add_cluster(owner="u-1", team_id="t-1", context="eks-shop", namespace="shop",
                                      credential={"token": "secret-token"}, has_gpu=False)
    seen: list[dict] = []

    def fake(args, *, context, dry_run=False, stdin=None, timeout=30, kubeconfig=None):
        seen.append({"args": args, "context": context, "dry_run": dry_run, "kubeconfig": kubeconfig, "timeout": timeout,
                     "body": kubeconfig.read_text() if kubeconfig else None})
        return _by_resource(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    r = client.get(f"/clusters/{cluster}/overview", headers=OTHER)

    assert r.status_code == 200
    body = r.json()
    assert (body["context"], body["namespace"]) == ("eks-shop", "shop")
    assert len(body["workloads"]) == 7 and len(body["pods"]) == 5 and len(body["events"]) == 20
    assert body["network"] == {"name": "Cilium", "version": "1.20.3", "source": "daemonset"}
    assert body["gpu"] is None                                               # GPU 노드가 없다 (test_cluster_gpu.py)
    assert datetime.fromisoformat(body["at"]).tzinfo is not None
    assert "secret-token" not in r.text
    assert [call["args"] for call in seen] == [
        ["get", "deployments,statefulsets,daemonsets", "-o", "json", "-n", "shop"],
        ["get", "pods", "-o", "json", "-n", "shop"],
        ["get", "events", "-o", "json", "-n", "shop"],
        ["get", "daemonsets", "-o", "json", "-n", "kube-system"],          # CNI — 앞의 세 조회 뒤에 한 번
        ["get", "nodes", "-o", "json"],                                     # GPU — 노드 목록 (못 읽으면 gpu 만 null)
    ]
    # 클러스터 범위 조회는 GPU 를 알아보는 노드 목록뿐이다 — GPU 가 없다고 아는 클러스터는 모든 namespace 파드를 읽지 않는다
    assert not any(flag in call["args"] for call in seen for flag in ("-A", "--all-namespaces"))
    assert all(call["args"].count("-n") == 1 for call in seen[:4])
    assert {call["context"] for call in seen} == {"eks-shop"} and not any(call["dry_run"] for call in seen)
    # 대시보드가 자주 부른다 — 기본 30초보다 짧게 끊는다
    assert {call["timeout"] for call in seen} == {clusters_api.OVERVIEW_TIMEOUT} and clusters_api.OVERVIEW_TIMEOUT < 30
    # server 에서 받은 값으로 만든 임시 kubeconfig 하나로 다섯 번 돌리고, 끝나면 지운다
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


@pytest.mark.parametrize("stderr", [
    'Error from server (Forbidden): daemonsets.apps is forbidden: User "x" cannot list resource "daemonsets" '
    'in API group "apps" in the namespace "kube-system"',
    "Command '[kubectl ...]' timed out after 15 seconds",
])
def test_CNI_조회가_실패해도_개요는_200이고_network만_null(client, fake_server, monkeypatch, caplog, stderr):
    cluster = fake_server.add_cluster(namespace="shop")

    def fake(args, **kwargs):
        if "kube-system" in args:
            return _result(stderr=stderr, success=False)
        return _by_resource(args)

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", fake)
    with caplog.at_level("DEBUG", logger="kukie_agent.clusters_api"):
        r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200
    body = r.json()
    assert body["network"] is None and len(body["workloads"]) == 7
    assert "CNI 조회를 건너뛴다" in caplog.text and "cannot list" not in caplog.text   # 원문은 남기지 않는다


def test_CNI_조회_출력이_깨져도_개요는_200(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster()
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl",
                        lambda args, **k: _result("not json") if "kube-system" in args else _by_resource(args))
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 200 and r.json()["network"] is None


def test_kubectl_출력이_JSON이_아니면_502_CLUSTER_BAD_RESPONSE(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster()
    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", lambda *a, **k: _result("not json"))
    r = client.get(f"/clusters/{cluster}/overview", headers=USER)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "CLUSTER_BAD_RESPONSE"


def test_개요도_스레드_상한을_쓴다(client, fake_server, monkeypatch):
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
