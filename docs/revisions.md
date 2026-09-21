# Safe engine revisions

A revision re-extracts selected episodes and shows what would change before
anything changes. It is an overlay: nothing is copied, so its cost follows the
episodes selected and not the size of the graph. It records the engine identity,
the candidate extractions, a semantic diff, the affected dreams, and the validation
evidence in Neo4j.

1. Run the golden eval suite on the new engine and model. Keep the report.
2. Select 1–100 fully extracted source episodes (`--reason` records why).
3. Build. Each selected episode is extracted again as a dry run against the live
   graph: one model call per episode, two when the first answer fails evidence
   validation and gets its one correction pass; nothing is written to the live
   graph. Completed or applied dreams that cite the affected facts are revalidated
   the same way (again one call, two with a correction). A failure names the
   episode or dream in `build_error` and leaves the revision in `building`; build
   again to continue. A failure that building again cannot cure (a dream whose
   query is too broad for the graph the revision produces) ends the revision as
   `failed`, and `get` says why.
4. Review the diff: added, removed and changed claims, dream outputs, the ids and
   count of unchanged claims, `revived` facts, and `dropped_decisions`.
   `dropped_decisions` lists every confirmation that would be lost, with the reason:
   no candidate makes the claim, the matching candidate is not `uncertain`, or
   another confirmation of the same claim took the only matching candidate. It also
   lists facts a person retracted that the candidate would bring back. `revived`
   names each retracted fact that goes live again under the same id, who retracted
   it, and whether a confirmation returns with it: one retracted by an earlier
   revision keeps its confirmation, one retracted by a person does not.
   Diff and validate work by running the promotion inside a transaction that is
   always rolled back, so they show exactly what promotion would do.
5. Validate project-specific expectations in addition to the golden suite. Checks
   may use `memory_recall`, `memory_latest`, `memory_status`, `memory_evidence`
   and `memory_search_entities`: the tools that read through the preview. The
   checks are stored on the revision.
6. Promote atomically, as one journaled write that touches only the selected
   episodes, their facts and entities, and the affected dreams and insights. Old
   facts of those episodes are retracted as superseded, a confirmation carries over
   to a matching new fact that is still `uncertain`, and knowledge as of an earlier
   change still shows the old facts. Inside the same transaction the stored checks
   run again on the graph being published, and the affected dreams are found again.
   Promotion is blocked by:
   - an engine, model, effort or suite different from the one the revision was
     created with (diff and validate refuse these too);
   - a change to the affected facts since validation;
   - a stored check that no longer passes, for example because another episode
     has since introduced a conflict: validate again;
   - a dream applied since the build that cites the affected facts, or an affected
     dream that another revision has already replaced: build again, which
     revalidates against the dreams in force and clears the validation;
   - a diff with changes, revived facts or dropped decisions that was not accepted
     by its digest.

**Resumed builds.** A build that continues after a failure keeps the candidates it
already has, even if the live graph has moved since (each candidate records the
`live_revision` it was extracted at). The live graph only supplies name hints to the
extraction; entity identity, the diff and the checks are always worked out against
the graph as it is at diff, validate and promote time. To extract everything
against today's graph, create a new revision.

**Locking.** Each preview (diff, validate, the dream context during build) and the
promotion itself hold the namespace write lock for their whole duration, so
ingestion and other writers to that namespace wait; recall is not blocked. Measured
on a local Neo4j 5.26 with three facts per episode and the journal audit off: about
50 ms for one episode, 0.2 s for ten and 0.7 s for thirty, roughly 20–25 ms per
episode, the same for a preview as for the promotion. Model calls never happen
while the lock is held. `MEMORY_JOURNAL_AUDIT` adds a hash of the whole namespace
to each of these transactions; keep it sampled or off where that matters.

Revisions created before the overlay (with a copied candidate graph) can still be
read with `get`; build, diff, validate and promote refuse them.

```sh
export MEMORY_LLM=codex
uv run graph-memory --namespace personal revision create --episode EPISODE_ID
# Use the returned revision_id for all following calls.
uv run graph-memory --namespace personal revision build --id REVISION_ID
uv run graph-memory --namespace personal revision diff --id REVISION_ID
uv run graph-memory --namespace personal revision get --id REVISION_ID
uv run graph-memory --namespace personal revision validate --id REVISION_ID \
  --eval-report .local/baseline-candidate.json --checks project-checks.json
uv run graph-memory --namespace personal revision promote --id REVISION_ID \
  --accept-diff REVIEWED_DIFF_DIGEST
```

Example `project-checks.json`:

```json
[
  {
    "tool": "memory_recall",
    "arguments": {"query": "Atlas"},
    "path": "current",
    "contains": {"relation": "uses_database", "target_contains": "mysql"}
  },
  {
    "tool": "memory_recall",
    "arguments": {"query": "Atlas"},
    "path": "current",
    "excludes": {"relation": "uses_database", "target_contains": "postgre"}
  }
]
```

The digest accepts precisely the reviewed diff. Automation can promote unchanged
outcomes without a digest. For changed outcomes, automation must be supplied the
accepted digest; a passing general suite alone is insufficient authorization to
change project knowledge. This keeps baseline evolution deliberate.

Promotion retains old facts as retracted, with the revision ID in
`replaced_by_revision`; it does not rewrite transcripts. New facts are committed in
the same Neo4j transaction. Affected old dreams retain their snapshots and outputs
and are marked superseded by a new dream that carries the revalidated output. The
new insights retain provenance back to that dream and the promoted source facts.

Candidate extractions and reports remain available for inspection. This version
supports bounded replay and forward correction, not a general rollback command,
schema migration framework, or automatic scheduling. Revision operations are an
operator CLI surface, not exposed to ordinary remote memory clients. Run them
from the repository (the tests and golden artifacts are part of release validation).
