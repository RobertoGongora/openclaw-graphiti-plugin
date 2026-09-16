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
# Verify that current knowledge equals the reconstructed journal head.
graph-memory --namespace personal history verify
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

Once a namespace has a journal, every knowledge writer must support it. Running
an older writer or editing knowledge properties directly in Cypher can diverge
from recorded history; subsequent journaled writes stop until that discrepancy is
resolved. `history verify` detects this condition. Keep the original backup and
validated image identifiers with the deployment record. Retrying an existing
source does not create a new journal entry unless its knowledge state changes.
