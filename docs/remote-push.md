# Remote push: a Mac as transcript source for a graph on another host

The Mac keeps its session files. `graph-memory follow` runs on the Mac and
stages new messages straight into the Neo4j of a receiving host (here CT 160)
over Bolt on Tailscale. The receiver extracts facts and serves recall over MCP.
No transcript directory is mounted across machines.

```text
Mac                                        Receiver (CT 160)
~/.claude/projects ─┐                      ┌─ neo4j   Bolt on 127.0.0.1 and the Tailscale IP
~/.codex/sessions  ─┤                      ├─ worker  daemon without --transcripts: extraction only
graph-memory follow ┴── Bolt, Tailscale ──►┤
graph-memory inventory                     └─ mcp     127.0.0.1 only, as in the base file
```

- **The Mac owns intake.** It runs `follow`, and `inventory` for its own
  backlog. It never runs `daemon` on the shared namespace: a daemon releases
  every in-flight lease of the namespace when it starts, and one daemon owns the
  queue.
- **The receiver owns extraction.** Its worker runs
  `daemon --source-records` with no transcript roots, and holds the model login.
- **Labels are machine-qualified.** A source key is `LABEL:relative/path`. Two
  machines with a bare `claude` label would share keys, so on a shared graph a
  bare `claude`, `codex` or `cursor` label is refused. Name every root
  `HOST.CONSUMER=PATH`, for example `rob-mbp.claude=~/.claude/projects`.
  The labels keep each machine's cursors apart. They do not separate entities or
  recall: every machine's sessions extract into the same graph.

## Which setup

| Setup | Intake runs on | Transcript files | Use when |
| --- | --- | --- | --- |
| Push (this page) | Each Mac, `follow` | Stay on the Mac | The graph lives on another host |
| Mount | The graph host, `daemon --transcripts` | Mounted into the worker | The files are on the graph host (`compose.transcripts.yaml`) |
| MCP only | Nowhere automatic | Not read | Agents write explicit memories with `memory_ingest`; no transcripts |

