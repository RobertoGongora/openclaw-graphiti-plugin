# Safe engine revisions

A revision re-extracts selected episodes and shows what would change before
anything changes. It is an overlay: nothing is copied, so its cost follows the
episodes selected and not the size of the graph. It records the engine identity,
the candidate extractions, a semantic diff, the affected dreams, and the validation
evidence in Neo4j.

1. Run the golden eval suite on the new engine and model. Keep the report.
2. Select 1–100 fully extracted source episodes (`--reason` records why).
3. Build. Each selected episode is extracted again as a dry run against the live
   graph: one model call per episode, nothing written to the live graph. Affected
   completed or applied dreams are revalidated the same way.
4. Review the diff: added, removed, changed and unchanged claims, dream outputs,
   and `dropped_decisions`: facts a person confirmed that have no matching
   candidate, and facts a person retracted that the candidate would bring back.
   Diff and validate work by running the promotion inside a transaction that is
   always rolled back, so they show exactly what promotion would do.
5. Validate project-specific expectations in addition to the golden suite.
6. Promote atomically, as one journaled write that touches only the selected
   episodes, their facts and entities. Old facts of those episodes are retracted as
   superseded, a confirmation carries over to the matching new fact, and knowledge
   as of an earlier change still shows the old facts. Promotion is blocked by an
   engine or suite change, incomplete dreams, missing checks, a change to the
   affected facts since validation, or a diff with changes or dropped decisions
   that was not accepted by its digest.

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

Promotion retains old facts as retracted, with the revision ID as the reason;
it does not rewrite transcripts. New facts are committed in the same Neo4j
transaction. Affected old dreams retain their snapshots and outputs and are
marked superseded. Revalidated candidate insights retain provenance back to their
candidate dream and promoted source facts.

Candidate graphs and reports remain available for inspection. This first version
supports bounded replay and forward correction, not a general rollback command,
schema migration framework, or automatic scheduling. Revision operations are an
operator CLI surface, not exposed to ordinary remote memory clients. Run them
from the repository (the tests and golden artifacts are part of release validation).
