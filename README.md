# @robertogongora/graphiti

Temporal knowledge graph plugin for [OpenClaw](https://github.com/openclaw/openclaw) using [Graphiti](https://github.com/getzep/graphiti) + Neo4j.

> **⚠️ Upgrading from v0.2.x?** As of v0.3.0, Graphiti no longer claims the `plugins.slots.memory` slot. If you were using `plugins.slots.memory = "graphiti"`, you must re-enable `memory-core` manually — see [Migrating from v0.2.x](#migrating-from-v02x).


## What it does

- **Knowledge graph tools**: `graphiti_search` and `graphiti_ingest` available as agent tools for on-demand entity/relationship queries and manual ingestion
- **Auto-capture**: Before compaction or session reset, ingests the conversation into the knowledge graph for entity/relationship extraction (async, via Graphiti's LLM pipeline)
- **Auto-index**: Automatically creates index episodes in Graphiti when files are written to `memory/`, bridging file-based memory with the knowledge graph
- **Auto-recall**: Optionally injects relevant facts before each turn — off by default, see [Auto-recall vs on-demand search](#auto-recall-vs-on-demand-search)
- **CLI**: `openclaw graphiti status|search|episodes|ingest|backfill`
- **Slash command**: `/graphiti` for quick health check

## Requirements

- Graphiti server running (e.g., `http://localhost:8100`)
- Neo4j database (Graphiti manages the connection)
- `OPENAI_API_KEY` set in the Graphiti server environment — this is separate from OpenClaw's own key. Without it, ingestion returns `202 Accepted` but entity extraction never runs and no episodes appear.

## Installation

### From npm

```bash
openclaw plugins install @robertogongora/graphiti
```

### From local directory

```bash
git clone https://github.com/RobertoGongora/openclaw-graphiti-plugin.git
ln -s /path/to/openclaw-graphiti-plugin ~/.openclaw/extensions/graphiti
```

The plugin declares `openclaw.extensions` in `package.json`, so OpenClaw discovers it automatically from `~/.openclaw/extensions/`.

## Recommended setup

As of v0.3.0, Graphiti does **not** claim the exclusive memory slot. The recommended
setup is to run Graphiti alongside `memory-core`:

- **`memory-core`** owns the memory slot → provides `memory_search` and `memory_get`
  over your workspace Markdown files (`MEMORY.md`, `memory/*.md`)
- **Graphiti** runs as a complementary plugin → provides `graphiti_search` and
  `graphiti_ingest` for temporal knowledge graph queries, and auto-captures
  conversations on compaction/reset

This gives you file-based semantic search (hybrid BM25 + vector) **and** a temporal
knowledge graph, operating independently on different data.

```json
{
  "plugins": {
    "slots": {
      "memory": "memory-core"
    },
    "entries": {
      "memory-core": { "enabled": true },
      "graphiti": {
        "enabled": true,
        "config": {
          "url": "http://localhost:8100",
          "groupId": "core",
          "autoCapture": true,
          "autoRecall": false
        }
      }
    }
  }
}
```

### When to use each tool

| Need | Tool |
|------|------|
| "What did I write about X in my notes?" | `memory_search` (memory-core) |
| "What's the relationship between X and Y?" | `graphiti_search` |
| "What was the history of project X?" | `graphiti_search` (temporal queries) |
| "Remember this fact long-term" | `graphiti_ingest` |

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `url` | string | `http://localhost:8100` | Graphiti server URL |
| `apiKey` | string | _(none)_ | Bearer token for authenticated Graphiti servers |
| `groupId` | string | `core` | Graph namespace (use different IDs per agent) |
| `autoRecall` | boolean | `false` | Inject relevant facts before each turn (opt-in) |
| `autoCapture` | boolean | `true` | Ingest conversations on compaction/reset |
| `autoIndex` | boolean | `true` | Create index episodes when files are written to `memory/` |
| `recallMaxFacts` | number | `1` | Max facts to inject per turn when auto-recall is on |
| `minPromptLength` | number | `10` | Min prompt length to trigger auto-recall |
| `debug` | boolean | `true` | Enable structured debug log file |
| `logFile` | string | `~/.openclaw/logs/graphiti-plugin.log` | Custom debug log file path |

## Auto-recall vs on-demand search

By default, `autoRecall` is **off**. The agent can search the knowledge graph at any
time using the `graphiti_search` tool — this is the recommended approach. The agent
decides when graph context is relevant rather than injecting facts on every turn.

When `autoRecall: true`, the plugin fires `before_agent_start`, searches Graphiti with
the incoming prompt, and injects matching facts as a `<graphiti-context>` block. This
adds latency and token cost to every message. Useful when you want persistent background
context without explicitly calling `graphiti_search`.

```json
{
  "plugins": {
    "entries": {
      "graphiti": {
        "config": {
          "autoRecall": true,
          "recallMaxFacts": 5
        }
      }
    }
  }
}
```

## Auto-capture flow

```
Session compacts or resets
  -> before_compaction / before_reset fires
  -> Plugin extracts user+assistant messages (min 4)
  -> Session metadata (session key, agent, channel) embedded in source_description
  -> POSTs up to 12,000 chars to Graphiti /messages
  -> Graphiti extracts entities + relationships async (gpt-5-nano or configured model)
  -> Facts become queryable via graphiti_search
```

## ContextEngine mode (v0.6.0+)

On OpenClaw v2026.3.7+, the plugin manifest declares `kind: "context-engine"` instead of
plain hooks. OpenClaw detects this and wires the plugin into the ContextEngine lifecycle
automatically — **no configuration changes needed**. On older OpenClaw versions, the
plugin falls back to the existing sidecar hooks (`before_agent_start`,
`before_compaction`, `before_reset`), so there is no breaking change.

### How it works

Instead of event hooks that fire at fixed points, the ContextEngine exposes lifecycle
methods that the runtime calls directly:

| Lifecycle method | Replaces hook | Purpose |
|------------------|---------------|---------|
| `assemble()` | `before_agent_start` | Recall relevant facts into the system prompt |
| `ingest()` | _(new)_ | Ingest a single message into the graph |
| `ingestBatch()` | _(new)_ | Batch-ingest multiple messages |
| `afterTurn()` | _(new)_ | Ingest new messages after each turn (replaces batch fallback) |
| `compact()` | `before_compaction` | Graph-aware compaction — ingest then truncate |
| `bootstrap()` | _(new)_ | Health-check and report graph population on startup |
| `onSubagentEnded()` | _(new)_ | Ingest a subagent's findings into the parent graph scope |
| `prepareSubagentSpawn()` | _(new)_ | Inject relevant facts into a spawning subagent's context |

The engine declares `ownsCompaction: true`, meaning it controls the compaction strategy.
During compaction, messages being discarded are first ingested into the knowledge graph,
so long-term memory is preserved even after the context window is truncated.

### Backwards compatibility

- **OpenClaw v2026.3.7+**: ContextEngine methods are used; hooks are not registered.
- **Older OpenClaw**: Hooks fire as before. No behavioural change.
- Config format is identical in both modes.

## Source provenance

Every ingested episode carries a JSON-encoded provenance object in `source_description` for traceability:

```json
{
  "plugin": "openclaw-graphiti",
  "event": "before_compaction",
  "ts": "2026-03-05T10:30:00.000Z",
  "group_id": "core",
  "session_key": "sess-abc-123",
  "agent": "main",
  "channel": "slack",
  "session_start": "2026-03-06T10:00:00.000Z"
}
```

| `event` value | Trigger |
|---------------|---------|
| `manual` | `graphiti_ingest` tool call |
| `before_compaction` | Auto-capture on session compaction (hooks mode) |
| `before_reset` | Auto-capture on `/new` session reset (hooks mode) |
| `cli_ingest` | `openclaw graphiti ingest` CLI command |
| `ingest` | ContextEngine single-message ingestion |
| `ingest_batch` | ContextEngine batch ingestion |
| `after_turn` | ContextEngine after-turn ingestion |
| `compact` | ContextEngine graph-aware compaction |
| `subagent_ended` | ContextEngine subagent result ingestion |

Session fields (`session_key`, `agent`, `channel`, `session_start`) are included
when available and omitted otherwise. The `openclaw graphiti episodes` command
parses this automatically and shows a human-readable summary. Legacy episodes
with plain-text `source_description` still display gracefully.

## Auto-index flow

When the agent writes a file to `memory/` (e.g., `memory/2026-03-05.md` or `memory/topics/project.md`), the plugin automatically creates a lightweight index episode in Graphiti. This bridges file-based memory with the knowledge graph — Graphiti can extract entities and relationships from your memory files.

```
Agent writes to memory/file.md
  -> after_tool_call hook fires
  -> Plugin detects memory/ path in tool params
  -> Reads file metadata (mtime, size, first 500 chars)
  -> Checks state file for idempotency (skips if mtime unchanged)
  -> Ingests index episode with YAML frontmatter + excerpt
  -> Updates state file (~/.openclaw/state/graphiti/graphiti-memory-index.json)
```

Index episodes are distinguishable from other episode types:

| Type | name pattern | role | source_description |
|------|-------------|------|--------------------|
| Manual | `manual-<ts>` | `shiba` | `{"plugin":"openclaw-graphiti","event":"manual",...}` |
| Compaction | `compaction-<ts>` | `conversation` | `{"plugin":"openclaw-graphiti","event":"before_compaction",...}` |
| Reset | `session-reset-<ts>` | `conversation` | `{"plugin":"openclaw-graphiti","event":"before_reset",...}` |
| CLI ingest | `<filename>` | `shiba` | `{"plugin":"openclaw-graphiti","event":"cli_ingest",...}` |
| **Index** | `memory-index::memory/file.md` | `memory-index` | `{"plugin":"openclaw-graphiti","event":"memory_index",...}` |

> **Privacy note:** Indexed memory files are sent to the Graphiti server for entity extraction, which calls your configured LLM. Avoid storing secrets (API keys, passwords) in `memory/` files, or set `autoIndex: false` to disable this feature.

### Backfill existing memory files

To index memory files that were created before the plugin was installed:

```bash
openclaw graphiti backfill                  # Index all files in ./memory
openclaw graphiti backfill --dir /path/to   # Custom memory directory
openclaw graphiti backfill --dry-run        # Show what would be indexed
```

The backfill command checks the state file and only ingests new or modified files.

To disable auto-indexing:

```json
{
  "plugins": {
    "entries": {
      "graphiti": {
        "config": { "autoIndex": false }
      }
    }
  }
}
```

## Remote / non-localhost setup

```json
{
  "plugins": {
    "entries": {
      "graphiti": {
        "config": {
          "url": "https://graphiti.example.com",
          "groupId": "my-agent"
        }
      }
    }
  }
}
```

For Docker, update Neo4j connection if using an external instance:

```yaml
NEO4J_URI: neo4j+s://your-neo4j-host:7687
```

## Authentication

For Graphiti servers behind a reverse proxy or API gateway with bearer token auth:

```json
{
  "plugins": {
    "entries": {
      "graphiti": {
        "config": {
          "url": "https://graphiti.example.com",
          "apiKey": "your-secret-token"
        }
      }
    }
  }
}
```

When `apiKey` is not set, no `Authorization` header is sent.

## Deploying Graphiti

Quick local setup with Docker Compose:

```yaml
services:
  neo4j:
    image: neo4j:5-community
    environment:
      NEO4J_AUTH: neo4j/your-password
    ports:
      - "7474:7474"
      - "7687:7687"
    volumes:
      - neo4j_data:/data
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:7474 || exit 1"]
      interval: 10s
      retries: 5

  graphiti:
    image: zepai/graphiti:latest
    environment:
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: your-password
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      MODEL_NAME: gpt-5-nano
    ports:
      - "127.0.0.1:8100:8000"
    depends_on:
      neo4j:
        condition: service_healthy

volumes:
  neo4j_data:
```

**Environment variables:**
- `OPENAI_API_KEY` — required. Must be set in your shell or a `.env` file alongside `docker-compose.yml`. If missing, Graphiti silently accepts ingestion but skips extraction — check `docker logs openclaw-graphiti` if episodes are not appearing after ingest.
- `MODEL_NAME` — the LLM used for entity and relationship extraction. Defaults to `gpt-4o-mini` if omitted. Recommended: `gpt-4.1-mini` or `gpt-5-nano` for a cost-efficient option that works well for extraction workloads.

See the [Graphiti GitHub](https://github.com/getzep/graphiti) for full deployment options including Coolify, Railway, and cloud-hosted Neo4j.

## Status commands

```bash
openclaw graphiti status          # Graphiti server health + episode count
openclaw graphiti search "query"  # Search the knowledge graph
openclaw graphiti episodes        # Recent episodes (human-readable provenance)
openclaw graphiti episodes --json # Raw JSON output
openclaw graphiti episodes -s <session-key>         # Filter by session key
openclaw graphiti ingest --source-file ./notes.md   # Ingest a file
openclaw graphiti ingest --content "key fact"        # Ingest text directly
openclaw graphiti backfill                           # Index existing memory files into Graphiti
openclaw memory status            # File-based memory index (memory-core)
```

## Debug logging

The plugin writes a structured, append-only debug log for diagnostics. It records HTTP status codes, timing, and result counts -- **never** conversation content, search queries, or PII. Safe to paste in bug reports.

```bash
openclaw graphiti logs           # Show last 50 log entries
openclaw graphiti logs --clear   # Truncate the log file
```

The `status` subcommand also shows the last 20 log entries.

**Default log path:** `~/.openclaw/logs/graphiti-plugin.log`

**Example output:**
```
2026-03-05T10:30:00.123Z [graphiti] healthcheck  status=200 ms=42
2026-03-05T10:30:00.200Z [graphiti] search       status=200 group=core count=3 ms=150
2026-03-05T10:30:01.000Z [graphiti] ingest       status=202 group=core messages=8 ms=320
2026-03-05T10:30:01.500Z [graphiti] episodes     group=core error=unreachable ms=5002
```

### Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `debug` | boolean | `true` | Enable/disable the debug log file |
| `logFile` | string | `~/.openclaw/logs/graphiti-plugin.log` | Custom log file path |

To disable:
```json
{
  "plugins": {
    "entries": {
      "graphiti": {
        "config": { "debug": false }
      }
    }
  }
}
```

### Including in bug reports

When filing a bug report, paste the output of `openclaw graphiti logs`. The log contains only operational metadata (status codes, timing, counts) and is safe to share publicly.

## Migrating from v0.2.x

In v0.2.x, Graphiti declared `kind: "memory"` and claimed the exclusive memory slot,
auto-disabling `memory-core`. This is removed in v0.3.0.

If you were using `plugins.slots.memory = "graphiti"`, update your config:

```json
{
  "plugins": {
    "slots": { "memory": "memory-core" },
    "entries": {
      "memory-core": { "enabled": true },
      "graphiti": { "enabled": true }
    }
  }
}
```

All Graphiti tools, hooks, and CLI remain unchanged.

## License

MIT
