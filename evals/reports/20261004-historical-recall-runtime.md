# Historical recall default-runtime validation

PR [#192](https://github.com/RobertoGongora/openclaw-graphiti-plugin/pull/192)
now uses the recall projection in ordinary historical recall, search and latest.
This fresh check compares the old ordinary service at `3032c25` with the new
ordinary service at `ec5eb01`, using separate Python processes and the same
frozen synthetic Neo4j namespace. The updated runner's `projected` mode uses
`GraphStore` directly; there is no opt-in store subclass in the measured arm.

| Median across three fresh processes | Old ordinary service | New ordinary service |
| --- | ---: | ---: |
| Peak process RSS | 148.31 MiB | 106.56 MiB |
| Total handler time, 24 requests | 4.87 s | 4.73 s |

Peak process memory was **28.15% lower** in this fixture. All **72 paired
recall/search/latest responses** matched byte for byte. The new runner's forced
`full` baseline also matched all 24 original-service responses, confirming that
it still measures a complete reconstruction. Two additional controls (entity
search and full evidence) matched; they do not use the new projection and are
excluded from the 72 pairs.

## Inputs and checks

The isolated Neo4j 5.26 instance used a 512 MiB heap and 128 MiB page cache.
The namespace contained 1,000 synthetic source sessions with eight tool messages
and eight artifact observations each, plus two source-backed database claims.
The journal verified 26,023 records. Sources were staged with the real source
graph implementation before initializing the fixture's journal. The claim
changes were then written through the normal journal. No live graph was read or
changed.

Requests used both `at_change` and `known_at`, a checkpoint at change 3 and a
checkpoint plus two deltas at change 5. Each set included compact/full recall,
compact/full latest, keyword search and an empty-result search. The complete
canonical JSON responses were compared, including IDs, temporal metadata,
history and freshness. The journal was verified before and after all queries;
its head stayed at change 5 with hash
`8dc60045365886df936f1930b4e2edc8ee4310393049f04c2b4f9af811a9f74c`.

The measured new code also passed 22 Neo4j regression tests covering projection
during restore, default handler routing, the genuinely full baseline, historical
response parity, timestamp fallbacks, old journal formats, replay and integrity
failures. Those tests exercise richer temporal histories than this memory fixture.

## Limits

This is a small, source-heavy synthetic graph with two facts, run sequentially
in local macOS processes. It confirms a reduction in the normal service's
working set; **28.15% is not a production estimate**. It measures process memory,
excluding Neo4j. It does not test concurrent load or promise to prevent OOMs.
Database and OS caches were not reset, and other workloads continued; timings
are descriptive rather than a latency guarantee. Trial order was full/projected,
projected/full, full/projected. Each process made one pass through the 24 calls.

The [2026-09-24 production-copy experiment](20260924-historical-recall.md)
remains separate historical evidence at its original source hashes. Its 50.7%
result is not a fresh measurement of this runtime commit.

Memory still grows with facts, entities, insights, message timestamps and nodes
changed since the checkpoint. Inline checkpoints and version 1 histories can
require a complete reconstruction before projection. Delta events are read twice.

The [aggregate JSON](20261004-historical-recall-runtime.json) records full commit
IDs, code and probe hashes, individual runs, fixture counts and the verified head.
