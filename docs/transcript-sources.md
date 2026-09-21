# Transcript source graph

The markdown import remains on the original Docker stack. It is not rewritten
by this work.
The transcript graph is a separate Neo4j Community container and persistent
volume. Community supports one standard database per instance; namespaces are
logical separation, not a replacement for database isolation.

## Evidence contract

Original conversational claims are the origin of facts. Tool outputs are only
validation evidence, never sources of independent facts. Memory reads, writes,
patches and compaction summaries are contextual, potentially stale material.

Every source-record fact must quote a user or assistant statement in `evidence`.
`validation_evidence` separately quotes tool results. An assistant claim without
such validation is retained only as uncertain and undated. A memory read/write
cannot validate it. Reading yesterday's memory today does not renew its date.
These structural constraints are deterministic; whether a quote semantically
supports a claim still depends on extraction quality and the fixed evals.

## Intake

Use `daemon --source-records --transcripts DIRECTORY` or
`feed FILE --session-id ID --source-records`. This explicitly selects the versioned
source parser, separate from the old text-only feed. Claude Code JSONL and Codex
response-item JSONL are supported, including Codex custom tool calls/outputs.
Memory MCP recall results also remain derived context, not fresh verification.
Provider duplicate event mirrors, reasoning, and system/developer instructions
are not claim sources. Delegated subagent instructions and automated Codex exec
prompts are context, even when their provider role is `user`. Non-text attachments are gaps, not interpreted content.

