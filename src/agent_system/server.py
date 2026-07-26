"""환경 설정으로 runtime과 FastAPI adapter를 조립하는 서버 진입점."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI

from agent_system.config import RuntimeSettings
from agent_system.http import create_app
from agent_system.runtime import build_runtime


def create_app_from_env(env: Mapping[str, str] | None = None) -> FastAPI:
    """환경 mapping을 검증하고 소유 runtime을 가진 FastAPI app을 반환한다."""

    settings = RuntimeSettings.from_env(env)
    return create_app(build_runtime(settings))


__all__ = ["create_app_from_env"]
