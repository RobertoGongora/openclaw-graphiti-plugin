# Graph Memory

Standalone, evidence-backed temporal memory for any LLM through **MCP 2026-07-28**.
The runtime talks directly to Neo4j. Its only direct Python dependencies are
**Pydantic** and the **official Neo4j driver**. No Graphiti server, embeddings,
agent framework, or Markdown lookup is required.

Transcripts become typed entities and evidence-backed relationships. Retrieval
recomputes current state by **when a fact was observed or an event happened**,
across sessions. Projects, people, organizations, technologies, decisions,
preferences, lessons, issues, and activities share the same graph and temporal
rules. Plans, contradictions, undated claims, and inferences remain distinguishable.

```text
Transcript → durable episode → LLM extraction → Pydantic + quote validation
                                                ↓ atomic commit
                                       Neo4j entities + facts + evidence
                                                ↓
                                current context / latest evidence by entity and relation
                                                ↓
                           Luna dream → separate candidate → validated insights
```

## Run locally

Python 3.11+ and Neo4j 5.26+ are required. `uv` is a convenient installer; ordinary
`pip install .` also works. Docker Compose runs the full local system: Neo4j, its browser UI, the background worker, and the MCP server. See [Docker setup](docs/docker.md) for authentication and mounted sources. Docker is optional for a Python-only installation.

```sh
cp .env.example .env
# Set your memory-bank path and a private MCP token in .env, then authenticate Codex.
docker compose up -d --build
```

The MCP endpoint is `http://127.0.0.1:8765/mcp`. `serve` defaults to stdio for
clients that launch subprocesses. See [MCP requests and client setup](docs/mcp.md).
The development database binds only to localhost and persists in a named volume.
Use Neo4j authentication and a private network for a shared installation.

### Extraction and dreaming with Terra

Log into your installed Codex CLI, then:

```sh
export MEMORY_LLM=codex
export MEMORY_MODEL=gpt-5.6-terra
export MEMORY_REASONING_EFFORT=low
uv run graph-memory --namespace personal ingest /path/to/session.jsonl --extract
uv run graph-memory --namespace personal recall Atlas
uv run graph-memory --namespace personal latest Atlas --relation decided
uv run graph-memory --namespace personal latest Atlas --relation resolved
```

The CLI adapter uses `codex exec` with isolated working directories, read-only
sandboxing, no inherited user configuration, no persistent session, disabled
shell/web tools, and schema-constrained output. The generated JSON Schema includes the event-time constraints enforced at commit.
A rejected schema or evidence candidate gets a bounded correction attempt. Present-tense state and plans use
the originating message time; event occurrences require their own time evidence.
Extraction failures remain visible and retryable.

Alternatively, `MEMORY_LLM=compatible` uses a configured chat-completions endpoint
(`MEMORY_LLM_URL`, `MEMORY_MODEL`, optional `MEMORY_LLM_API_KEY`). No provider SDK is
needed. The worker requires a configured model. MCP sessions only queue information;
Python and CLI retain the prepare/commit operations for engine integrations.

## MCP tools

Every tool carries an explicit namespace and all necessary identifiers. No
transport session, initialize handshake, sampling callback, or agent host is
required for the 2026 protocol.

| Tool | Advertised description |
| --- | --- |
| `memory_recall` | Use when the user asks about their projects, preferences, people, decisions, or past work. |
| `memory_latest` | Use when the user asks when something last happened or what was most recently recorded about a subject. |
| `memory_ingest` | Use when the user asks you to remember something or when saving new information from a conversation. |
| `memory_retract` | Use when the user says a remembered fact is incorrect or should no longer inform answers. |
| `memory_merge` | Use when separate memory entries are confirmed to refer to the same person, project, or thing. |
| `memory_render` | Use when the user wants to see their memory graph or how its facts connect. Shows the whole graph by default, or a selected view using optional Cypher. |

Only these six operations are exposed through MCP. Read-only mode exposes recall, latest,
and rendering. Extraction, commit, repair, and dreaming stay inside the engine and CLI;
calling an internal operation through MCP is rejected, even by name.

`memory_recall` takes an entity name, key, or alias, not an arbitrary natural-language
question. Tool-using LLMs turn a question into its subject. Ambiguous identities
are returned explicitly. Default output limits are applied **after** temporal
resolution. `memory_latest` takes `entity` and an optional typed `relation`; it
resolves complete evidence before output limits and returns all co-latest facts
instead of picking an arbitrary winner. It is not a replacement for the complete
current-state neighborhood returned by `memory_recall`.

### Temporal rules

- Source messages retain their timestamps and IDs. Every relationship cites an
  exact quote from an ingested message. Original content stays in the graph.
- State assertions may use an exclusive `slot`, such as `production-primary`.
  Multiple databases otherwise coexist. A planned migration cannot displace the
  deployed database. Equal-time incompatible assertions remain conflicts.
- Confirmed chronological events require timezone-aware occurrence times. Undated
  event claims remain time-uncertain. `memory_latest` returns an uncertain status
  when unresolved claims might be newer than its latest dated evidence.
  Undated Markdown claims retain
  filesystem modification time and creation time where available (birthtime, not
  Unix ctime). They appear in a `documented` lane ordered by file date, labeled
  `document_updated`/`document_created`. These dates never override explicit
  observations or become occurrence times. No file date means uncertain.
- Latest means **latest committed evidence**. Responses disclose pending and
  failed ingestion. No system can know about a session it has never received.
- Framework-to-language inference follows explicit supported graph edges.
  Inferences disappear when their supporting facts stop being current.
