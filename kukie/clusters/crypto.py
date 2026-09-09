"""자격증명 암호화 (기획 04 §8 "암호화").

DB 에 토큰·개인키를 평문으로 두지 않는다. 서버 환경변수 키로 대칭 암호화한다.

**키가 없으면 기동을 거부한다(fail-closed).** 없을 때 평문으로 넘어가면 아무도 눈치채지 못한 채
운영에 나간다 — 그래서 클러스터 기능을 처음 쓰는 순간 크게 실패하도록 둔다.
후속으로 Secret Manager / KMS 로 옮긴다 (기획 04 §4).
"""
from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "KUKIE_SECRET_KEY"


class SecretKeyMissing(RuntimeError):
    """서버에 암호화 키가 없다. 클러스터 자격증명을 다룰 수 없다."""


class CredentialUnreadable(RuntimeError):
    """저장된 자격증명을 풀 수 없다 — 키가 바뀌었거나 값이 깨졌다."""


def generate_key() -> str:
    """새 키 한 개. 설정 안내와 테스트가 쓴다."""
    return Fernet.generate_key().decode()


def _cipher() -> Fernet:
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        raise SecretKeyMissing(
            f"{KEY_ENV} 가 없습니다. 클러스터 자격증명을 암호화할 수 없습니다 — "
            f"`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            f"로 만들어 .env 에 넣으세요"
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise SecretKeyMissing(f"{KEY_ENV} 가 올바른 Fernet 키가 아닙니다") from exc


def available() -> bool:
    """키가 준비돼 있나. 등록 화면을 열기 전에 미리 알려 주려고."""
    try:
        _cipher()
        return True
    except SecretKeyMissing:
        return False


def encrypt(credential: dict[str, str]) -> str:
    """자격증명 묶음을 한 문자열로. 이 값만 DB 에 들어간다."""
    return _cipher().encrypt(json.dumps(credential, sort_keys=True).encode()).decode()


def decrypt(value: str) -> dict[str, str]:
    """실행 직전에만 푼다. 푼 값은 임시 kubeconfig 파일 밖으로 나가지 않는다."""
    try:
        loaded = json.loads(_cipher().decrypt(value.encode()).decode())
    except (InvalidToken, ValueError) as exc:
        raise CredentialUnreadable(
            "저장된 자격증명을 읽을 수 없습니다 — 서버 키가 바뀌었을 수 있습니다. 클러스터를 다시 등록하세요"
        ) from exc
    if not isinstance(loaded, dict):
        raise CredentialUnreadable("저장된 자격증명 형식이 올바르지 않습니다")
    return {str(k): str(v) for k, v in loaded.items()}
