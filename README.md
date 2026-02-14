# openclaw-graphiti-plugin

Temporal knowledge graph plugin for [OpenClaw](https://github.com/openclaw/openclaw) using [Graphiti](https://github.com/getzep/graphiti) + Neo4j.

## What it does

- **Auto-recall**: Before each conversation turn, searches the knowledge graph for relevant facts based on the user's prompt and injects them into context
- **Auto-capture**: After each conversation, ingests the exchange into the knowledge graph for entity/relationship extraction
- **Native tools**: `graphiti_search` and `graphiti_ingest` available as agent tools
- **CLI**: `openclaw graphiti status|search|episodes`
- **Slash command**: `/graphiti` for quick health check

## Requirements

- Graphiti server running (e.g., `http://localhost:8100`)
- Neo4j database (Graphiti manages this)
- OpenAI API key configured in Graphiti server

## Installation

### Link from local directory

```bash
# Clone the repo
git clone https://github.com/RobertoGongora/openclaw-graphiti-plugin.git

# Add to OpenClaw config
openclaw config set plugins.load.paths '["/path/to/openclaw-graphiti-plugin"]'
```

### Manual config

Add to `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "load": {
      "paths": ["/path/to/openclaw-graphiti-plugin"]
    },
    "entries": {
      "graphiti": {
        "enabled": true,
        "config": {
          "url": "http://localhost:8100",
          "groupId": "shiba-core",
          "autoRecall": true,
          "autoCapture": true,
          "recallMaxFacts": 10,
          "minPromptLength": 10
        }
      }
    }
  }
}
```

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `url` | string | `http://localhost:8100` | Graphiti server URL |
| `groupId` | string | `shiba-core` | Graph namespace |
| `autoRecall` | boolean | `true` | Inject relevant facts before each turn |
| `autoCapture` | boolean | `true` | Ingest conversations after each turn |
| `recallMaxFacts` | number | `10` | Max facts to inject on recall |
| `captureRoles` | string[] | `["user", "assistant"]` | Message roles to capture |
| `minPromptLength` | number | `10` | Min prompt length to trigger recall |

## How it works

### Auto-recall flow
```
User sends message
  → before_agent_start fires
  → Plugin searches Graphiti with user's prompt
  → Top facts injected as <graphiti-context> block
  → Agent sees relevant knowledge alongside the prompt
```

### Auto-capture flow
```
Agent finishes response
  → agent_end fires
  → Plugin extracts conversation text
  → Filters out heartbeats, system messages, short exchanges
  → POSTs to Graphiti /messages endpoint
  → Graphiti extracts entities + relationships async
```

## Deploying Graphiti

See the [Graphiti GitHub](https://github.com/getzep/graphiti) for deployment options. Quick local setup:

```yaml
# docker-compose.yml
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

## License

MIT
