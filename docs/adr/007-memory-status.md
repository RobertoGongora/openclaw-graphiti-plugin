# ADR 007: Read-only memory status

Status: Implemented and validated, 2026-09-17.

**Updated 2026-09-21.** "Leases do not prove worker liveness" still holds, but
liveness is no longer unknown: the daemon writes a heartbeat that `memory_status`
returns under `workers`, with the provider state, and that the container
healthcheck reads. Status also reports quarantined episodes and cached
extractions. See [operations](../operations.md#health).

Recall freshness reports do not provide a whole-namespace operational view, and
persisted episode status does not distinguish queued work from active extraction.
Workers claim episodes using expiring leases while retaining pending/failed status.

Expose `memory_status` in the public MCP catalog and retrieval-only servers, using
the existing server-owned namespace contract. Return status totals, lease-derived
processing and queue counts, retry delays, expired leases, bounded active episode
summaries, latest ingestion/completion, oldest incomplete ingestion, and counts of
unmerged entities and unretracted facts. Use database aggregation and explicitly
project episode metadata rather than fetching transcript or extraction payloads.
The tool performs no mutations or LLM calls.

Eligibility mirrors `worker_tick`: incomplete episodes with an unexpired lease
are active; otherwise retry time separates queued and delayed episodes. Expired
leases overlap the latter categories. Completion excludes an episode even if its
lease remains. Counts by persisted status are independent of queue categories.
Leases do not prove worker liveness. Scanner health, unseen sources, and direct
extraction outside the leased worker queue cannot be inferred. Concurrent writes
can change the data between queries; this is an operational view, not a snapshot.

Validation: 100 Python tests passed against a disposable Neo4j 5.26 container,
including empty namespace/no writes, read-only MCP dispatch, scope rejection and
isolation, lease/retry boundary times, completed residual leases, bounded active
summaries, latest/oldest selection, and exclusion of source payloads/errors.
All 284 legacy TypeScript tests, Ruff lint/format checks, and Python package builds
passed. Both local MCP services were deployed with `graph-memory:status-edf8b6cc89f4`.
Live HTTP catalogs exposed nine tools, and scoped `memory_status` calls succeeded
on ports 8765 (personal) and 8766 (transcripts). Workers and databases retained
their container IDs and start times. Private deployment backups and verification
are under `~/.local/share/graph-memory/deployment/status-rollout/`.
