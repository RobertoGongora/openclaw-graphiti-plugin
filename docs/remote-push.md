# Remote Mac transcript source: Tailscale push

This document describes using a Mac as a remote transcript source for Graph Memory
on a separate CT (container host) via Tailscale. The Mac runs a local forwarder
that pushes transcripts to the CT's Neo4j over Bolt; MCP recall stays on the CT.

## Architecture overview

```text
┌─────────────────────────────────────────┐    ┌──────────────────────────────────────┐
│           Mac (rob-mbp)                 │    │           CT (ct-160)                │
│                                         │    │                                      │
│  ~/.claude/projects/                    │    │  ┌──────────────────────────────┐   │
│  ~/.codex/sessions/                     │    │  │         Neo4j                 │   │
│         │                               │    │  │   (Bolt on Tailscale only)    │   │
│         ▼                               │    │  └──────────────────────────────┘   │
│  graph-memory follow                    │    │               ▲                      │
│  (with machine-qualified labels)        │────────────────────┘                      │
│         │                               │    │  Bolt over Tailscale                │
│         │                               │    │                                      │
│  NEO4J_URI=bolt://ct-160:27687          │    │  ┌──────────────────────────────┐   │
│                                         │    │  │         MCP Server            │   │
│                                         │    │  │   (recall over Tailscale)     │   │
│                                         │    │  └──────────────────────────────┘   │
└─────────────────────────────────────────┘    └──────────────────────────────────────┘
```

**Key design decisions:**

1. **Mac owns intake**: The Mac runs `graph-memory follow` with machine-qualified
   labels (e.g. `rob-mbp.claude`). It scans local transcript directories and pushes
   new messages to the CT's Neo4j via remote Bolt.

2. **CT owns extraction**: The CT runs Neo4j, the extraction worker (with model
   credentials), and the MCP server. It processes the queued episodes that the Mac
   pushed and serves recall.

3. **No Mac mounts on CT**: Transcripts are pushed over Bolt, not shared via
   NFS/SSHFS. This avoids network filesystem latency and failure modes.

4. **Feed identity**: Machine-qualified labels prevent collisions when multiple
   machines push to the same database. The Mac uses `rob-mbp.claude` instead of
   bare `claude`.

5. **Reconnect safety**: Feed cursors and prefix hashes survive Mac sleep/reboot.
   On reconnect, the forwarder resumes from the stored cursor without replaying
   committed messages.

## Security hardening

### Bind Bolt to Tailscale only

The CT's Neo4j Bolt port must be bound to the Tailscale interface, not to all
interfaces or the LAN. Use `compose.transcripts.tailscale.yaml` as an overlay:

```sh
# On the CT
export TAILSCALE_IP=$(tailscale ip -4)
docker compose -f compose.transcripts.yaml -f compose.transcripts.tailscale.yaml up -d
```

Or set `TAILSCALE_IP` in your env file.

### Tailscale ACL rules

In your Tailscale admin console, add ACL rules to allow only your Mac to reach
the CT's Bolt and MCP ports:

```json
{
  "acls": [
    {
      "action": "accept",
      "src": ["rob-mbp"],
      "dst": ["ct-160:27687", "ct-160:8766"]
    }
  ]
}
```

Replace `rob-mbp` and `ct-160` with your actual Tailscale machine names.

### Deny LAN access

Ensure the CT's Bolt port is not exposed on the LAN. With the Tailscale overlay:
- Bolt binds to both `127.0.0.1:27687` (for CT-local tooling) and `${TAILSCALE_IP}:27687` (for remote push)
- Bolt is NOT bound to `0.0.0.0` or LAN interfaces

Verify with:

```sh
# Should show 127.0.0.1 and Tailscale IP, not 0.0.0.0
docker inspect graph-memory-transcripts-neo4j-1 | grep -A5 PortBindings
```

### Neo4j password

The Bolt connection uses the same `NEO4J_PASSWORD` as the CT's local services.
On the Mac, export it or store it in a keychain/secrets manager:

```sh
# Mac forwarder environment
export NEO4J_URI=bolt://ct-160:27687
export NEO4J_PASSWORD=...  # Same as CT's NEO4J_PASSWORD
```

Do not commit the password to version control.

## Mac forwarder setup

### Installation

Install the graph-memory package on the Mac:

```sh
uv pip install /path/to/graph-memory
# Or
pip install /path/to/graph-memory
```

### Running the forwarder

Run the forwarder with machine-qualified labels:

```sh
export NEO4J_URI=bolt://ct-160:27687
export NEO4J_PASSWORD=...

graph-memory --namespace transcripts follow \
  --source-records \
  rob-mbp.claude=~/.claude/projects \
  rob-mbp.codex=~/.codex/sessions
```

For continuous operation, run under a service supervisor (launchd on macOS):

```xml
<!-- ~/Library/LaunchAgents/com.graph-memory.forwarder.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.graph-memory.forwarder</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/graph-memory</string>
    <string>--namespace</string>
    <string>transcripts</string>
    <string>follow</string>
    <string>--source-records</string>
    <string>rob-mbp.claude=/Users/you/.claude/projects</string>
    <string>rob-mbp.codex=/Users/you/.codex/sessions</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NEO4J_URI</key>
    <string>bolt://ct-160:27687</string>
    <key>NEO4J_PASSWORD</key>
    <string>REPLACE_WITH_ACTUAL_PASSWORD</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/graph-memory-forwarder.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/graph-memory-forwarder.err</string>
</dict>
</plist>
```

