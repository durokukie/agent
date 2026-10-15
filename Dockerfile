# syntax=docker/dockerfile:1
# Kukie 에이전트 컨테이너 (DURO-107 4단계). 리버스 프록시 뒤에서 /api/* 가 /api 를 떼고 여기 8000 으로 온다.
# 띄우는 법은 kukie-electron/deploy/README.md — 도커 컴포즈가 이 파일을 빌드한다.

# 두 단계가 같은 베이스를 쓴다 — 2단계는 1단계가 만든 .venv 를 그대로 옮겨 오므로 Python 이 같아야 한다
ARG PYTHON_IMAGE=python:3.14-slim

# ── 1단계: uv 로 .venv 를 만든다. uv 와 내려받기 캐시는 여기에만 남는다
FROM ${PYTHON_IMAGE} AS build
# .python-version 과 같은 3.14 — 어긋나면 아래 uv sync 가 멈춘다
# uv 는 CI 의 SETUP_UV_VERSION 과 같은 버전. 태그는 옮겨질 수 있어 digest 로 고정한다
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv
WORKDIR /app
# uv.lock 에 적힌 버전 그대로 깐다 — 빌드할 때마다 새로 고르지 않는다. 이미지 안의 Python 을 쓰고 받아 오지 않는다
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# 의존성 먼저, 소스는 나중에 — 소스만 바뀌면 의존성 레이어를 다시 쓴다. 앱도 .venv 에 설치한다(--no-editable)
# DB 드라이버는 없다 — agent 는 DB 를 갖지 않고 저장은 Kukie API 서버(kukie-server) 내부 API 로 한다 (DURO-113)
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY kukie_agent ./kukie_agent
# --reinstall-package: 앱 휠은 매번 새로 굽는다. uv 는 pyproject.toml 이 그대로면 캐시 마운트에 남은 예전 휠을 다시 깐다 —
# 소스만 바뀐 걸 모르므로, 코드를 받아 `docker compose up -d --build` 해도 옛 코드가 돈다. 의존성은 여전히 캐시에서 온다
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --reinstall-package kukie-agent
# 깔린 앱이 방금 복사한 소스와 같은지 — 다르면 옛 코드를 실은 이미지를 내보내지 않고 빌드에서 멈춘다.
# 바이트코드와 숨김 파일(맥의 .DS_Store 등)은 휠에 들어가지 않으므로 견주지 않는다
RUN diff -r -x __pycache__ -x '.*' kukie_agent .venv/lib/python3.*/site-packages/kukie_agent

# ── 2단계: 실행 이미지 — .venv 와 kubectl 만. 소스 폴더는 두지 않는다(두면 설치본 대신 그쪽이 import 된다)
FROM ${PYTHON_IMAGE}
# kubectl 은 등록된 클러스터에 대고 실행하는 도구라 이미지 안에 있어야 한다 (kukie_agent/kubectl/runner.py).
# 버전을 고정하고 sha256 을 대조한다 — 빌드마다 다른 바이너리가 들어오지 않게.
ARG KUBECTL_VERSION=v1.37.0
ARG TARGETARCH
# TARGETARCH 는 BuildKit 이 채운다 — 구형 빌더면 빈 값이 URL 에 들어가 원인 모를 404 가 되므로 먼저 확인한다 (PR #82 리뷰)
RUN : "${TARGETARCH:?BuildKit 이 필요하다 — DOCKER_BUILDKIT=1 로 빌드}" \
 && apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
 && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/kubectl && rm /tmp/kubectl.sha256 \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# 옮겨 온 .venv 가 이 베이스에서 도는지 — 어긋나면 띄울 때가 아니라 빌드에서 멈춘다
RUN python -c "import kukie_agent.server"

# 쓰기 가능한 HOME — 개발용 flat 엔드포인트(KUKIE_DEV_AUTH=1, server 없음)의 승인 계획 파일(~/.kukie/plans)이 여기 쌓인다.
# 대화 경로는 저장을 전부 kukie-server 에 하므로 이 디렉터리에 지킬 상태가 없다 — 볼륨이 없어도 된다
RUN useradd --create-home --home-dir /data --uid 1000 kukie
USER kukie
ENV HOME=/data PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
# --forwarded-allow-ips 는 기본값(127.0.0.1)으로 둔다 — uvicorn 은 proxy-headers 미들웨어가 기본으로 켜져 있고, 그 헤더를 믿을지는 이 값이 정한다.
# 코드가 scheme·client IP 를 읽는 데가 없고 8000 은 컴포즈 밖에 안 열리므로 지금은 아무 헤더도 안 믿는 쪽이 맞다 (PR #82 리뷰).
# 나중에 IP 기반 판단(레이트리밋·감사 로그)이 생기면 프록시 서브넷을 FORWARDED_ALLOW_IPS 로 좁혀 연다
CMD ["uvicorn", "kukie_agent.server:app", "--host", "0.0.0.0", "--port", "8000"]