Remote MCP for recall is independent of all three and is not set up here. See
[mcp.md](mcp.md#host-and-origin).

## Namespace

Push into the existing `transcripts` namespace, after loading the Mac's graph
onto the receiver (cutover step 4). Neo4j Community has one standard database per
instance ([ADR 004](adr/004-transcript-source-provenance.md)), so a second
namespace such as `mac-transcripts` would be a second corpus in the same
database, extracted from scratch. Choose it only if you want that.

## Security

The Bolt password is the `neo4j` admin password. Anyone who reaches the port
with it can read and change the whole database, including the source text of
every session. Neo4j Community has no narrower role to give the Mac. Three
controls stand in for one:

1. **Bind.** The overlay publishes Bolt on `127.0.0.1` and on `TAILSCALE_IP`
   only. Never on `0.0.0.0`, and never on the LAN address.
2. **Tailnet policy.** A new tailnet allows every device to reach every other.
   Replace that default with rules that let only the pushing Mac reach port 27687
   on the receiver. Policy rules name devices through `hosts` or tags:

   ```json
   {
     "hosts": {
       "rob-mbp": "100.x.y.z",
       "graph-memory": "100.123.33.78"
     },
     "acls": [
       {"action": "accept", "src": ["rob-mbp"], "dst": ["graph-memory:27687"]}
     ]
   }
   ```

   Keep your other rules; tailnet policy denies whatever no rule accepts.
3. **Password handling.** Keep the password out of files on the Mac (below).
   Rotating it is the procedure in [operations.md](operations.md#secrets), then
   the Keychain item on every Mac that pushes.

Check the bind on the receiver:

```sh
ss -ltn | grep 27687          # 127.0.0.1:27687 and 100.x.y.z:27687, nothing else
nc -zv 10.10.10.136 27687     # from the LAN: refused
```

## Receiver

Requires Docker Compose 2.24.4 or later. In the env file, besides the variables
of `compose.transcripts.yaml`:

```sh
TAILSCALE_IP=100.123.33.78        # tailscale ip -4 on the receiver
CLAUDE_SESSIONS_PATH=remote-push  # placeholders; see below
CODEX_SESSIONS_PATH=remote-push
```

Compose reads the base file's required variables before the overlay removes the
mounts that use them, so the two session paths need a value. A placeholder that
is not a path also makes the base file alone refuse to start on this host.

```sh
alias gmr='docker compose --env-file /path/outside/repo/transcripts.env \
  -f compose.transcripts.yaml -f compose.transcripts.tailscale.yaml'
gmr run --rm --no-deps --entrypoint codex worker login --device-auth
gmr up -d
```

The overlay:

- publishes Bolt on loopback and `TAILSCALE_IP`, and advertises the Tailscale
  address;
- runs the worker without transcript roots or session mounts;
- disables `inventory`, which counts files this host does not have. Do not name
  it in `gmr up`: naming a service enables its profile, and Compose then fails on
  the placeholder session paths. Upgrade with `gmr up -d neo4j`, then
  `gmr up -d mcp`, then `gmr up -d worker`;
- sets `MEMORY_FEED_REMOTE_RECEIVER=1`. Every other machine's feeds are
  unmatched here by design, so `MEMORY_FEED_ACCEPT_UNMATCHED=1` would stage their
  files again under new ids. With the flag set, a worker that receives it exits
  with code 2 instead of starting. A process with a remote `NEO4J_URI`, such as
  the Mac's `follow`, refuses it the same way.

Docker binds `TAILSCALE_IP` when the container starts. If tailscaled is not up
yet, Neo4j fails to start, and the worker and MCP wait for it. Start Docker after
tailscaled, for example with `After=tailscaled.service` in a systemd drop-in for
`docker.service`.

## Cutover from a Mac-local stack

Today the Mac runs `compose.transcripts.yaml` itself. The worker has fed the
Mac's sessions under the bare labels `claude` and `codex`, from `/sessions/...`
inside the container. In order:

1. **Secrets.** Generate `NEO4J_PASSWORD` and `TRANSCRIPT_MCP_TOKEN` for the
   receiver's env file with `openssl rand -hex 32`. Users and passwords live in
   Neo4j's `system` database, which the dump below does not carry: the receiver
   keeps the password its volume got on its first authenticated start.
2. **Drain and stop the Mac stack.** Wait for `memory_status` to report no
   pending episodes, then stop every writer. From here nothing may write to the
   Mac's graph:

   ```sh
   gm stop worker inventory mcp
   gm stop -t 120 neo4j
   ```

3. **Dump on the Mac** with the procedure in
   [operations.md](operations.md#backup), and note the image tag that wrote it.
4. **Load on the receiver** into the stopped stack's volume, with the same Neo4j
   image and the same `GRAPH_MEMORY_TAG`. Do this before anything pushes. A
   receiver that accepts pushes into an empty graph and is overwritten by a load
   later loses what it received, and one that is loaded after pushes began holds
   two feeds for the same files.
5. **Relabel** on the receiver, before any Mac follows. This renames the keys
   and nothing else: feed ids, sessions and cursors stay, so no file is read
   again. Look at the counts first, then apply:

   ```sh
   gmr up -d neo4j
   gmr run --rm --no-deps worker feeds relabel --from claude --to rob-mbp.claude
   gmr run --rm --no-deps worker feeds relabel --from codex --to rob-mbp.codex
   # feeds = the Mac's feeds under that label, conflicts = 0; then again with --apply
   ```

   `feeds stamp` does not do this: it names only feeds that have no key, and the
   Mac's worker keyed every feed on its first scan. Without the relabel the Mac's
   first `follow` finds no feed under the new label. If the stored paths fall
   under the Mac's roots, intake and inventory block until ownership is resolved.
   Paths that also changed cannot identify a missed relabel: always complete this
   step and check the counts before continuing. Remote path and filename matching
   never crosses labels. `--apply` exits 1 for conflicts **or zero matching feeds**;
   check the source label and resolve conflicts before continuing. A dry run may
   exit 0 with zero feeds, for example if that consumer has never been used.
6. **Start the receiver**: `gmr up -d`. The worker watches no transcripts, so it
   logs no `transcript_scan` events; it only works the queue.
7. **Match versions.** Install the Mac's CLI from the same commit as the
   receiver's image, and compare `graph-memory --version` on both. The engine
   hash must be equal, or episodes staged by the Mac are extracted under
   different code than the ones staged before. Upgrade both together.
8. **Inventory once from the Mac.** Set up the Keychain and wrapper in
   [Mac forwarder](#mac-forwarder) first, then use its inventory mode. Both modes
   connect to the same receiver with the same credentials and roots:

   ```sh
   ~/.local/bin/graph-memory-push inventory
   ```

   Expect `"state": "available"` and zero `gaps.identity_refused`. The unstaged
   counts should be what the Mac wrote after step 2. `identity_blocked` means
   either unkeyed legacy feeds need explicit stamping with their known owning
   machine's label and stored paths, or the new label overlaps another label's
   stored paths. Follow the reported action: stamp legacy feeds or complete step 5.
   Do not acknowledge the cutover as a new source, or use
   `MEMORY_FEED_ACCEPT_UNMATCHED`.
9. **Start `follow`** on the Mac (next section).

## Mac forwarder

Install the CLI from the same commit as the receiver:

```sh
uv tool install --from /path/to/openclaw-graphiti-plugin graph-memory
graph-memory --version
```

Store the Bolt password in the login Keychain. With `-w` last, `security`
prompts for it, so the value never reaches shell history:

```sh
security add-generic-password -a "$USER" -s graph-memory-transcripts -w
```

A wrapper reads it at start and hands it to the process environment only.
Save it as `~/.local/bin/graph-memory-push` and make it executable with
`chmod 700 ~/.local/bin/graph-memory-push`:

```sh
#!/bin/sh
# ~/.local/bin/graph-memory-push
set -eu
NEO4J_PASSWORD=$(security find-generic-password -a "$USER" -s graph-memory-transcripts -w)
export NEO4J_PASSWORD
export NEO4J_URI=bolt://graph-memory.taild00569.ts.net:27687
export MEMORY_FEED_REMOTE_RECEIVER=1
case "${1:-follow}" in
  inventory)
    exec graph-memory --namespace transcripts inventory --once \
      --transcripts "rob-mbp.claude=$HOME/.claude/projects" \
      --transcripts "rob-mbp.codex=$HOME/.codex/sessions"
    ;;
  follow|once)
    mode=${1:-follow}
    set --
    if [ "$mode" = once ]; then set -- --once; fi
    exec graph-memory --namespace transcripts follow --source-records "$@" \
      "rob-mbp.claude=$HOME/.claude/projects" \
      "rob-mbp.codex=$HOME/.codex/sessions"
    ;;
  *)
    printf 'usage: graph-memory-push [inventory|follow|once]\n' >&2
    exit 2
    ;;
esac
```

The guards against bare labels and `MEMORY_FEED_ACCEPT_UNMATCHED` apply when
`NEO4J_URI` names another host, whatever the scheme. A URI that reaches the
receiver through this host, such as an SSH port forward to `127.0.0.1`, looks
local. `MEMORY_FEED_REMOTE_RECEIVER=1` in the wrapper keeps the guards on in
that case too.

Run it under launchd:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- ~/Library/LaunchAgents/com.graph-memory.push.plist -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.graph-memory.push</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/rob/.local/bin/graph-memory-push</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/rob/.local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>/Users/rob/Library/Logs/graph-memory-push.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/rob/Library/Logs/graph-memory-push.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.graph-memory.push.plist
```

The Keychain may ask once whether `security` may read the item; allow it.

Before `follow` connects it refuses, with exit code 2: a bare label, a
`LABEL=PATH` whose PATH does not exist, and `MEMORY_FEED_ACCEPT_UNMATCHED=1`.
launchd restarts those too, so check the log after a change.

## Sleep, wake and catch-up

Nothing is queued on the Mac. Catch-up is a rescan of the files:

- When the connection drops, `follow` exits (code 69 while the receiver cannot
  be reached) and launchd starts a new process after `ThrottleInterval`, until
  the receiver answers.
- The new process reads the receiver's caught-up stamps (`caught_up_mtime_ns`,
  `caught_up_size`). Files unchanged since are skipped without being read; a
  changed file resumes at its stored `message_count`, and its `prefix_hash` must
  still match.
- Messages written while the Mac was offline are staged on the first scan after
  it reconnects, once. Keep the session files until they are staged.

## Several Macs

Give each Mac its own host prefix (`rob-mbp.claude`, `rob-mini.claude`). Each
follows only its own roots. Already keyed feeds from other machines do not block
an established label. Unkeyed legacy feeds block intake on the shared graph even
when their paths match: stop followers and explicitly run `feeds stamp` with the
known owning machine's qualified label and stored path prefix. Shared followers
never auto-stamp. `feed --source-records` is refused before connecting on a shared
graph because it creates no key; use `follow LABEL=PATH --source-records --once`.

Paths, filenames and caught-up stamps are scoped to the root label on a shared
graph. Two Macs can have identical absolute paths and filenames without taking
over each other's feeds. Moving a session to another machine or changing its
label requires an explicit `feeds relabel` with both followers stopped.

A label with no keyed feeds needs a one-time acknowledgement if another label
has stored paths under its root. A new Mac with the same home directory and an
accidentally renamed existing Mac are indistinguishable by paths. For an existing
source, use `feeds relabel`; never acknowledge it as new. A new Mac with different,
nonoverlapping paths needs no acknowledgement.

For a genuinely new Mac, first edit its wrapper to use that Mac's qualified
labels. Then run inventory and one scan with those exact labels acknowledged:

```sh
MEMORY_FEED_NEW_SOURCE_LABELS=rob-mini.claude,rob-mini.codex \
  ~/.local/bin/graph-memory-push inventory
MEMORY_FEED_NEW_SOURCE_LABELS=rob-mini.claude,rob-mini.codex \
  ~/.local/bin/graph-memory-push once
# Confirm inventory works without acknowledgement before starting launchd:
~/.local/bin/graph-memory-push inventory
```

The wrapper inherits this transient environment value. It is a comma-separated
list of exact labels (surrounding spaces allowed); wildcards, empty entries and
bare consumer labels are refused. Do not add it to the wrapper, `.env`, or the
launchd plist. Each acknowledged label must create at least one feed before it
can restart without acknowledgement; if the queue is full or no file exists yet,
repeat the one-time scan when it can stage work. Inventory does not register a
label. The acknowledgement only permits new feeds under the listed labels; it
never adopts another label's feeds and never bypasses unkeyed legacy feeds.

Push each session from one Mac only. A session copied or synced to another Mac
under a different label becomes a separate feed; content is not deduplicated
across machines.

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `follow` exits 69 | Receiver unreachable, or wrong password | `tailscale status`, `nc -zv graph-memory.taild00569.ts.net 27687`, the tailnet policy, the Keychain item |
| `follow` exits 2, "machine-qualified" | A bare label with a remote `NEO4J_URI` | Name the root `HOST.CONSUMER=PATH` |
| `feed_identity_blocked` / inventory `identity_blocked`, `unkeyed` | Shared graph contains feeds with no known owning label | Stop followers; `feeds stamp --root LABEL=STORED_PREFIX` with the known owner's qualified label; see [operations.md](operations.md#feed-identity) |
| `feed_identity_blocked` / inventory `identity_blocked`, `new_source_labels` | A label has no feeds, but its root overlaps paths stored under other labels | Existing source: `feeds relabel` (cutover step 5), with followers stopped. Genuinely new Mac: transient acknowledgement as above |
| `feed_identity_refused` | A known file name under the same label appears copied beside its original | Keep one authoritative source; inspect the reported known feed and key |
| Worker exits 2 at start | `MEMORY_FEED_ACCEPT_UNMATCHED=1` reached a receiver | Remove it from the env file |
| Episodes stay pending | Worker down, model login expired, or provider outage | `gmr ps worker`, `gmr logs worker`; see [operations.md](operations.md#provider-outage) |
