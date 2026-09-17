# Local memory-bank ingestion

The local system now runs through Docker Compose on Colima. Colima is the Linux
VM hosting Docker on macOS. Three services are deployed:

- `graph-memory-neo4j-1`: existing graph and Neo4j Browser.
- `graph-memory-worker-1`: read-only bank scanner and four Luna/medium consumers.
- `graph-memory-mcp-1`: five public tools over HTTP.

Deployment configuration lives at
`~/.local/share/graph-memory/deployment/compose.json`, outside the changing worktree.
It is private because it contains the MCP bearer token. The deployment pins the
validated Docker image and reuses the existing `graph-memory-dev-data` volume.
The old `graph-memory-dev` container is stopped with restart disabled.

The worker explicitly sets its home directory to `/home/memory` so Codex finds
its dedicated login mount when the container runs under the host user ID. The
first live start exposed this configuration issue; rejected jobs remain in the
queue for normal retry after the fix. At verification, Docker had completed an
original pending episode and repeated scans added no duplicate sources.

The worker mounts `~/.claude/projects` and `~/.codex/memories` read-only at their
original absolute paths. Claude intake is limited to its project `memory/*.md`
files; Codex intake includes its Markdown bank. Sources are scanned every 30
seconds. This does not imply that all historical raw conversations are imported.
Optional transcript mounts use the daemon's `--transcripts` argument.

The initial snapshot contained 805 files and 832 source episodes. The bank changes
while other sessions run; the daemon now picks up new and edited files. Check live
counts below rather than treating the initial snapshot as a permanent total.

## Inspect and operate

```sh
~/.local/share/graph-memory/bin/status
~/.local/share/graph-memory/bin/compose ps
~/.local/share/graph-memory/bin/compose logs --tail 50 worker
```

The deployment uses engine
`5cadbbca81d77be3e8d3ae8791f6708b1f2b6ec0213bdd86cc7ded3364a22b01`.
The diagnostics update passed 50 deterministic tests in Docker. Its model prompts
and extraction JSON schema match the preceding journal build exactly; no failed
bank sources were reproduced and no model evaluations were rerun for this logging
change. The preceding journal build passed 47 deterministic tests and 12/12 fixed
Luna model cases. Two earlier Claude A/B attempts passed the native arm but were blocked during graph
source extraction by exact-quote validation, before the MCP arm. This remains an
open extraction-quality limitation; that A/B was not rerun for the journal upgrade
and no new golden baseline was approved. The extractor, prompts, and current
temporal projection are unchanged. The graph store now journals knowledge writes.
Rejected candidates stay outside the fact graph and retry.

All jobs must be complete, with none processing, queued, or retrying, before
calling the bank fully ingested. A source may produce zero facts if it contains
no supported durable claims.

## Cutover evidence

The four macOS workers finished their active calls and were stopped with zero
active leases. Their LaunchAgent files were moved to
`~/.local/share/graph-memory/deployment/retired-launchagents/` so they cannot
restart alongside Docker at login.

Before the swap, the graph held 832 episodes, 19 completed episodes, and 188 facts.
The before/after audit checks that all original records remain, the 19 completed
episodes and 188 facts are unchanged, and newly queued source content is not a
duplicate of the original bank. It also records original pending episodes that
Docker completes. Evidence and the graph snapshot are in
`~/.local/share/graph-memory/deployment/`:

- `before-docker.json`: complete personal graph snapshot before the database swap.
- `macos-drain.json`: stopped worker identities and zero active leases.
- `docker-eval-summary.json`: passing deterministic/model evidence.
- `claude-ab-attempts.json`: the two blocked A/B attempts.
- `cutover-check.json`: preservation, duplicate, completion, and endpoint checks.

The prototype sandbox was separately backed up before the original fresh import:
`~/.local/share/graph-memory/backups/20260916T164612Z-prototype-graph.json`.
The current shared scope is `personal`.

## Failure diagnostics

On 2026-09-17 the worker's existing log stream gained safe reason codes, validation
locations, failure stages, elapsed time, and retry timing. Details are documented
in [Docker operations](docker.md). Episode error properties retain their previous
class-only format; operational diagnostics do not enter the knowledge journal.
Only future failures have the additional details.

Worker and MCP now pin image
`sha256:a9005f9f1eb653d72f7b2ba609a6664945ffe80291b4e6a3d215d34d60fe2160`.
The host CLI uses the same build. Two unfinished model attempts were cancelled
during drain and their normal cleanup released the jobs for retry. Journal change
112, containing 2,205 knowledge records, was verified before the switch. Private
deployment verification is saved in `deployment/diagnostics-deployment.json`.

## Journal activation

The journal started at **2026-09-16T19:11:31.729247Z** with change **0**. Existing
knowledge is preserved as that baseline; earlier changes cannot be reconstructed.
At activation the graph held 848 source episodes (49 completed), 369 entities, and
460 facts. Current answers and five representative historical queries matched.
Live HTTP recall/latest worked with and without the new `at_change` argument.

At initial journal activation, worker and MCP used image
`sha256:5d366fa3f10705a4744fd0c6b3b5697f2af8e6dd8c0582e4d0b545335c8c4c1a`.
The stable host CLI is installed from the same engine build. Four consumers
resumed the existing queue. One old model attempt was cancelled during shutdown;
its job remained durable and its lease was released before activation.

Private activation evidence is under `deployment/journal-validation/`:

- `before-journal.json`: graph records, relationships, and normalized knowledge.
- `baseline.json`: complete fixed model regression results.
- `replica-check.json`: isolated bank-copy parity and baseline timing.
- `activation.json`: baseline hash, preserved counts, and query parity.
- `live-check.json`: journal integrity, live MCP historical queries, and Browser.
- `compose-before.json`: previous private deployment configuration for reference.

Use the [history commands](history.md) to inspect changes or materialize an isolated
replay. All future knowledge writers must support the journal; restoring an old
image alone is not a compatible rollback after new journaled changes exist.

```sh
~/.local/share/graph-memory/bin/compose exec worker graph-memory --namespace personal history list --limit 20
~/.local/share/graph-memory/bin/compose exec worker graph-memory --namespace personal recall Atlas --at-change 0
```

## UI and MCP

Open http://127.0.0.1:17474/browser/ and connect to
`bolt://127.0.0.1:17687`, with authentication disabled for this local database.
The MCP endpoint is http://127.0.0.1:8765/mcp and requires the private bearer token.
Its exposed tools are recall, latest, ingest, retract, and merge.

[Graph queries](graph-browser.cypher) show the graph and queue status.
[Alfred / DailyAI / Unearth watch query](watch-alfred-dailyai.cypher) follows their
fact neighborhoods without assuming an ownership relationship.

This Colima instance's automatic SSH forwarding failed to add the new MCP port.
Port 8765 was added to Colima's existing managed SSH connection; the existing
Browser and Bolt forwards remained available. If the MCP address is unavailable
after restarting Colima, check its forwarding logs and the published Docker port.
No extra application server or macOS worker is needed.

## Lifecycle

```sh
# Gracefully finish active work and stop ingestion:
~/.local/share/graph-memory/bin/compose stop worker
# Resume the same queue:
~/.local/share/graph-memory/bin/compose up -d worker
```

Compose restarts containers unless explicitly stopped. The machine must be awake
and Colima/Docker running. Do not start the retired container against the same
Neo4j volume while the Compose database is running. Do not use `down -v` when
preserving data. Future engine changes should follow the eval/revision workflow.
