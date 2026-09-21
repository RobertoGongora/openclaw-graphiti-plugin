# ADR-001: Standalone temporal graph memory with validated model revisions

Status: Implemented. Deployed locally since 2026-09-16; the golden baseline remains a candidate awaiting product review.

**Updated 2026-09-21.** The public catalog has grown from the five tools decided
here to ten (ADR-003, ADR-005, ADR-006, ADR-007, and `memory_confirm`). Reads no longer restore missing
links; that is the explicit `repair` command. Neo4j now requires a password and
its Browser is published only on request. The queue, leases, retry budget and
provider breaker are described in [ingestion pipeline](../ingestion-pipeline.md),
and running the stacks in [operations](../operations.md).

## Problem

The OpenClaw plugin delegated extraction to Graphiti and also indexed Markdown
excerpts. Host-specific hooks and asynchronous acceptance did not provide a
portable, explicit guarantee that a newly observed decision or project change
was available to the next session. A new extraction prompt could also silently
change stored knowledge without behavioral regression evidence.

## Decision

Use Python, Pydantic, and the official Neo4j driver. Expose typed tools through a
small tools-only MCP 2026-07-28 HTTP/stdio binding. Keep application state in Neo4j
and pass namespace and required identifiers explicitly. Expose only five intent-oriented
session tools: recall, latest, ingest, retract, and merge. Keep extraction, commit,
repair, dreaming, and revision orchestration inside Python/CLI. Retain
legacy stdio initialization only as a compatibility binding.

Represent source transcripts as durable episodes. Represent temporal statements
as fact nodes with typed subject/target links, occurrence/observation time, status,
confidence, and exact quote evidence. Reify facts so corrections, corroboration,
dream support, and source provenance are directly traversable. Full source content
is available from the graph; source files are never needed during retrieval.
The engine is entity-centric rather than an activity-specific tracker. Projects,
people, services, technologies, decisions, preferences, lessons, and events use
shared projection and provenance rules. `memory_latest(entity, relation)` is
kind-independent, preserves simultaneous facts, and exposes unresolved claims.
Existing exclusive relationship roles are supplied during subsequent extraction
so scoped updates can reuse the same role instead of inventing new slots.

Use an atomic complete receipt to mark visibility. Pending/failed episodes stay
in an explicit durable inbox. Claude hooks and a host-neutral JSONL watcher
feed new transcript messages through durable append-only cursors; a leased
worker drains the queue with retry backoff. Docker Compose includes Neo4j/Browser,
the MCP endpoint, and a daemon with a read-only bank scanner and queue consumers.
The scanner recognizes imported source content across macOS/Linux creation-date
differences and restarts; changed files become new immutable evidence. Canonical identities use keys/aliases, with explicit
audited merging when ambiguous. Namespace write locks serialize canonicalization
and revisions. Leader-routed transactions prevent a new MCP process from reading
a lagging cluster follower immediately after a successful commit.

Compute current state on demand using event time. Keep intent and unknown time
separate. Retain filesystem dates for undated imports in a documented-claims
lane without promoting them to occurrence or observation times. Undated completed
activities remain time-uncertain evidence; unresolved claims qualify latest-evidence
results instead of being discarded or assigned an invented time. Preserve
equal-time conflicts instead of choosing by import order.
An explicit exclusive slot supports changing a primary database while allowing
multiple databases where the source does not establish exclusivity.

Use Codex CLI with `gpt-5.6-terra` at `low` effort for extraction and
dreaming, with fast mode off and model/effort configurable. On 2026-09-17 Rob chose
to retain Terra after it passed both frozen real-bank failure probes and all six
fixed scenarios; Luna medium and high failed both probes. This supersedes the
earlier Luna medium choice. See [comparison evidence](../../evals/effort-study.md). Any LLM
can instead supply the same extraction schema through the Python/CLI interface; an optional generic
HTTP adapter supports unattended non-Codex models. Model output is validated and
never executed as Cypher or shell. There is no Graphiti runtime dependency.

Dreaming follows the separate-output pattern documented by Anthropic: snapshot
existing memory and transcripts, generate supported candidate insights, then
promote only against the unchanged source revision. Insights remain labeled as
inferences and depend on current supporting facts.

Use pytest and fixed JSON behavioral fixtures rather than adding a Node-based
evaluation stack. Version the engine source, prompt/schema code, dependencies,
eval suite, model, and effort in validation evidence. Run model evals only after
deterministic checks, in isolated namespaces. Expected outputs never self-update. A fresh-session Claude native-memory/MCP
A/B harness records retrieval traces and tests the same known-answer question
against identical source snapshots with native memories off in the graph arm.

For an engine change, clone the namespace, replay selected episodes, revalidate
affected dreams, compare claims, and apply explicit project expectations. Atomic
promotion requires matching live/candidate revisions and an accepted changed diff.
Keep original transcripts, old facts, and old dream outputs for audit.

## Validation

See `docs/validation.md` for executed checks and the bounded real-bank trial.
The model-facing JSON Schema expresses the same event-time constraint as runtime
validation: undated events must remain uncertain. Provider output is still
validated again before commit. Original behavioral expectations remain unchanged.

## Consequences and limits

- Two direct runtime packages; Docker, uv, Codex CLI, and pytest are optional
  environment/development tools. Neo4j is the only persistent service.
- The wire implementation is deliberately narrow. It does not claim optional
  MCP extensions, hosted OAuth, or broad third-party client certification.
- The deployment must send new transcripts; stateless tools cannot observe
  arbitrary private sessions automatically. Freshness exposes incomplete intake.
- Quote/schema validation cannot prove semantic entailment. Golden tests, replay
  diffs, and human review remain important when expanding scope or model support.
- Queries resolve entity names/aliases; there is no vector search or freeform
  query planning inside the storage service.
- Graph repair reconstructs structural links; it does not fabricate deleted
  source evidence or guess the right side of an unresolved contradiction.
- The initial revision implementation snapshots a namespace in memory and is
  intended for a personal graph. Large multi-user installations need bounded
  snapshot storage, identity authorization, and operational sizing work.
