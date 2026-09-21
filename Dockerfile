# syntax=docker/dockerfile:1
# Kukie 에이전트 컨테이너 (DURO-107 4단계). 리버스 프록시 뒤에서 /api/* 가 /api 를 떼고 여기 8000 으로 온다.
# 띄우는 법은 kukie-electron/deploy/README.md — 도커 컴포즈가 이 파일을 빌드한다.
FROM python:3.10-slim

# CI 와 같은 3.10. kubectl 은 등록된 클러스터에 대고 실행하는 도구라 이미지 안에 있어야 한다 (kukie/kubectl/runner.py).
# 버전을 고정하고 sha256 을 대조한다 — 빌드마다 다른 바이너리가 들어오지 않게.
ARG KUBECTL_VERSION=v1.37.0
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
 && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/kubectl && rm /tmp/kubectl.sha256 \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY kukie ./kukie
# psycopg 는 KUKIE_DATABASE_URL 을 Postgres 로 줄 때 쓴다 (.env.example). 기본 SQLite 면 있어도 무해
RUN pip install --no-cache-dir . 'psycopg[binary]>=3.1'

# 상태 파일 — 기본 SQLite(~/.kukie/kukie.db)와 승인 계획(~/.kukie/plans) — 이 HOME 아래로 모인다.
# 컴포즈는 /data 를 볼륨으로 잡아 컨테이너를 갈아도 남긴다
RUN useradd --create-home --home-dir /data --uid 1000 kukie
USER kukie
ENV HOME=/data PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "kukie.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
