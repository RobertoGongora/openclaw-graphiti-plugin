# @robertogongora/graphiti

Temporal knowledge graph plugin for [OpenClaw](https://github.com/openclaw/openclaw) using [Graphiti](https://github.com/getzep/graphiti) + Neo4j.

## What it does

- **Knowledge graph tools**: `graphiti_search` and `graphiti_ingest` available as agent tools for on-demand entity/relationship queries and manual ingestion
- **Auto-capture**: Before compaction or session reset, ingests the conversation into the knowledge graph for entity/relationship extraction (async, via Graphiti's LLM pipeline)
- **Auto-recall**: Optionally injects relevant facts before each turn — off by default, see [Auto-recall vs on-demand search](#auto-recall-vs-on-demand-search)
- **CLI**: `openclaw graphiti status|search|episodes`
- **Slash command**: `/graphiti` for quick health check

## Requirements

- Graphiti server running (e.g., `http://localhost:8100`)
- Neo4j database (Graphiti manages the connection)
- OpenAI API key configured on the Graphiti server

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
| `recallMaxFacts` | number | `1` | Max facts to inject per turn when auto-recall is on |
| `minPromptLength` | number | `10` | Min prompt length to trigger auto-recall |

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
  -> POSTs up to 12,000 chars to Graphiti /messages
  -> Graphiti extracts entities + relationships async (gpt-5-nano or configured model)
  -> Facts become queryable via graphiti_search
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

See the [Graphiti GitHub](https://github.com/getzep/graphiti) for full deployment options including Coolify, Railway, and cloud-hosted Neo4j.

## Status commands

```bash
openclaw graphiti status          # Graphiti server health + episode count
openclaw graphiti search "query"  # Search the knowledge graph
openclaw graphiti episodes        # Recent ingested episodes
openclaw memory status            # File-based memory index (memory-core)
```

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
