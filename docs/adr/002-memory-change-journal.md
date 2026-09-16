# ADR-002: Knowledge journal and historical replay

Status: implemented and validated in isolation; live activation pending.

## Decision

Keep the current graph as the ordinary recall view. Add append-only `MemoryChange`
records under `audit:<namespace>` with `scope` pointing to their owning namespace.
No journal edges connect to the current entity/fact graph. Existing namespace and
label filters exclude journal entries from ordinary retrieval and visualization.
This is a database journal, not a distributed blockchain or protection against a
database administrator rewriting the complete database.

Capture a baseline when journaling first starts. Existing state is documented as
preexisting knowledge at that boundary; earlier mutations are not fabricated.
Reject historical knowledge cutoffs before journal coverage begins.

Record source acceptance, validated fact commits, entity merges, fact retractions,
dream creation/completion/publication, candidate creation, and revision promotion.
Every knowledge mutation and its journal entry share one Neo4j transaction and
namespace lock. Nested commits in a revision promotion form one journal change.
The worker's leases, error messages, attempts, and retry scheduling are operational
state and are excluded; an incomplete source remains pending in historical views.

Store property deltas and a full checkpoint every 100 changes. Sequence numbers
are authoritative for exact ordering. Monotonic recorded timestamps provide
human-facing knowledge cutoffs. Hashes link entries and verify reconstructed state.
Before a write, compare current knowledge against the last journal state hash;
stop on untracked changes instead of silently inventing missing history.

## Two clocks

`as_of` selects when facts were true or events happened. `known_at` selects what
information had entered this memory system by a given time. `at_change` is an
exact journal sequence alternative to `known_at`. Historical recall reconstructs
entity identities, fact retractions, and insight publication before projecting
facts by event time. It never calls a model or retrieves the source files again.
When a historical cutoff is supplied without `as_of`, the selected change's
recorded time is also the event-time cutoff.

Retain the five public MCP tools and their intent-oriented descriptions. Add
optional cutoffs to recall/latest rather than exposing orchestration tools.
Operator CLI commands initialize, list, verify, inspect, and replay history.

## Replay and isolation

Materialized historical views require a new `replay:` namespace, preserve source
provenance, remap structural IDs, and are read-only through engine mutations.
They cannot overwrite an existing graph. Replays use stored changes; they do not
rerun an LLM. Re-extraction with a different engine remains the separate validated
revision workflow. No experiment is automatically promoted into personal memory.

Dream snapshots in replay graphs keep their original embedded evidence IDs for
audit. Their nodes, published insights, and graph links are remapped, but original
snapshots cannot be run or applied from a read-only replay.

## Costs and boundaries

This first implementation reads the namespace's knowledge state around a write
to derive deltas and detect untracked edits. That adds work proportional to graph
size on writes, while ordinary current recall avoids journal reconstruction.
Historical reads verify the hash chain and replay deltas from the nearest prior
checkpoint. Large deployments may need targeted change capture and indexed,
streamed journal verification; those optimizations must preserve these contracts.

Source ordering is unchanged. Journaling reproduces accepted changes, including
order-dependent model decisions; it does not make model extraction order-invariant.
Historical state uses the running temporal projection rules. Byte-for-byte replay
of a past assistant answer also requires the historical model/engine and prompt.

## Validation

Deterministic tests cover out-of-order observations versus knowledge acquisition,
current/historical recall parity, retractions, merges, insight publication and
support invalidation, concurrent idempotent writes, transaction rollback, journal
corruption detection, bootstrap coverage, checkpoint reconstruction, isolated
read-only replay, and atomic revision promotion. Model regression expectations
remain fixed; the existing real-bank exact-quote A/B limitation remains separate.

The Docker candidate passed 47 deterministic tests and all six fixed Luna/medium
cases twice (12/12). A private replica of 1,677 existing knowledge records retained
identical current results after bootstrap, with matching historical results for
five representative subjects. Baseline creation took 0.17 seconds on that replica;
full head verification took 0.15 seconds. These are one-run measurements, not a
scaling guarantee. Sanitized results are committed in
`evals/baselines/journal-validation.json`; golden approval remains false.
