---
name: graphiti
description: Legacy OpenClaw Graphiti plugin tools (graphiti_search, graphiti_ingest, graphiti_forget, graphiti_episodes). Use only when these graphiti_* tools are present or the user names Graphiti explicitly; generic memory requests belong to the graph-memory `memory` skill.
trigger: graphiti, graphiti_search, graphiti_ingest, graphiti_forget, graphiti_episodes, openclaw graphiti
---

# Graphiti Knowledge Graph

**Status: legacy.** This skill belongs to the archived OpenClaw Graphiti plugin, superseded by the standalone `graph_memory` service and its `memory` skill (`plugins/graph-memory/skills/memory/SKILL.md`); see `docs/legacy-openclaw.md`.

Use these tools to interact with the Graphiti temporal knowledge graph. Graphiti extracts entities and relationships from conversations and stores them in a Neo4j graph database for long-term retrieval.

## Tools

| Tool | Parameters | When to use |
|------|------------|-------------|
| `graphiti_search` | `query` (string, required); `limit` (number 1–50, default 10); `groupIds` (string[], default: current group) | Find facts, entities, and relationships — "What do I know about X?", "What's the relationship between X and Y?", "What was the history of project X?" |
| `graphiti_ingest` | `content` (string, required); `name` (string, episode label); `source` (string, default `manual`) | Manually store important information the user wants remembered long-term |
| `graphiti_forget` | `uuid` (string) or `query` (string) — one is required; `type` (`fact` \| `episode`, default `fact`); `confirm` (boolean, default `false`) | Remove outdated, incorrect, or superseded facts from the graph |
| `graphiti_episodes` | `limit` (number 1–50, default 10); `sessionKey` (string, filter by session) | Browse recently ingested episodes — useful for checking what was captured; shows episode UUIDs |

### Forgetting is two-step

Deletion is irreversible. A `query` never deletes by itself: it lists the matching facts with their UUIDs. Show the candidate to the user, then call `graphiti_forget` again with the `uuid`. Passing the same `query` with `confirm: true` deletes only when exactly one fact matches. Episodes can only be deleted by `uuid` with `type: "episode"`.

## Complementing memory-core

This plugin works alongside `memory-core`, not as a replacement:

- **memory-core** (`memory_search`) — searches workspace Markdown files in `memory/`
- **Graphiti** (`graphiti_search`) — searches extracted entities, relationships, and temporal facts from conversations

Use `memory_search` for "what did I write in my notes?" and `graphiti_search` for "what do I know about the relationship between X and Y?" or "what was discussed about X over time?"

## Requirements

- Graphiti server must be running (default: `http://localhost:8100`)
- Neo4j database (managed by Graphiti)
