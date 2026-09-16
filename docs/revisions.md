# Safe engine revisions

The revision workflow isolates extraction changes from the live namespace.
It records immutable inputs, an engine identity, candidate results, a semantic
diff, affected dreams, and validation evidence in Neo4j.

1. Run the golden eval suite on the new engine and model. Keep the report.
2. Select 1–100 fully extracted source episodes to replay. The candidate includes
   the existing namespace as context, including aliases and retractions.
3. Re-extract those sources in the candidate graph. Re-run affected completed or
   applied dreams with the candidate context and the configured model.
4. Review added/removed claims and dream outputs. Validate project-specific
   expectations in addition to the global golden suite.
5. Promote atomically. Concurrent live/candidate changes, engine/suite changes,
   incomplete dreams, missing checks, and unaccepted changed diffs block promotion.

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
