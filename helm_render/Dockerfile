# syntax=docker/dockerfile:1
# 렌더링 전용 서비스 이미지 (DURO-137). 빌드 컨텍스트는 helm_render/ 폴더다 — 에이전트 코드와 비밀값이 들어오지 않는다.

# 파이썬과 uv 는 digest 로, helm 은 버전과 sha256 으로 고정한다. 태그는 옮겨질 수 있다
ARG PYTHON_IMAGE=python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966

# ── helm: 받은 파일을 대조한 뒤 바이너리만 다음 단계로 옮긴다. curl 은 실행 이미지에 남지 않는다
FROM ${PYTHON_IMAGE} AS helm
# Argo CD v3.5.3 (hack/tool-versions.sh helm4_version) 과 같은 버전. Argo CD 를 올리면 함께 올린다
ARG HELM_VERSION=v4.2.1
ARG HELM_SHA256_AMD64=479dca836e5b45e8bd222400c5591b0e3a647378f03ff96597180db97c17fdae
ARG HELM_SHA256_ARM64=596b9a73d366c1e72ce67d595c22805480e30914593aafbc9f547694e72814db
ARG TARGETARCH
RUN : "${TARGETARCH:?BuildKit 이 필요하다 — DOCKER_BUILDKIT=1 로 빌드}" \
 && case "$TARGETARCH" in amd64) sum="$HELM_SHA256_AMD64" ;; arm64) sum="$HELM_SHA256_ARM64" ;; *) exit 1 ;; esac \
 && apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSLo /tmp/helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
 && echo "${sum}  /tmp/helm.tgz" | sha256sum -c - \
 && tar -xzf /tmp/helm.tgz -C /tmp "linux-${TARGETARCH}/helm" \
 && install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm

# ── 파이썬 의존성: 잠금 파일 그대로
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY kukie_helm_render ./kukie_helm_render
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --reinstall-package kukie-helm-render

# ── 실행 이미지: .venv 와 helm 만. 플러그인은 없다
FROM ${PYTHON_IMAGE}
COPY --from=helm /usr/local/bin/helm /usr/local/bin/helm
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN python -c "import kukie_helm_render.config" && helm version --short
RUN useradd --no-create-home --home-dir /work --uid 1000 kukie
USER 1000
# 쓸 수 있는 곳은 /work 뿐이다 — 파드에서 emptyDir 을 붙이고 나머지는 읽기 전용이다
ENV HOME=/work TMPDIR=/work HELM_RENDER_WORK_DIR=/work PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["uvicorn", "kukie_helm_render.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header"]
