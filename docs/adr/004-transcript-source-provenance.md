# ADR 004: Transcript claims with separate validation and artifact evidence

Status: accepted and validated, 2026-09-17. Updated 2026-09-26 for Cursor
agent-transcript support.

The original deployment imports derived markdown memories. It cannot establish
what was said, corrected, observed by tools, or merely repeated from old memory.
Rob requires transcripts as primary sources and explicitly requires tool outputs
to validate conversational claims rather than generate independent facts.

Use a versioned Claude/Codex/Cursor transcript adapter, durable incremental cursor
and bounded episodes, retaining session identity. Store messages/calls/results and
historical memory observations alongside the existing fact graph. Do not read
today's memory file to fill a missing historical version. Preserve gaps.

Keep conversational `evidence` and tool `validation_evidence` distinct. A fact
without a conversational claim is rejected; assistant claims without primary
validation remain uncertain and undated. Memory reads/writes never validate
current state. Exact quotes remain necessary but do not prove semantic entailment.

## Timestamp and dating policy

Timestamps in transcript records establish when a claim was made. When a record
lacks a timestamp:

- The message's `timestamp` field remains `null`.
- Facts extracted from such claims must use `status=uncertain` and `valid_at=null`.
- The extraction system must not invent timestamps from file modification times
  or other heuristics. Undated claims must not pretend to be dated.
- File modification times are operational metadata only, not evidence of when
  statements were made.

This policy applies equally to Claude, Codex, and Cursor records. Cursor
agent-transcripts may have weak or missing timestamps; the parser accepts them
and preserves the gap.

Keep the existing markdown workers running until completion for comparison.
Deploy the transcript engine to a second Neo4j Community container/volume, with
its own Browser and MCP endpoint. Community allows one standard database per
instance; namespace separation alone would not prevent an unscoped query from
mixing experimental and benchmark records. No paid edition or added Python
package is necessary.

Source records, facts and inferences have readable names. Source graph edges are
reconstructible from journaled properties and survive replay. Frozen source
canaries complement existing temporal, revision, retry and MCP tests. Bulk
extraction is enabled only after these checks pass, with conservative handling
of compound commands and unsupported artifact formats.

Reference: https://neo4j.com/docs/operations-manual/current/database-administration/

Validation: 86 deterministic tests and four frozen transcript-source model
canaries passed on the release engine. All six existing model regression cases
passed on Terra/low before the parser-only origin fix; the extraction contract
files are byte-identical in the release. The initial
source citation and legacy prompt failures, their fixes, and final evidence are
recorded in `evals/reports/transcript-source-v1.json`. This establishes the input
contract, not a native-memory versus transcript-graph benchmark result.
