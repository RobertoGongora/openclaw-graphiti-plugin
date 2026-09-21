# syntax=docker/dockerfile:1
# Base images are pinned by digest; refresh with
#   docker buildx imagetools inspect <image>:<tag> --format '{{.Manifest.Digest}}'
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.11@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a

FROM ${UV_IMAGE} AS uv

# npm is only the delivery vehicle: the package wraps one static musl binary per
# architecture, so node never reaches the final image.
FROM ${NODE_IMAGE} AS codex
ARG CODEX_VERSION=0.154.0
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) pkg=codex-linux-x64; triple=x86_64-unknown-linux-musl ;; \
      arm64) pkg=codex-linux-arm64; triple=aarch64-unknown-linux-musl ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    npm install --prefix /opt/codex --no-audit --no-fund "@openai/codex@${CODEX_VERSION}"; \
    install -m 0755 "/opt/codex/node_modules/@openai/${pkg}/vendor/${triple}/bin/codex" /codex; \
    test "$(/codex --version)" = "codex-cli ${CODEX_VERSION}"

FROM ${PYTHON_IMAGE} AS python-base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:${PATH}"

# Dependencies come from uv.lock, so the image runs what CI tested.
FROM python-base AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY graph_memory ./graph_memory
COPY evals ./evals
RUN uv sync --locked --no-dev --no-editable

FROM python-base AS runtime
# bubblewrap: codex sandboxes with the system bwrap and warns when it must fall back
# to its bundled copy. tini: PID 1 must forward SIGTERM under plain `docker run`,
# otherwise the daemon never drains and is killed after the grace period.
RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz fonts-dejavu-core bubblewrap tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 memory \
    && install -d -o memory -g memory /home/memory/.codex
# Nine worker threads each kept their own glibc arena high-water mark (6.5 GB RSS).
ENV MALLOC_ARENA_MAX=2
COPY --from=codex /codex /usr/local/bin/codex
COPY --from=build /opt/venv /opt/venv
USER 10001
WORKDIR /home/memory
ENTRYPOINT ["tini", "--", "graph-memory"]
# Every real role needs a database and provider configuration; compose supplies the
# command, and a bare `docker run` must not crash-loop on missing environment.
CMD ["--help"]

FROM runtime AS eval
USER 0
# The process-group tests shell out to `ps`, which slim images do not ship.
RUN apt-get update && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE /checks/
COPY graph_memory /checks/graph_memory
COPY evals /checks/evals
COPY tests /checks/tests
WORKDIR /checks
# Locked dev group: pytest is the exact version CI runs.
RUN UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_CACHE=1 \
    uv sync --locked --no-editable
USER 10001

FROM runtime AS production
ARG GIT_SHA=unknown
ARG CODEX_VERSION=0.154.0
LABEL org.opencontainers.image.title="graph-memory" \
      org.opencontainers.image.description="Evidence-backed temporal memory over stateless MCP and direct Neo4j" \
      org.opencontainers.image.source="https://github.com/RobertoGongora/openclaw-graphiti-plugin" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      com.openai.codex.version="${CODEX_VERSION}"
