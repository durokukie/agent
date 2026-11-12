"""새 대화에서 고를 namespace 목록 — kukie_agent/clusters/namespaces.py, GET /clusters/{id}/namespaces (DURO-228)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from helpers import USER
from kukie_agent import server
from kukie_agent.clusters import namespaces
from kukie_agent.kubectl import KubectlResult

FORBIDDEN = ('Error from server (Forbidden): namespaces is forbidden: User "kukie" cannot list resource "namespaces" '
             'in API group "" at the cluster scope')


def _ok(body) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout=json.dumps(body), stderr="", success=True, exit_code=0)


def _fail(stderr: str) -> KubectlResult:
    return KubectlResult(command="kubectl get", stdout="", stderr=stderr, success=False, exit_code=1)


def _ns(name: str, phase: str | None = "Active") -> dict:
    return {"metadata": {"name": name}, **({"status": {"phase": phase}} if phase else {})}


@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)


def _api(monkeypatch, answer: KubectlResult) -> list:
    ran: list = []

    def run(args, **kwargs):
        ran.append(args)
        return answer

    monkeypatch.setattr("kukie_agent.clusters_api.run_kubectl", run)
    return ran


def test_시스템과_지우는_중인_namespace_를_빼고_이름순으로_준다():
    body = {"items": [_ns("web"), _ns("kube-system"), _ns("default"), _ns("old", "Terminating"), _ns("llm", None),
                      {"metadata": {}}]}
    assert namespaces.names(json.dumps(body)) == ["default", "llm", "web"]


@pytest.mark.parametrize("stdout", ["not json", json.dumps({"kind": "Status"}), json.dumps({"items": ["x"]})])
def test_모양이_다른_출력은_ValueError(stdout):
    with pytest.raises(ValueError):
        namespaces.names(stdout)


def test_목록과_등록한_namespace_를_준다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(owner="u-1", namespace="study")
    ran = _api(monkeypatch, _ok({"items": [_ns("study"), _ns("llm"), _ns("kube-public")]}))
    r = client.get(f"/clusters/{cluster}/namespaces", headers=USER)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "default_namespace": "study", "namespaces": ["llm", "study"]}
    assert ran == [namespaces.LIST_COMMAND]


def test_목록을_읽을_권한이_없으면_200_forbidden_과_등록한_namespace(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(owner="u-1", namespace="kukie-demo")
    _api(monkeypatch, _fail(FORBIDDEN))
    r = client.get(f"/clusters/{cluster}/namespaces", headers=USER)
    assert r.status_code == 200
    assert r.json() == {"status": "forbidden", "default_namespace": "kukie-demo", "namespaces": []}


@pytest.mark.parametrize(("answer", "code"), [
    (_fail("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"), "CLUSTER_UNREACHABLE"),
    (_fail("error: You must be logged in to the server (Unauthorized)"), "CLUSTER_AUTH_EXPIRED"),
    (KubectlResult(command="kubectl get", stdout="not json", stderr="", success=True), "CLUSTER_BAD_RESPONSE"),
])
def test_그_밖의_실패는_개요와_같이_502(client, fake_server, monkeypatch, answer, code):
    cluster = fake_server.add_cluster(owner="u-1")
    _api(monkeypatch, answer)
    r = client.get(f"/clusters/{cluster}/namespaces", headers=USER)
    assert r.status_code == 502 and r.json()["detail"]["code"] == code


def test_볼_수_없는_클러스터는_404이고_kubectl은_돌지_않는다(client, fake_server, monkeypatch):
    cluster = fake_server.add_cluster(owner="u-2")
    ran = _api(monkeypatch, _ok({"items": []}))
    r = client.get(f"/clusters/{cluster}/namespaces", headers=USER)
    assert r.status_code == 404 and ran == []
