# @robertogongora/graphiti

Temporal knowledge graph plugin for [OpenClaw](https://github.com/openclaw/openclaw) using [Graphiti](https://github.com/getzep/graphiti) + Neo4j.

## What it does

- **Auto-recall**: Before each conversation turn, searches the knowledge graph for relevant facts based on the user's prompt and injects them into context
- **Auto-capture**: Before compaction or session reset, ingests the conversation into the knowledge graph for entity/relationship extraction
- **Native tools**: `graphiti_search` and `graphiti_ingest` available as agent tools
- **CLI**: `openclaw graphiti status|search|episodes`
- **Memory CLI bridge**: `openclaw memory status|index|search` (built-in file-based memory)
- **Slash command**: `/graphiti` for quick health check

## Requirements

- Graphiti server running (e.g., `http://localhost:8100`)
- Neo4j database (Graphiti manages this)
- OpenAI API key configured in Graphiti server

## Installation

### From npm

```bash
openclaw plugins install @robertogongora/graphiti
```

### From local directory

```bash
# Clone the repo
git clone https://github.com/RobertoGongora/openclaw-graphiti-plugin.git

# Add to OpenClaw config
openclaw config set plugins.load.paths '["/path/to/openclaw-graphiti-plugin"]'
```

### Auto-discovery

Place the plugin in one of OpenClaw's extension directories:

```bash
# Global (all workspaces)
ln -s /path/to/openclaw-graphiti-plugin ~/.openclaw/extensions/graphiti

# Workspace-local
ln -s /path/to/openclaw-graphiti-plugin .openclaw/extensions/graphiti
```

The plugin declares `openclaw.extensions` in `package.json`, so OpenClaw discovers it automatically.

## Memory slot configuration

This plugin declares `kind: "memory"`. OpenClaw's memory slot is **exclusive** -- only one memory plugin can be active at a time. When Graphiti is selected, `memory-core` (the default) is automatically disabled.

### Select Graphiti as memory plugin

```bash
openclaw config set plugins.slots.memory graphiti
```

Or in `~/.openclaw/settings.json`:

```json
{
  "plugins": {
    "slots": {
      "memory": "graphiti"
    },
    "entries": {
      "graphiti": {
        "config": {
          "url": "http://localhost:8100",
          "groupId": "core"
        }
      }
    }
  }
}
```

### Revert to default (memory-core)

```bash
openclaw config set plugins.slots.memory memory-core
```

### Disable all memory plugins

```bash
openclaw config set plugins.slots.memory none
```

## Status commands

With Graphiti active:

```bash
# Overall status -- shows "Memory: enabled (plugin graphiti)"
openclaw status

# Graphiti server health
openclaw graphiti status

# Built-in file-based memory (MEMORY.md index) -- still works via bridge
openclaw memory status
```

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `url` | string | `http://localhost:8100` | Graphiti server URL |
| `groupId` | string | `core` | Graph namespace |
| `autoRecall` | boolean | `true` | Inject relevant facts before each turn |
| `autoCapture` | boolean | `true` | Ingest conversations on compaction/reset |
| `recallMaxFacts` | number | `10` | Max facts to inject on recall |
| `minPromptLength` | number | `10` | Min prompt length to trigger recall |
| `apiKey` | string | *(none)* | Optional Bearer token for reverse proxy auth |

## How it works

### Auto-recall flow
```
User sends message
  -> before_agent_start fires
  -> Plugin searches Graphiti with user's prompt
  -> Top facts injected as <graphiti-context> block
  -> Agent sees relevant knowledge alongside the prompt
```

### Auto-capture flow
```
Session compacts or resets
  -> before_compaction / before_reset fires
  -> Plugin extracts user+assistant text
  -> Filters out short exchanges (<4 messages)
  -> POSTs to Graphiti /messages endpoint
  -> Graphiti extracts entities + relationships async
```

### Memory slot behavior
```
plugins.slots.memory = "graphiti"
  -> memory-core: disabled (auto, slot exclusivity)
  -> graphiti: enabled, selected
  -> openclaw status: "Memory: enabled (plugin graphiti)"
  -> openclaw memory status: works (bridge to built-in file memory)
  -> openclaw graphiti status: works (knowledge graph health)
```

## Migrating from memory-core

1. Install the Graphiti plugin (see Installation above)
2. Deploy a Graphiti server (see Deploying Graphiti below)
3. Set the memory slot:
   ```bash
   openclaw config set plugins.slots.memory graphiti
   ```
4. Configure the plugin:
   ```bash
   openclaw config set plugins.entries.graphiti.config.url http://localhost:8100
   ```
5. Verify:
   ```bash
   openclaw status          # Should show "Memory: enabled (plugin graphiti)"
   openclaw graphiti status # Should show healthy
   openclaw memory status   # Should still report file-based memory
   ```

Your existing `MEMORY.md` files and file-based memory index remain intact and
accessible via `openclaw memory status`. Graphiti adds a knowledge graph layer
on top without replacing the file system.

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
