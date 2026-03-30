---
name: graphiti
description: Temporal knowledge graph — search facts and relationships, ingest knowledge, manage episodes, and forget outdated information.
---

# Graphiti Knowledge Graph

Use these tools to interact with the Graphiti temporal knowledge graph. Graphiti extracts entities and relationships from conversations and stores them in a Neo4j graph database for long-term retrieval.

## Tools

| Tool | When to use |
|------|-------------|
| `graphiti_search` | Find facts, entities, and relationships — "What do I know about X?", "What's the relationship between X and Y?", "What was the history of project X?" |
| `graphiti_ingest` | Manually store important information the user wants remembered long-term |
| `graphiti_forget` | Remove outdated, incorrect, or superseded facts from the graph |
| `graphiti_episodes` | Browse recently ingested episodes — useful for checking what was captured |

## Complementing memory-core

This plugin works alongside `memory-core`, not as a replacement:

- **memory-core** (`memory_search`) — searches workspace Markdown files in `memory/`
- **Graphiti** (`graphiti_search`) — searches extracted entities, relationships, and temporal facts from conversations

Use `memory_search` for "what did I write in my notes?" and `graphiti_search` for "what do I know about the relationship between X and Y?" or "what was discussed about X over time?"

## Requirements

- Graphiti server must be running (default: `http://localhost:8100`)
- Neo4j database (managed by Graphiti)
