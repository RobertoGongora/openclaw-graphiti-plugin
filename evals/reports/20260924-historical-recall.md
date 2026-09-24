# Broad historical recall efficiency experiment

Issue [#191](https://github.com/RobertoGongora/openclaw-graphiti-plugin/issues/191).
The recall projection reduced median peak process memory **50.7%** on the frozen
historical profile workload. All **51 paired responses** matched byte for byte:
42 from the repeated original audit and nine additional regression requests.
The candidate remains opt-in in the eval runner; ordinary service routing is
unchanged and nothing was deployed.

## Measurements

Three fresh containers per mode, each executing the original seven requests
twice, sequentially. Each container had two CPUs and a 2 GiB memory limit.
The table reports medians across the three containers, not per-query peaks.

| Measurement | Full reconstruction | Recall projection | Reduction |
| --- | ---: | ---: | ---: |
| Peak process RSS | 883.22 MiB | 435.27 MiB | 50.7% |
| Peak query-container memory | 874.22 MiB | 425.20 MiB | 51.4% |
| Final process RSS after explicit collection | 378.97 MiB | 254.55 MiB | 32.8% |
| Total handler time, two passes | 43.43 s | 40.54 s | 6.7% |
| Recall/search handler time, two passes | 31.48 s | 28.85 s | 8.4% |
| First request, fresh process | 4.66 s | 4.30 s | 7.7% |
| Complete response bytes per pass | 53,815 | 53,815 | Identical |

Peak process RSS ranged from 882.14–887.34 MiB for full reconstruction and
435.20–435.47 MiB for the projection. First-pass median handler totals were
21.85 s and 20.23 s; repeated-pass totals were 21.45 s and 20.31 s.

Both modes completed all six valid requests and preserved the seventh request's
expected empty-query validation error. The earlier audit's OOM was **not
reproduced** in these sequential runs; these measurements do not establish its
cause or promise that concurrent sessions cannot exhaust memory.

## Frozen inputs and method

The existing `20260924-pre-a9b49e9` backup was checksum-verified and restored into
a new, isolated Neo4j volume. No benchmarks accessed the live database or MCP
container. The restored database had a 512 MiB heap and 128 MiB page cache;
query-container memory excludes the database container.

The original private audit supplied exact tool arguments and the knowledge
cutoff `2026-09-24T12:53:50Z`, resolving to journal change 42540. Reconstruction
started at checkpoint 41039 and replayed 1,501 subsequent events. The backup
head stayed unchanged at 42769 with the same journal hash after all runs.

Both arms used the code based on `9ce29c9` and the same existing
`graph-memory:a9b49e9` Python container, with the worktree mounted read-only.
Full reconstruction leaves the new projection hook inactive. The candidate
opts in through `evals.historical_recall.ProjectedStore`, without modifying the
normal store routing. Main trial order was full/projected, projected/full,
full/projected. Raw requests, responses, logs and detailed timings remain under
ignored `.local/history-191/`. The aggregate JSON records source hashes, backup
checksum, cutoff, individual runs and query-file hashes.

Handler timing includes schema validation, database reads, temporal projection,
ranking and response serialization. It excludes HTTP transport, model work,
artifact writes and explicit garbage collection. Fresh containers reset Python
and cgroup peak counters; database and OS caches were not reset. Background
workloads continued. Latency differences are descriptive measurements, not a
statistical performance guarantee or fully cold database benchmark.

## Candidate and correctness

The candidate retains every fact, entity and insight, preserving full temporal
competitor groups, related decisions, conflict resolution and inference supports.
Episode records retain ID/status, and message records retain ID/timestamp for
report-time fallback. Source sessions, artifacts, observations and dream payloads
are omitted from the recall working set. Evidence remains available through the
existing selective historical evidence path, with original source references
and body-hash checks.

The journal still reads and verifies every checkpoint part and replayed event.
Projection happens after original-record hashing. Every node changed by a later
delta remains complete until replay and its shape/state checks finish. Older
whole-state hash formats reconstruct fully before projection. No current/live
properties are substituted for historical status, aliases or timestamps.

All complete canonical JSON responses matched across modes and repetitions,
including facts, IDs, statuses, conflicts, temporal metadata, pagination,
freshness, source references and error details. The nine supplemental cases
covered compact/full ranking, unranked full recall, pagination/history,
independent `as_of`, a later knowledge cutoff after identity merges, latest,
search misses and full evidence expansion. Full evidence bytes also matched.

Deterministic integration tests cover mixed journal versions, knowledge/event
cutoffs, no future decision leakage, projected episode changes, corrupted delta
shape hashes and corrupt checkpoint data. Final validation: **410 passed,
1 skipped**, Ruff check/format clean, Pyright zero errors, and clean diff checks.
One new corruption test initially selected an already-pending episode and made
no change; it now explicitly selects a completed episode and asserts that a new
journal event was written before testing corruption rejection.

## Decision and limits

The experiment passes its measured memory and response-parity gates. It supports
reviewing this projection for normal historical recall/search routing as the next
implementation step. Deployment remains a separate decision.

This is a smaller working set, not a fixed memory cap: all facts/entities/insights
and message timestamps remain resident. Large namespaces, long delta tails and
legacy snapshots can still use more memory. Explicit post-query collection
measures retained allocator memory under that condition, not arbitrary idle MCP
session behavior. The next optimization could select facts and their complete
temporal dependencies more narrowly, with an additional replay/latency tradeoff.

Machine-readable results: [aggregate JSON](20260924-historical-recall.json).
Reproduction: [eval instructions](../README.md#broad-historical-recall-memory-experiment).
