FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.154.0
RUN npm install --prefix /opt/codex @openai/codex@${CODEX_VERSION}

FROM python:3.13-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends graphviz fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /opt/codex /opt/codex
ENV PATH="/opt/codex/node_modules/.bin:${PATH}" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml /app/
COPY graph_memory /app/graph_memory
COPY evals /app/evals
RUN pip install --no-cache-dir 'neo4j==6.3.1' 'pydantic==2.13.5' . \
    && useradd --create-home --uid 10001 memory \
    && mkdir -p /home/memory/.codex \
    && chown -R memory:memory /home/memory
USER memory
WORKDIR /home/memory
ENTRYPOINT ["graph-memory"]
CMD ["daemon", "/bank"]

FROM runtime AS eval
USER root
RUN pip install --no-cache-dir 'pytest>=8,<10'
COPY tests /checks/tests
COPY pyproject.toml /checks/pyproject.toml
WORKDIR /checks
USER memory

FROM runtime AS production