A session stays a single source identity with multiple bounded episodes. Each
batch contains at most eight new text chunks (90,000 characters), four preceding
chunks of context, and available earlier calls for late results. Individual
records are split at 24,000 characters without dropping remaining text. Each
chunk retains a record ID, role, timestamp, call ID and source classification.
The durable cursor and prefix hash reject rewritten history; repeated intake or
process restart does not replay completed chunks. A partial final JSONL line
waits for completion. One scan opens at most `MEMORY_INTAKE_FILES` files with
work (default 4) and stages at most four batches from each. It stops after 120
seconds. Intake pauses when `MEMORY_INTAKE_QUEUE` episodes (default 32) are due,
counting pending and failed episodes whose retry time has come and leaving out
quarantined ones. The scan resumes after the last file it examined, and a fully
fed file is stamped on its feed node so a restart skips it without parsing.
Staging also pauses while the model provider is unavailable. See
[ingestion pipeline](ingestion-pipeline.md#intake).
Source files must remain available until intake has caught up.

### Backlog inventory

The Compose `inventory` service independently scans the same read-only transcript
mounts, compares them with durable feed cursors, and caches a census for
`memory_status`. It waits 300 seconds between completed scans. It needs neither
Codex credentials nor an LLM and does not modify episodes, cursors, or facts.

For a one-time snapshot using the same paths as the intake worker:

```sh
graph-memory --namespace transcripts inventory --transcripts /sessions/claude --transcripts /sessions/codex --once
```

`unstaged_episodes` uses the current parser and the same eight-chunk/90,000-character
batch limits as intake. It excludes invalid, inaccessible, rewritten, or changing
files and flags incomplete trailing records. Inspect `state`, `gaps`, `stale`, and
scan times before interpreting counts. Paths must match the worker's paths because
feed identities include them. Source changes during a scan and concurrent staging
make this an estimate at scan time, not a transactionally consistent total.
An empty saved queue is not evidence that intake has caught up. A zero unstaged
count is meaningful only for fully counted mounted sources at the recorded scan
time; it says nothing about sessions outside those mounts.

Inventory is operational metadata (`MemoryInventory`), excluded from knowledge
journal snapshots and default graph renders. It becomes stale after two refresh
intervals plus the last scan duration (at least 600 seconds). Servers without an
inventory process report unavailable coverage rather than implying zero backlog.

## Historical memory artifacts

Explicit Read/Write/Edit tools, apply_patch, static cat/sed/head/tail/rg/grep
references and literal nested exec arguments are recognized without executing
source commands. Relative paths resolve against recorded working directories;
unknown relative paths are scoped to their session to avoid cross-project merges.

The importer never opens a memory path mentioned in a transcript. Read results
are captured excerpts, not guaranteed complete file versions. Writes preserve
submitted content; edits preserve the available patch or before/after strings.
An invocation is not proof that the write succeeded. No before-version is
invented. Redaction occurs before graph storage, so these are redacted evidence
records, not byte-identical source archives.

Compound command output is retained on its source message, but it is not assigned
as a file snapshot when individual outputs cannot be attributed. The artifact
observation records `captured=unavailable` and a gap. Dynamic paths, arbitrary
scripts, missing calls/results, and non-text output remain explicit coverage
limitations. The engine does not execute, fetch, or reconstruct missing history.

## Graph and inspection

| Node | Display name | Links |
|---|---|---|
| MemorySession | First conversational request | HAS_EPISODE, HAS_MESSAGE |
| MemoryEpisode | Session title and source date | CONTAINS |
| MemoryMessage | Role/type, date, tool or text excerpt | RESULT_OF, TOUCHED_MEMORY |
| MemoryArtifact | File basename (full path remains a property) | Incoming VERSION_OF |
| MemoryArtifactObservation | Read/write/patch, filename, date | VERSION_OF |
| MemoryFact | Fact summary | CITES claims, VALIDATED_BY tool results, SUPPORTED_BY episode |
| MemoryEntity | Entity name | Existing semantic graph links |
| MemoryDream / MemoryInsight | Dream subject / insight summary | Existing inference links |

Source nodes and their reconstructible links participate in audit verification,
historical replay and candidate revisions. Existing live markdown engines are
not upgraded as part of this deployment. Do not share the new database with an
old engine unaware of source labels.

```cypher
MATCH p=(s:MemorySession)-[:HAS_EPISODE]->(e)-[:CONTAINS]->(m)
RETURN p LIMIT 200;
```

```cypher
MATCH p=(f:MemoryFact)-[:CITES|VALIDATED_BY]->(m:MemoryMessage)
RETURN p LIMIT 200;
```

## Deployment and validation

`compose.transcripts.yaml` starts Neo4j, the transcript worker, the inventory
service and MCP separately from the markdown stack. It requires `NEO4J_PASSWORD`
and `GRAPH_MEMORY_TAG`. The template defaults to eight consumers
(`TRANSCRIPT_WORKERS`), which is what the local deployment has run since
2026-09-20. Earlier allocations (two, then twelve on 2026-09-17) are history.

- Browser: http://127.0.0.1:27474/browser/ (only with `--profile browser`)
- Bolt: bolt://127.0.0.1:27687 (user `neo4j`, `NEO4J_PASSWORD`)
- MCP: http://127.0.0.1:8766/mcp (private token; namespace `transcripts`)

Set the paths/token named in the Compose file; mount sources read-only. Do not
mount model-worker-generated session directories, which would ingest extraction
prompts and outputs recursively.

`tests/test_session_sources.py` covers source parsing, identity, historical
artifacts, append/restart, tool-only rejection and audit replay. Frozen real-model
canaries live in `evals/transcripts/cases.json`; run
`MEMORY_LLM=codex python -m evals.transcripts.run`. They make no graph writes.
A passing canary is not certification of full historical coverage or semantic
accuracy. Preserve failed runs as well as successful evidence.

After both imports reach the agreed fixed source coverage, benchmark the same
questions and expected facts against both graphs. No memory-system superiority
claim or new golden baseline is established by this implementation.

On the local Colima instance, automatic forwarding did not expose the new ports.
Ports 27474, 27687 and 8766 were added to its existing SSH control connection,
without restarting Colima. Check forwarding again after a VM restart. The local
private Compose helper is `~/.local/share/graph-memory/bin/compose-transcripts`;
the original `compose` helper still operates the markdown stack.

The first real-source pilot exposed delegated prompts being classified as direct
user assertions. Its volume was quarantined, not merged or repaired into the
candidate. The corrected release starts on `graph-memory-transcripts_source-v1`.
The original pilot volume is retained for diagnosis; it is not served by MCP.
The original markdown volume and worker image were unchanged.

Release evidence is recorded in `evals/reports/transcript-source-v1.json`, including
the initial failures. Successful model canaries validate specific expectations;
they do not establish that every historical claim is correctly interpreted.

Live smoke at 2026-09-17 11:35 UTC: both consumers completed episodes; 16 were
complete and 21 pending. MCP discovery exposed six tools, recall succeeded, and
the graph rendered without truncation. The audit journal matched the live graph.
No fact used a tool/context message as its conversational citation. A transient
Neo4j deadlock was retried by the driver and subsequent processing succeeded.
These are checkpoint counts, not total archive coverage or a completion estimate.

## Transcript database memory — 2026-09-17

The import stalled at 739 completed episodes with a 512 MiB Neo4j heap.
Journal transactions capture the source graph before and after writes and reached
the default 358.4 MiB aggregate transaction-memory limit. Both intake and
extraction commits failed with `MemoryPoolOutOfMemoryError`; concurrent work also
reported retried deadlocks.

The local transcript database now uses a 1 GiB initial / 2 GiB maximum heap,
with the default transaction limit reporting 1.4 GiB. The Compose template uses
the same defaults, configurable through `TRANSCRIPT_NEO4J_HEAP_INITIAL` and
`TRANSCRIPT_NEO4J_HEAP_MAX`. Page cache remains 256 MiB. Size these settings for
the Docker host's available memory and other workloads; transaction memory
tracking and limits remain enabled.

**Update, 2026-09-21.** The Compose defaults above are superseded. The template
now uses a 4 GiB maximum heap (`TRANSCRIPT_NEO4J_HEAP_MAX`), a 2 GiB page cache
(`TRANSCRIPT_NEO4J_PAGECACHE`), a 1 GiB transaction memory limit and an 8 GiB
container limit. Writes no longer capture the source graph before and after;
they journal only the nodes they change. See
[sizing](operations.md#sizing-neo4j).

The worker drained to zero leases before the database restart. All 739 completed
episode IDs and extraction hashes were preserved, and journal verification
passed at change 1499 with 7,020 records. Only the ten database-error retry
delays were released; validation failures retained their normal retry handling.
The twelve-consumer worker resumed with its existing image and model settings.
At 13:19:49 UTC, completions had increased to 748: nine of the ten database-error
episodes had recovered and one was processing. No new memory-limit errors were
logged in that first minute; five automatically retried deadlocks still occurred.
Private configuration backup and checks are under
`~/.local/share/graph-memory/deployment/memory-fix-20260917T131645Z/`.
This capacity adjustment does not establish full archive coverage or resolve
extraction-quality failures.

## Direct MCP writes and evidence

`memory_ingest` queues new information. Direct submissions now use the
`direct-mcp-v1` contract: without a verified source, their claims can only become
`uncertain` facts with no asserted effective date. Caller-supplied roles,
source-format labels, or URLs cannot certify a claim.

The optional `sources` object maps submitted message IDs to stored
`MemoryMessage` IDs. `memory_evidence` returns these as `source_message_id`.
The server checks that the referenced message exists in the same namespace and
that submitted text is an exact excerpt. It inherits the stored source's role,
time, and tool outcome. A stored user assertion supports an attributed claim;
it is not independent proof of real-world correctness. Assistant claims still
need corroborating tool evidence, while tool results may validate claims but
cannot originate them. Memory reads and writes remain context only.

For example, `"sources": {"m1": "<source_message_id>"}` associates submitted
message `m1` with that existing source. A source URL alone remains provenance,
not validation. Existing imported episodes are not retroactively reclassified.

Unvalidated direct claims use the existing `uncertain` lane. A future background
review can inspect these alongside their episode payload and verified source
references: confirm supported claims through a sourced write, retract disproved
claims with a reason, or retain uncertainty when evidence is missing. An LLM's
agreement alone must not promote a claim. The autonomous review queue and
validation/promotion workflow are not implemented by this contract change;
`uncertain` also contains other forms of uncertainty and is not a dedicated
validation-status field.

## Reducing repeated model work

Validated extractions are checkpointed on the episode before graph commit.
If a database commit fails after the driver's transaction retries, the next
worker attempt reuses that checkpoint, including across process restarts. Reuse
requires the same engine fingerprint and model/provider/effort settings; evidence
is checked again, and entity identities are resolved against the current graph at
commit time. Completed episodes discard the temporary checkpoint. A database
outage before the checkpoint itself is saved can still lose that model work.

After three failed validation runs for an engine version, an episode is paused
for review. A run includes the existing bounded schema/evidence correction calls;
three runs does not mean three individual CLI calls. Provider failures (timeouts
and failed invocations) are not charged to the episode at all: it waits 60
seconds, keeps its attempts, its validation budget and its rejection feedback,
and the provider breaker decides when calls resume. Identity conflicts,
extraction conflicts and damaged checkpoints pause immediately. A journal
mismatch or changed engine files are faults of the namespace or process, so no
episode is charged or paused for them. A batch whose new messages cannot carry
a fact is committed empty without a model call (`skipped: no_claim_in_focus`).
The full rules are in [ingestion pipeline](ingestion-pipeline.md). Source-role validation errors now carry specific diagnostics
and correction feedback instead of being classified as unknown failures.

`memory_status.processing` reports `quarantined` and `cached_extractions`.
Paused episodes remain failed/incomplete and count against coverage. They become
eligible again after an engine change, or an operator can deliberately release
one using `graph-memory --namespace transcripts retry-quarantined EPISODE_ID`.
The existing backoff may still delay an engine-change retry. Historical failure
totals are not assumed to be validation failures: the new counter starts with
this release. No source, fact, or rejection evidence is removed by quarantine.

Within each worker process, database transactions execute one at a time while
LLM calls remain concurrent. The graph's namespace lock already serialized
canonical writes; local serialization avoids contention among those same worker
threads. Other processes still use Neo4j locking and transaction retries.
Checkpoint and retry bookkeeping is excluded from the knowledge audit journal.