Load it:

```sh
launchctl load ~/Library/LaunchAgents/com.graph-memory.forwarder.plist
```

### Sleep and reconnect behavior

When the Mac sleeps:
1. The forwarder's Bolt connection drops
2. On wake, the Neo4j driver reconnects automatically
3. The forwarder resumes from its in-memory cursor

If the forwarder process restarts:
1. It loads caught-up stamps from Neo4j (`MemoryFeed.caught_up_mtime_ns`, `.caught_up_size`)
2. Files unchanged since the last scan are skipped without parsing
3. Changed files resume from their stored cursor (`MemoryFeed.message_count`, `.prefix_hash`)

No durable push queue on the Mac: it re-reads files on reconnect. Keep source
transcripts available until extraction completes.

## CT setup

### First-time installation

The CT runs Neo4j, the worker, and the MCP server. It does NOT mount Mac transcript
directories.

1. Copy `.env.example` to `.env` and configure:
   ```sh
   NEO4J_PASSWORD=...  # Strong random password
   TRANSCRIPT_MCP_TOKEN=...  # Bearer token for MCP
   GRAPH_MEMORY_TAG=...  # Image tag
   TAILSCALE_IP=...  # CT's Tailscale IPv4
   TRANSCRIPT_AUTH_PATH=/path/to/codex/auth  # For model credentials
   ```

2. Start with the Tailscale overlay:
   ```sh
   docker compose -f compose.transcripts.yaml -f compose.transcripts.tailscale.yaml up -d
   ```

3. Authenticate the worker for model calls:
   ```sh
   docker compose -f compose.transcripts.yaml run --rm --no-deps --entrypoint codex worker login --device-auth
   ```

### Cutover from local mounts

If migrating from a local-mount setup to remote push:

1. Stop the old worker: `docker compose -f compose.transcripts.yaml stop worker`
2. Verify all episodes are complete: check `memory_status.processing.pending = 0`
3. Remove transcript mounts from the worker (use the Tailscale overlay)
4. Stamp existing feeds with machine-qualified labels:
   ```sh
   docker compose -f compose.transcripts.yaml exec worker \
     graph-memory --namespace transcripts feeds stamp \
     --root rob-mbp.claude=/sessions/claude \
     --root rob-mbp.codex=/sessions/codex
   ```
5. Start the forwarder on the Mac
6. Restart the CT worker with the Tailscale overlay

### MEMORY_FEED_ACCEPT_UNMATCHED

**Never set `MEMORY_FEED_ACCEPT_UNMATCHED=1` on the CT in remote mode.**

This variable bypasses feed identity checks and would cause duplicate ingestion
when the Mac forwarder reconnects with different labels or roots.

The Tailscale overlay enforces this with two mechanisms:
1. **`MEMORY_FEED_REMOTE_RECEIVER=1`**: When set, `MEMORY_FEED_ACCEPT_UNMATCHED=1`
   becomes a hard error (RuntimeError at startup). This is necessary because the
   CT worker uses `bolt://neo4j` internally, so `is_remote_bolt()` returns false.
2. **`MEMORY_FEED_ACCEPT_UNMATCHED=''`**: Defense in depth; explicitly unset.

## Deployment profiles

### Push only (recommended)

- Mac runs the forwarder, pushing to CT Neo4j over Tailscale
- CT runs worker (extraction) and MCP (recall)
- No NFS/SSHFS mounts
- Simplest failure mode: Mac offline = no new episodes, CT continues recall

### Mount only (existing setup)

- CT mounts Mac transcript directories over NFS/SSHFS
- CT runs scanner, worker, and MCP
- Mac does nothing special
- Failure mode: mount failure = scanner errors, potential data loss

### MCP-only (minimal)

- Mac calls `memory_ingest` MCP tool for explicit ingestion
- No automatic scanning
- CT runs worker and MCP
- Useful when automatic intake is not needed

## Secrets management

| Secret | Lives on | Notes |
| --- | --- | --- |
| `NEO4J_PASSWORD` | CT and Mac | Same value; Mac needs it for remote Bolt |
| `TRANSCRIPT_MCP_TOKEN` | CT | Mac uses MCP only if recall is needed there |
| Model credentials (Codex login) | CT | Worker needs them; Mac forwarder does not |

**Never commit secrets to git.** Use env files outside the repository, keychain,
or a secrets manager.

## Troubleshooting

### Mac forwarder cannot connect

1. Verify Tailscale is connected: `tailscale status`
2. Check the CT is reachable: `ping ct-160` (use your machine name)
3. Verify port is listening: `nc -zv ct-160 27687`
4. Check Tailscale ACLs allow the connection
5. Verify `NEO4J_PASSWORD` matches the CT's

### Feeds show as blocked

If the forwarder reports `feed_identity_blocked`:
1. Older feeds are stored under paths the current roots cannot name
2. Stamp them with the correct roots (see cutover steps above)
3. Do NOT set `MEMORY_FEED_ACCEPT_UNMATCHED=1`

### Episodes stuck pending

1. Check the CT worker is running: `docker compose -f compose.transcripts.yaml ps worker`
2. Check for provider outage: look for `provider_unavailable` in worker logs
3. Check model credentials: `docker compose -f compose.transcripts.yaml logs worker | grep auth`

### Duplicate facts after reconnect

This indicates `MEMORY_FEED_ACCEPT_UNMATCHED=1` was set incorrectly, or feed
labels changed between runs. Check feed identity and ensure machine-qualified
labels are consistent.
