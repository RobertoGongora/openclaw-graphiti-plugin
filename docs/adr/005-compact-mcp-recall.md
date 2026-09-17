# ADR 005: Compact public recall with explicit evidence expansion

Status: implemented and validated, 2026-09-17.

Memory lookup latency is only part of the calling agent's cost. The old MCP
response returned entire records in up to seven temporal categories, including
repeated provenance. Frozen examples produced 175–202 KB of JSON before the MCP
text/structured-content representations. Rob requests concise, question-relevant
JSON with inspectable sources and a way to retrieve additional details.

Keep the temporal store, extraction and worker behavior unchanged. Apply a public
MCP view that defaults to five existing facts or derived conclusions, preserves
temporal categories, and exposes freshness, conflicts, ambiguity and omission
counts. Group identical records within a category; do not merge paraphrases or
events at different times. Cap summary excerpts and flag truncation.

Accept an entity name plus an optional question, following Context7's scoped-query
interface. Use deterministic token overlap for this first implementation. Match
all temporal evidence before selecting rows, so mentioning a superseded value
can still surface its replacement within the same exclusive relationship role.
Return no matching facts when lexical selection finds none. This is not semantic
question answering; synonyms and misclassified identities remain limitations.

Add `memory_evidence` for exact quoted claims, separate tool validation and source
references by namespace-bound fact IDs. It also exposes retractions, unavailable
sources and historical snapshots. Evidence inspection does not declare current
truth. Preserve the old record view with `detail:full` and internal engine/CLI
call behavior. Compact pages re-query the graph and expose the revision; callers
can select a fixed historical cutoff when needed.

No retrieval-time LLM, embedding index or Python dependency is added. A future
semantic ranker must demonstrate better coverage/relevance against frozen cases;
shorter output alone is not an accuracy result. The markdown/transcript ingestion
comparison remains deferred until source coverage is ready.

Validation: 94 deterministic tests passed in a disposable Neo4j database, including
MCP HTTP, temporal semantics, source evidence, historical queries, namespace
isolation, retractions, compact selection and pagination. Four private frozen
responses shrank from 175–202 KB to 1.8–2.3 KB. This deliberately returns fewer
records; counts and expansion expose what was omitted. The report contains no
private fact text: `evals/reports/compact-recall-v1.json`.

The MCP services on ports 8765 and 8766 were independently upgraded to
`graph-memory:recall-6a5ac4442b31`. Live discovery, compact/full recall, latest and
exact-evidence retrieval passed on both. Both ingestion worker images and start
times were verified unchanged. No facts or extraction expectations were rewritten.

Reference: https://github.com/upstash/context7#available-tools
