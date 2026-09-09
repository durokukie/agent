"""업로드한 kubeconfig 를 검사하고 context 하나로 정규화한다 (기획 04 §8 "업로드 검증").

거부하는 것이 핵심이다. 서버가 남의 kubeconfig 를 그대로 쓰면 두 가지가 위험하다.

  1) exec 플러그인 — kubeconfig 는 `aws eks get-token` 같은 **외부 명령을 실행하라**고 적을 수 있다.
     그대로 두면 업로드한 사람이 서버에서 임의 명령을 돌릴 수 있다. Cloud Identity 지원(기획 04 §6)
     전까지는 무조건 거부한다.
  2) 파일 경로 참조 — `client-certificate: /etc/…` 처럼 **서버의 파일을 읽으라**고 적을 수 있다.
     인라인 `*-data` 만 받는다.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse

import yaml


class KubeconfigRejected(ValueError):
    """업로드한 kubeconfig 를 받아들일 수 없다. 문구를 그대로 사용자에게 보여준다."""


@dataclass(frozen=True)
class ParsedCluster:
    """등록에 필요한 값만 남긴 결과. 원본 kubeconfig 는 보관하지 않는다."""

    context_name: str
    api_server: str
    ca_data: str | None            # base64 (인라인). insecure-skip-tls-verify 면 None
    namespace: str
    credential: dict[str, str]     # {"token": …} 또는 {"client_certificate_data": …, "client_key_data": …}
    insecure: bool

    @property
    def fingerprint(self) -> str:
        """이 클러스터를 가리키는 값. 채팅방이 복사해 "승인한 대상 = 실행 대상" 을 확인한다 (기획 04 §8)."""
        return hashlib.sha256(f"{self.api_server}\n{self.ca_data or ''}".encode()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KubeconfigRejected(message)


def _named(entries: object, kind: str) -> dict[str, dict]:
    _require(isinstance(entries, list), f"kubeconfig 에 {kind} 목록이 없습니다")
    found: dict[str, dict] = {}
    for entry in entries:  # type: ignore[union-attr]
        if not isinstance(entry, dict):
            continue
        name, body = entry.get("name"), entry.get(kind)
        if isinstance(name, str) and isinstance(body, dict):
            found[name] = body
    _require(bool(found), f"kubeconfig 에 {kind} 가 없습니다")
    return found


def _base64(value: object, field: str) -> str:
    _require(isinstance(value, str) and value.strip() != "", f"{field} 값이 비어 있습니다")
    try:
        base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KubeconfigRejected(f"{field} 가 base64 가 아닙니다") from exc
    return str(value)


def _check_api_server(url: str, *, allow_local: bool) -> None:
    parsed = urlparse(url)
    _require(parsed.scheme == "https", f"클러스터 주소는 https 여야 합니다: {url}")
    _require(bool(parsed.hostname), f"클러스터 주소에 호스트가 없습니다: {url}")
    if allow_local:
        return
    host = str(parsed.hostname)
    try:
        address = ip_address(host)
    except ValueError:
        _require(host not in {"localhost", "localhost.localdomain"},
                 "로컬 주소는 등록할 수 없습니다 (서버에서 닿지 않습니다)")
        return
    _require(
        not (address.is_loopback or address.is_private or address.is_link_local),
        f"사설·로컬 주소는 등록할 수 없습니다 (서버에서 닿지 않습니다): {host}",
    )


def _credential(user: dict) -> dict[str, str]:
    # exec 은 "서버에서 이 명령을 실행하라" 는 뜻이다 — 업로드한 사람이 서버를 조종하게 된다
    _require(
        "exec" not in user,
        "외부 인증 프로그램(exec)을 쓰는 kubeconfig 는 아직 지원하지 않습니다 — "
        "EKS/GKE/AKS 연동은 후속 작업입니다",
    )
    _require("auth-provider" not in user, "auth-provider 방식은 아직 지원하지 않습니다")
    for path_field in ("client-certificate", "client-key", "tokenFile"):
        _require(
            path_field not in user,
            f"파일 경로({path_field})는 쓸 수 없습니다 — 인라인 값만 받습니다",
        )

    token = user.get("token")
    if isinstance(token, str) and token.strip():
        return {"token": token.strip()}

    certificate, key = user.get("client-certificate-data"), user.get("client-key-data")
    if certificate is not None or key is not None:
        return {
            "client_certificate_data": _base64(certificate, "client-certificate-data"),
            "client_key_data": _base64(key, "client-key-data"),
        }

    _require(
        "username" not in user and "password" not in user,
        "아이디·비밀번호 방식은 지원하지 않습니다 (토큰 또는 인증서를 쓰세요)",
    )
    raise KubeconfigRejected("kubeconfig 에 자격증명(토큰 또는 client certificate)이 없습니다")


def parse_kubeconfig(
    text: str, *, context_name: str | None = None, allow_local: bool = False
) -> ParsedCluster:
    """kubeconfig 본문에서 context 하나를 골라 등록에 필요한 값만 뽑는다.

    context_name 을 주지 않으면 current-context 를 쓰고, 그것도 없는데 context 가 여럿이면
    고르라고 거절한다 — 서버가 임의로 고르면 사용자가 의도하지 않은 클러스터에 붙는다.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KubeconfigRejected(f"kubeconfig 를 읽을 수 없습니다 (YAML 오류): {exc}") from exc
    _require(isinstance(document, dict), "kubeconfig 형식이 아닙니다")
    assert isinstance(document, dict)

    contexts = _named(document.get("contexts"), "context")
    chosen = context_name or document.get("current-context")
    if not chosen:
        _require(
            len(contexts) == 1,
            f"context 가 여럿입니다. 하나를 골라 주세요: {', '.join(sorted(contexts))}",
        )
        chosen = next(iter(contexts))
    _require(
        chosen in contexts,
        f"그런 context 가 없습니다: {chosen} (있는 것: {', '.join(sorted(contexts))})",
    )
    context = contexts[str(chosen)]

    clusters = _named(document.get("clusters"), "cluster")
    users = _named(document.get("users"), "user")
    cluster_key, user_key = context.get("cluster"), context.get("user")
    _require(cluster_key in clusters, f"context 가 가리키는 cluster 가 없습니다: {cluster_key}")
    _require(user_key in users, f"context 가 가리키는 user 가 없습니다: {user_key}")
    cluster, user = clusters[str(cluster_key)], users[str(user_key)]

    _require("certificate-authority" not in cluster,
             "파일 경로(certificate-authority)는 쓸 수 없습니다 — 인라인 값만 받습니다")
    server = cluster.get("server")
    _require(isinstance(server, str) and server.strip() != "", "클러스터 주소(server)가 없습니다")
    _check_api_server(str(server), allow_local=allow_local)

    insecure = bool(cluster.get("insecure-skip-tls-verify"))
    ca = cluster.get("certificate-authority-data")
    if ca is None:
        _require(insecure, "CA 인증서(certificate-authority-data)가 없습니다")
        ca_data = None
    else:
        ca_data = _base64(ca, "certificate-authority-data")

    namespace = context.get("namespace")
    return ParsedCluster(
        context_name=str(chosen),
        api_server=str(server).rstrip("/"),
        ca_data=ca_data,
        namespace=str(namespace) if isinstance(namespace, str) and namespace else "default",
        credential=_credential(user),
        insecure=insecure,
    )
