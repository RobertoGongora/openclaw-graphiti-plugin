# @robertogongora/graphiti

**Status: legacy, no longer maintained.** This OpenClaw plugin is archived. It is superseded by the standalone `graph_memory` Python service in the same repository, which talks to Neo4j directly and serves any MCP client. Newest stable release: `0.6.2`; the `0.7.0` line only shipped as betas. Full plugin documentation, kept for migration reference: [docs/legacy-openclaw.md](https://github.com/RobertoGongora/openclaw-graphiti-plugin/blob/master/docs/legacy-openclaw.md).

Temporal knowledge graph plugin for [OpenClaw](https://github.com/openclaw/openclaw), backed by a [Graphiti](https://github.com/getzep/graphiti) server and Neo4j.

## What it provides

- Agent tools: `graphiti_search`, `graphiti_ingest`, `graphiti_forget`, `graphiti_episodes`
- Auto-capture of conversation content into the graph (per turn in ContextEngine mode, on compaction/reset in hooks mode)
- Auto-index of files written to the workspace `memory/` directory
- Optional auto-recall (`autoRecall: true`, off by default)
- CLI: `openclaw graphiti status|search|episodes|ingest|logs|backfill`, plus the `/graphiti` slash command

`graphiti_forget` deletes irreversibly: a `query` only lists candidates, and the delete needs a second call with the `uuid` (or the same `query` with `confirm: true` when exactly one fact matches).

## Requirements

- A running Graphiti server (default `http://localhost:8100`) with `OPENAI_API_KEY` set in its environment
- Neo4j, managed by the Graphiti server
- Node.js 20 or newer

## Install

```bash
openclaw plugins install @robertogongora/graphiti@0.6.2
```

## Minimal configuration

```json
{
  "plugins": {
    "entries": {
      "graphiti": {
        "enabled": true,
        "config": { "url": "http://localhost:8100", "groupId": "core" }
      }
    }
  }
}
```

Every option, the ContextEngine lifecycle, provenance format, and the Graphiti Docker Compose snippet are described in [docs/legacy-openclaw.md](https://github.com/RobertoGongora/openclaw-graphiti-plugin/blob/master/docs/legacy-openclaw.md).

## License

MIT
