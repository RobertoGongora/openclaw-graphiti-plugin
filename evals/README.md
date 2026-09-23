# Behavioral evals and golden baselines

The eval runner is intentionally small: pytest for deterministic/integration
checks, JSON fixtures for LLM behavior, and standard-library orchestration.
There is no model judge that can relax an expectation to match a new response.

## Fixed expectations

- Projects retain deployed dependencies alongside planned migrations.
- Decisions are ordered by their original decision time, not late report time.
- Updated personal preferences supersede old evidence within the same scope while
  preserving separate reporting preferences.
- Atlas keeps deployed MySQL alongside a planned PostgreSQL migration.
- PHP is inferred only through supported Laravel → PHP evidence.
- Undated imported notes cannot become newly observed current state.
- Instructions embedded in a transcript cannot invent a completed repetition.
- Dreaming preserves source facts and promotes only supported candidate insights.
- A habit repetition is one event-time regression example, using the same generic
  entity/relation retrieval as projects, people, and services.

The deterministic suite additionally exercises invalid quotes and schemas,
concurrency/idempotency, namespace isolation, temporal conflicts, source adapters,
HTTP headers/auth/Origin, dependency invalidation, structural repair, candidate
replay, changed-diff acceptance, and concurrent-write promotion rejection.

```sh
uv sync
# Explicit endpoint is mandatory. Use the disposable database from compose.test.yaml.
# Never use 17687 or 27687: those are the live graphs, and evals wipe what they touch.
make test-db-up
export MEMORY_TEST_NEO4J_URI=bolt://127.0.0.1:37687
uv run pytest -q
MEMORY_LLM=codex uv run python -m evals.run --runs 2 \
  --output .local/baseline-candidate.json
```

The model runner first runs deterministic/Neo4j tests, then evaluates every
fixture. Nonzero exit means failed checks. Every case gets a new `eval:<UUID>`
namespace, cleaned in a finally block. The runner never updates the fixtures.
Reports include actual results, engine and suite hashes, model, reasoning effort,
repeat count, and the deterministic gate. An engine change during a run fails it.

For model variance, repeat the suite; one pass is a smoke test, not a stability
claim. Sensitive real-bank results remain under ignored `.local/`; only synthetic
reports or aggregate evidence belong in version control.

The [effort comparison](effort-study.md) records why Luna now defaults to medium,
including failed trials and the schema correction they exposed.

## Graph baselines

`evals/graph_baseline.py` snapshots everything countable about a live graph:
schema, node and relationship counts, degree distributions, episode outcomes and
timings, fact status and validation crosstabs, entity kinds, message types and
gaps, sessions, feeds, artifacts, journal state, integrity checks, and, with
`--container-prefix`, the deployment's images, memory, store size and worker
log. It is read-only and records counts only, so a report can be committed.
`--names` adds entity, subject, slot and path names; keep such a report under
`.local/`.

```sh
set -a; . ~/.local/share/graph-memory/deployment/transcripts.env; set +a
NEO4J_URI=bolt://127.0.0.1:27687 uv run python -m evals.graph_baseline \
  --output evals/reports/$(date +%Y%m%d)-graph-baseline.json \
  --container-prefix graph-memory-transcripts
```

`reports/20260922-graph-baseline.json` is the first: the transcript graph the
morning after the backlog finished under the shell-output and turn look-back
rules (commit 7ee5f34). Compare later snapshots against it.

## Relation and slot vocabulary

`evals/vocabulary.py` extracts sampled real batches and reports the relation
mix, the share that falls back to `related_to`, how many slots are emitted and
how many of those only one fact uses, and the assistant-claim validation rate as
a regression check. Create a shared `--corpus /absolute/path/.local/vocabulary-corpus.json`
on the first run and reuse it from both worktrees: a seed alone cannot freeze
session files that are still changing. The corpus contains private transcript
text and must stay under ignored `.local/`. Reports checkpoint atomically after
every batch; repeat the same command to resume. A different engine, model, effort
or corpus refuses the checkpoint. Slot identities in the report are hashed.
Version 2 reports include per-batch counts and bounded failure diagnostics. Use
`--details-dir /absolute/path/.local/vocabulary-details` to retain accepted and
last rejected extractions for evidence review. Those files contain private claim
text and quotes; never commit them. Version 1 reports need a fresh output path
because they cannot reconstruct batch-level results.
The 2026-09-22 change adds three relations, limits slots to role-bearing relations,
and ignores single-use slots at read time.

