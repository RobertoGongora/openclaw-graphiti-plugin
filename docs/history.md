# Memory history and replay

The current graph answers normal questions. Its journal records changes in the
separate `audit:<namespace>` scope. `known_at` and `at_change` on the existing
`memory_recall` and `memory_latest` tools reconstruct earlier knowledge.

| Input | Meaning |
| --- | --- |
| `as_of` | What was true at this event/observation time? |
| `known_at` | Using only what the graph knew at this recorded time |
| `at_change` | Using the exact numbered journal state, instead of `known_at` |

For example, a fact learned today about last Tuesday can appear in an `as_of`
Tuesday query today, but cannot appear in what the graph knew yesterday.
Historical results include the selected change number and journal coverage start.
Dates before coverage started are rejected instead of returning an invented past.
The initial snapshot preserves existing knowledge; only subsequent changes have
an exact recorded sequence. A missing event date remains uncertain.

The following commands are engine operations, not additional MCP tools. Set
`NEO4J_URI` for your intended database first. In the deployed Docker stack, prefix
commands with `~/.local/share/graph-memory/bin/compose exec worker`.

```sh
# Inspect numbered changes (paginated, without returning private source payloads).
graph-memory --namespace personal history list --limit 20
# Routine check: stream the live graph and compare it with the journal head.
# Seconds and constant memory. Finds untracked writes.
graph-memory --namespace personal history verify-live
# Full audit: the whole hash chain, then the live graph against its reconstruction.
# Holds the namespace lock and reads every event: run it in a maintenance window.
graph-memory --namespace personal history verify
# Embed the current state so historical reads replay from here (maintenance action).
graph-memory --namespace personal history checkpoint
# Query the graph as it stood after a particular change.
graph-memory --namespace personal recall Atlas --at-change 10
# Combine knowledge-time and event-time cutoffs.
graph-memory --namespace personal recall Atlas \
  --known-at 2026-09-20T12:00:00Z --as-of 2026-09-12T12:00:00Z
# Create an isolated, read-only historical view. No model calls or live rewrites.
graph-memory --namespace personal history replay --at-change 10 --target replay:atlas-review
```

Use change numbers and dates actually present in your journal; the examples above
are illustrative. `history snapshot` returns full private evidence, while `history
list` returns only change metadata. `history init` explicitly establishes a baseline
for an existing namespace and is idempotent.

## How the journal is written

A stage, commit, retract or merge journals only the nodes it changes. The state
hash is a sum of per-node hashes that does not depend on order, so such a write
updates it without reading the rest of the graph. Dream and revision writes still
capture the whole namespace.

Only a baseline and an explicit `history checkpoint` embed the full state. There
are no periodic snapshots. A historical read starts at the latest checkpoint at
or before the requested change and replays the differences, checking every hash
on the way. After a long run of changes, a checkpoint shortens those reads.
Snapshots that earlier versions embedded every 100 changes remain readable.

A write that touches a few nodes cannot notice a change made elsewhere outside
the journal. Untracked writes are detected by `history verify-live`, by
`history verify`, by `history checkpoint`, by any full-capture write, and by the
sampled audit: `MEMORY_JOURNAL_AUDIT=N` compares the streamed live graph with the
journal head on every Nth write (`1` every write, `0` off). `verify-live` is the
routine check. `verify` stays the full audit for a maintenance window.

After an untracked write, `history checkpoint --accept-live` records the live
graph as the new truth. Use it only once the cause is known; see the
[runbook](operations.md#journal-maintenance). The design is described in
[ingestion pipeline](ingestion-pipeline.md#journal-write-path).

In Neo4j Browser, view the history metadata:

```cypher
MATCH (e:MemoryChange {scope:'personal'})
RETURN e.sequence, e.kind, e.recorded_at
ORDER BY e.sequence;
```

To visualize a materialized replay without mixing it with current memory:

```cypher
MATCH p=(n {namespace:'replay:atlas-review'})-[r]->(m)
WHERE m.namespace = n.namespace
RETURN p LIMIT 300;
```

Ordinary queries should keep their namespace filter. An unrestricted `MATCH (n)`
will include audit entries and every replay namespace too. Journal entries have
no graph relationships, so relationship-only views do not display them.

A replay materializes stored decisions, rather than extracting the original text
again. Comparing new extraction behavior uses [validated revisions](revisions.md).
Read-only replay protects its historical view from ingest, corrections, merges,
and dream publication. Source file content and embedded dream snapshots retain
original provenance. See [ADR-002](adr/002-memory-change-journal.md) for boundaries
and implementation costs.

## Upgrading an existing installation

Validate the candidate engine against an isolated database first. Gracefully stop
all writers, save a graph backup, and run `history init` with the new engine before
starting the upgraded worker and MCP service. Preserve the database volume and
source mount paths so the worker resumes the same queue.

Once a namespace has a journal, every knowledge writer must support it. Editing
knowledge properties directly in Cypher diverges from recorded history. The next
`verify-live`, verify, checkpoint, full-capture write or sampled audit detects it, and journaled
writes that detect it stop until the discrepancy is resolved.

Journals written before the per-node state hash migrate on the first write by the
current code. From then on an older engine fails its own state check and refuses
to write to that namespace. Stop every old writer before the first new write, and
treat the migration as irreversible without the backup. Keep the original backup and
validated image identifiers with the deployment record. Retrying an existing
source does not create a new journal entry unless its knowledge state changes.
