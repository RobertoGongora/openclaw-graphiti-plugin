# Local memory-bank ingestion

This is the original markdown benchmark deployment. A separate
[transcript source stack](transcript-sources.md) now runs on Browser port 27474,
Bolt 27687 and MCP 8766. It does not replace or modify these workers.

The local system now runs through Docker Compose on Colima. Colima is the Linux
VM hosting Docker on macOS. Three services are deployed:

- `graph-memory-neo4j-1`: existing graph and Neo4j Browser.
- `graph-memory-worker-1`: read-only bank scanner and two Terra/low consumers
  (concurrency reduced from twelve on 2026-09-17, fast mode off).
- `graph-memory-mcp-1`: seven public tools over HTTP, with
  [compact recall and evidence on demand](compact-recall.md).

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

The ingestion worker and host CLI use engine
`78e20fcdf6ffad4bc60785708d3d56d8d8fe6120a6596516d01c185e7fe41582`.
Queued retries now retain bounded rejection feedback across restarts; see
[durable retry behavior](docker.md#durable-rejection-feedback). The final build
passed 54 deterministic tests, six fixed Terra/low scenarios, and a real-model
persisted-feedback canary. MCP discovery/recall and journal integrity were verified
after deployment. The worker drained without cancelling any model calls.

The diagnostics-only deployment described below preceded this update.
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

## Ingestion concurrency

On 2026-09-17 Rob requested tripling the live consumers from four to twelve.
This is a deployment configuration change (`daemon --workers 12`), retaining
the worker image, Terra/low model, prompts, and validation rules. The portable
Compose template remains configurable through `MEMORY_WORKERS` (default four).

Before the change, 211 of 938 episodes were complete. Recent throughput was
45 completions in one hour and 112 in two hours. Twelve consumers are not a
guarantee of three times the throughput; measure completed episodes after the
change, including retry overhead. Recall changes and baseline experiments stay
deferred until ingestion and outstanding failures are resolved.

The old worker drained to zero active leases with 214 complete episodes. After
restart, all 214 extraction hashes were preserved, twelve episodes held active
leases, and the first scan staged zero duplicate episodes (820 files scanned,
849 existing source chunks recognized, no scan failures).
Consumers 6, 9, 10, and 11 then completed episodes, bringing the total to 218
while twelve jobs remained active. One transient Neo4j deadlock triggered the
driver's automatic transaction retry; completions continued. This short smoke
check establishes resumed processing, not sustained throughput or a new ETA.

Private configuration backup and restart evidence are stored under
`~/.local/share/graph-memory/deployment/workers-12/`. The configuration backup
contains credentials and must not be committed.

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

At the diagnostics rollout, worker and MCP pinned image
`sha256:a9005f9f1eb653d72f7b2ba609a6664945ffe80291b4e6a3d215d34d60fe2160`.
The host CLI then used the same build. Two unfinished model attempts were cancelled
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

## Retry-feedback deployment — 2026-09-17

At this rollout, worker, MCP, and host CLI used the retry-feedback engine above. Worker and MCP
pinned `sha256:5f227380263f5d3916a2d6219825f7c258f8c526f0534096d174c19e52941515`.
The worker remains Terra low with fast mode off. Before and after the upgrade,
journal change 152 verified with the same hash and all 2,524 knowledge records.
No source, fact, or historical record was re-extracted or repaired during rollout.
Evidence is in [the sanitized validation report](../evals/baselines/retry-feedback.json)
and the private deployment's `retry-feedback/` folder. No golden expectations changed.

## Graph-render tool — 2026-09-17

The MCP service now exposes `memory_render` and includes Graphviz. Its image is
`sha256:a3c9a8eb3163c61b2ef16fa5473abaec5d20f0ef16d38d0b19502c1401c3298e`,
with engine `ffd43cb63037cc715da34dbbb642f50209850f69596dbc35a555473dabed9dd9`.
Worker and host CLI remain on the validated retry-feedback build. This read-only
addition required no worker restart and preserves existing queued retry context.
The extraction and dream prompts/schemas match exactly; model evals were not rerun.
All 67 deterministic checks passed, including actual PNG responses and graph
preservation. Live HTTP calls rendered 2,651 nodes / 2,916 relationships without
truncation and a focused 39-node graph. Journal change 163 verified afterward.
See [rendering](rendering.md) and [validation evidence](../evals/baselines/render-validation.json).


## Allocation toward transcript ingestion

On 2026-09-17 Rob requested more transcript consumers and fewer markdown-bank
consumers. The local allocation changed from 2 transcript / 12 markdown to
12 transcript / 2 markdown, retaining fourteen total consumers. This changes only
Docker worker command arguments. Model, effort, image and ingestion semantics
remain pinned independently for each deployment.

Both worker services drain active jobs before recreation. MCP and Neo4j services
continue running. Completed episode IDs and extraction hashes are compared before
and after the restart, and the transcript queue is checked for resumed work.
Private configuration backups and evidence live under
`~/.local/share/graph-memory/deployment/workers-12-transcripts-2-bank/`.

Verification after restart: both services were running with their requested
`--workers` values, unchanged images, and zero active leases at the drain boundary.
All 928 previously completed markdown episodes and 406 previously completed
transcript episodes retained their extraction hashes. Transcript completions had
reached 437 at the restart checkpoint. Configured consumer count is a ceiling;
active model calls depend on ready jobs and retry backoff.