The [2026-09-22 paired run](reports/20260922-vocabulary-comparison.json) reduced
`related_to` from 67/104 facts to 2/103, but assistant-claim validation fell from
16/97 to 13/97. Both engines failed one of 24 batches and emitted no slots.
That first run held the release. The separately retried failed new-engine batch
passed, but its retry is not substituted into the comparison.

The [repeated comparison](reports/20260922-vocabulary-repeats.json) includes three
runs per engine on those same 24 frozen batches, plus a separate eight-episode
sample selected for previously emitted slots. Main-sample validation is 43/307
(14.0%) old versus 46/287 (16.0%) new, and `related_to` falls from 220/326 (67.5%)
to 3/306 (1.0%). The targeted sample drops one singleton slot to zero and retains
a shared webhook role. Main-sample failures are 3/72 versus 4/72; targeted-sample
failures are 1/8 versus 0/8, totaling 4/80 for each engine. The primary metrics
support rollout after CI and backup. This bounded study does not prove statistical
non-inferiority; batch evidence and the report preserve the variability and failures.

## Frozen recall regression

`python -m evals.recall_regression PRIVATE_SNAPSHOT --output REPORT` evaluates
question-ranked compact/full views on saved complete projections, without any
live graph or model access. Inputs contain `cases` with `entity`, `question`,
`raw`, source-reviewed `expected_ids`, and optional `baseline_ids`. Every case
needs a question; empty expectations are unscored. Outputs contain only sizes,
timings and hit counts. Keep private snapshots in `.local/`. The 2026-09-22
development comparison is `reports/20260922-recall-ranking.json`; it is not an
independent accuracy estimate. Compact and full run on the same fake store, so
their agreement checks the formatting path, not the database projection. Do not
build expected answers from live recall: the study sessions that produced the
original echoes are themselves ingested, so review each expected record against
its source before freezing it.

## Establishing the golden standard

`baselines/candidate.json` records the current candidate, not an automatically
approved baseline. Review the cases for product correctness and the repeated
results for model variance before blessing them. Changes to `cases/*.json` are
ordinary reviewable diffs and need a reason explaining the intended behavioral
change. Never copy actual output into expected output merely to make a run pass.

Add regressions before fixing newly discovered failures. Prefer semantic checks
(e.g. a deployed database and its time) over exact prose or entity IDs. Expectation
files may also be used for project-specific revision checks; see
[the revision workflow](../docs/revisions.md).

## What these checks do not prove

Exact quote validation proves provenance, not that a model interpreted a quote
correctly. These cases sample important behavior; they do not certify every
transcript, language, ambiguous date, or provider. Replaying real sources in a
candidate namespace and inspecting the diff is still part of an engine release.
Unseen sessions and sources still waiting for extraction cannot be included in
freshness claims.

## Real Claude native-memory versus MCP E2E

```sh
MEMORY_LLM=codex uv run python -m evals.ab \
  --memory-dir /path/to/claude/project/memory \
  --output .local/claude-ab.json
# Bind the E2E evidence into a repeated model-eval report for the same engine:
MEMORY_LLM=codex uv run python -m evals.run --runs 2 \
  --ab-report .local/claude-ab.json --output .local/release-candidate.json
```

The pinned Atlas question in `ab/atlas-postgres.json` asks about the last known
migration status. Its expected answer is written from known source evidence,
not generated from either response. No live production tools are provided, so
claiming live verification fails the check.

The runner copies and redacts the selected native memory bank, preserving mtime,
then supplies identical source bytes to both arms. A focused known-answer bank
can contain the original `MEMORY.md` index plus its relevant source documents;
the report lists exact files and hashes so corpus scope is inspectable. The native arm enables Claude
auto memory and permits read-only file tools. The graph arm disables auto memory,
has no file tools, and uses only a namespace-scoped read-only MCP server. Separate
working directories and new session IDs prevent conversational carryover. Both
use the same Claude model. The graph's default extraction/dream model is Terra; `MEMORY_REASONING_EFFORT` selects effort (default `low`). Fast mode is not requested.
The sources, tool events, evidence quotes, answers, exact model usage, and checks
are written to ignored local reports, then the test graph is removed.

Scoring checks the fixed answer fields, exact original evidence quotes, an actual
successful source Read/content-Grep/native or memory_recall/MCP call, absence of denied tools, and the
separation of last-known knowledge from live verification. This is a bounded
product E2E, not a claim that either memory system is universally better. Add
separate known-answer cases for habits, freshness, verification-enabled sessions,
and source conflicts as the corpus grows; never replace expectations with the
engine's output. Structural assertion fields complement human review of the full
answer and tool trace.
