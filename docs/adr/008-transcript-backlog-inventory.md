# ADR 008: Cached transcript backlog inventory

Status: Implemented and validated, 2026-09-17.

**Updated 2026-09-21.** The intake limits this inventory compensates for are now
settings (`MEMORY_INTAKE_QUEUE`, `MEMORY_INTAKE_FILES`), and the inventory service
has a container healthcheck. See [ingestion pipeline](../ingestion-pipeline.md#intake)
and [operations](../operations.md#health).

Saved-episode counts made a bounded staging queue look like a nearly completed
corpus import. The source-records worker intentionally limits intake, so those
counts cannot describe unstaged transcript content.

Add an independent `inventory` CLI process and Compose service. It reads the same
mounted sources and a namespace-scoped cursor snapshot, validates cursor prefixes,
and counts the remaining batches under the source parser's current rules. It
persists one operational `MemoryInventory` record per namespace. No LLM calls,
staging, extraction, cursor advances, or knowledge-journal events occur. The
existing workers can continue running unchanged.

`memory_status` reads this small cached record, never the source files. It returns
scan start/end times, age and staleness, files counted, unstaged chunks and episodes,
and bounded diagnostics without source paths or text. Inventory failures,
unavailable mounts, traversal failures, changed files, incomplete trailing records,
and rewritten history cannot silently become a complete zero-backlog result.
Missing inventory is explicitly unavailable. A partial count excludes unreadable
or invalid material and cannot establish completion. Concurrent staging can reduce
the actual backlog while the scan runs; new messages can increase it.

The inventory waits five minutes between scans. Keeping this separate from the
intake loop avoids delaying worker startup and allows rollout without restarting
active extraction jobs. The cost is one extra lightweight container and periodic
filesystem parsing. No new Python dependencies are introduced.

The first implementation covers source-records transcript intake only; markdown
import coverage remains unavailable. Test fixtures compare inventory predictions
with actual feed batches, and cover overlapping roots, missing/invalid/partial
files, rewritten or changing sources, namespace isolation, stale snapshots, and
read-only status behavior.

Validation (2026-09-17): all 105 Python tests passed against a disposable Neo4j
5.26 container; Ruff lint/format and wheel/source builds passed. Deployed
`graph-memory:inventory-v1` to both MCP services and a new transcript inventory
service. Both workers and both databases retained their container IDs and start
times. Direct MCP status calls succeeded for both namespaces. The first live
inventory counted 1,021 of 1,022 files, estimating 22,033 unstaged episodes and
reporting one parser validation gap (`content` too short). Personal/markdown
coverage correctly remained unavailable. The scan took about 30 seconds.
Private rollback configurations are under
`~/.local/share/graph-memory/deployment/inventory-rollout/`.