- Canonical keys and explicit aliases connect sessions. People, organizations,
  projects, and habits with different keys are not merged merely because their
  display names match. Broad-name ambiguity is returned for disambiguation;
  explicit merges preserve sources.
- Reads restore missing structural links when their durable endpoints and source
  exist. Facts missing evidence are excluded and counted in freshness.

## History and replay

Knowledge changes are journaled atomically under `audit:<namespace>`. Ordinary
recall uses the current graph; optional `known_at` or `at_change` inputs reconstruct
past identities, facts, corrections, and published insights. `as_of` remains the
event-time cutoff. Existing data starts with a dated baseline, not invented past
history. [History commands and examples](docs/history.md) describe verification
and read-only replay into a separate namespace.

## Dreaming

Inspired by [Claude Managed Agents Dreams](https://platform.claude.com/docs/en/managed-agents/dreams),
our implementation snapshots an existing graph plus selected ingested sessions,
then asks Luna to identify useful patterns and contradictions. This is a local
implementation of that workflow, not a call to Anthropic's Dreams API.

```sh
uv run graph-memory --namespace personal dream Atlas \
  --episode EPISODE_ID --episode ANOTHER_EPISODE_ID
```

The output is a separate durable `MemoryDream`. `--apply` promotes supported
**inferences**, preserving the input facts. Promotion rejects stale graph revisions.
Dream operations are available through Python and CLI `call memory_dream_*`, not MCP.
Dream scheduling and promotion remain explicit operator actions; the ingestion daemon
does not silently generate or publish new insights.

## Import and continuous ingestion

```sh
# Inventory, then stage complete redacted source content in an isolated sandbox.
uv run graph-memory --namespace sandbox:import import ~/.claude/projects ~/.codex/memories
uv run graph-memory --namespace sandbox:import import ~/.claude/projects ~/.codex/memories --apply
# Bounded, resumable extraction of the durable inbox.
MEMORY_LLM=codex uv run graph-memory --namespace sandbox:import work --limit 10
```

Importers support Claude Code and Codex JSONL transcripts, `.md`, and `.txt`.
Large sources are chunked; source paths are provenance, never retrieval targets.
JSONL adapters skip duplicate event representations and non-message tool metadata.
Known credential patterns are redacted, but redaction is not a universal secret detector.

For continuous intake, Docker runs `daemon /bank`: it watches the mounted memory
bank and drains queued messages independently. Pass `--transcripts /sessions` for
append-only session files. Alternatively use `work --watch` with the generated
Claude hooks or the host-neutral `follow` watcher. [Client integration](docs/clients.md) covers native
memories off, hook installation, transcript cursors, retry behavior, and the
calling-agent instructions. Installation does not change global host settings or
modify either original memory bank. A complete extraction receipt marks when
new facts are visible.

## Evals and safe revisions

See [evals/README.md](evals/README.md). The harness uses pytest plus a small
standard-library behavioral runner; Promptfoo/Node is not needed for the new runtime.
Fixtures contain fixed expectations. Runs never regenerate expected outcomes.
A real fresh-session Claude A/B runner compares native memory against MCP using
identical source bytes, read-only access, fixed answers, and actual tool traces.

```sh
MEMORY_TEST_NEO4J_URI=bolt://127.0.0.1:17687 uv run pytest -q
MEMORY_TEST_NEO4J_URI=bolt://127.0.0.1:17687 MEMORY_LLM=codex \
  uv run python -m evals.run --runs 2 --output .local/baseline-candidate.json
```

Tests and model evals use unique namespaces and clean up only their own data.
Reports bind to the engine source, installed dependency versions, golden suite,
model, and reasoning effort. Use a separate test database for CI and shared deployments.
The baseline remains a **candidate** until reviewed; passing tests are not
proof that all real-world extraction is correct.

Engine updates replay selected sources into a candidate graph. They expose added
and removed claims, rerun affected dreams, require golden and project-specific
checks, and atomically promote only if both live and candidate revisions still
match. Changed diffs require explicit acceptance; unchanged outcomes can promote
automatically. See [revision workflow](docs/revisions.md).

## Configuration

| Variable | Default / meaning |
| --- | --- |
| `NEO4J_URI` | `bolt://127.0.0.1:7687` |
| `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | `neo4j`, unset, `neo4j` |
| `MEMORY_NAMESPACE` | `personal`; a shared HTTP server is bound to one namespace |
| `MEMORY_HTTP_TOKEN` | Optional bearer auth locally; required for non-loopback HTTP |
| `MEMORY_LLM` | `caller`, `codex`, or `compatible` |
| `MEMORY_MODEL` | `gpt-5.6-terra` for Codex |
| `MEMORY_REASONING_EFFORT` | `low` for Codex; configurable |

HTTP validates Origin and routing headers. Its bearer authentication is a
private-service option; a public OAuth/OIDC deployment requires an external auth
layer. No public deployment or client auto-configuration is performed by installation.

## Repository transition

The old TypeScript/OpenClaw plugin remains available for migration reference and
its existing tests. It is not imported by the Python service. Its original setup
is documented in [legacy OpenClaw documentation](docs/legacy-openclaw.md).
Existing Graphiti data is not silently reinterpreted under this schema.

### Render the graph in chat

Call `memory_render` with `{"namespace":"personal"}` for the entire knowledge
graph, including disconnected source episodes. Defaults are 10,000 nodes and
30,000 relationships; any truncation is reported in the image and metadata.
An optional `cypher` selects a subgraph. The response contains a PNG image block,
ready for an MCP client to display. See [rendering examples](docs/rendering.md).
