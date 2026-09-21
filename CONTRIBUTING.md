# Contributing

The standalone runtime is Python 3.11+, Pydantic, and the official Neo4j driver.
The older TypeScript/OpenClaw implementation remains a compatibility archive.

```sh
uv sync
uv run ruff check graph_memory evals tests
uv run ruff format --check graph_memory evals tests
make test          # unit tests; database tests skip themselves
make test-db       # starts compose.test.yaml on 127.0.0.1:37687 and runs everything
make test-db-down
uv build
# Preserve the existing plugin tests while the archive remains in this repo.
npm ci --ignore-scripts
npm test
```

Use the disposable Neo4j from `compose.test.yaml` (`make test-db-up`). It has no
authentication and no persistence. Tests allocate and remove only their own random
namespaces. Never point tests or evals at a live graph: ports 17687 and 27687 are
the personal and transcripts databases. Model evals require a configured
Codex login or compatible endpoint; see [evals](evals/README.md).

For extraction, prompt, schema, identity, or temporal changes, add a regression
case with independently established expectations, run repeated model evals, and
replay representative real sources through the [revision workflow](docs/revisions.md).
Do not rewrite expected outcomes from new model output or apply ad hoc backfills.
Review differences, rerun affected dreams, and promote only validated candidates.
Keep private transcripts, local configs, and real-bank reports under `.local/`.

Keep tool schemas and dispatch together. Model output must remain data, never
executable Cypher, shell commands, or filesystem paths to follow at retrieval.
Prefer standard-library utilities to new runtime dependencies. Record any
behavioral contract changes and their validation in the ADR and eval documentation.
