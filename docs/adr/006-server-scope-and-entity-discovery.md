# ADR 006: Server-owned namespace and explicit entity discovery

Status: Implemented and validated, 2026-09-17.

**Updated 2026-09-21.** The catalog has nine tools since ADR-007 added
`memory_status`; "eight-tool catalog" below describes this release. The worker
counts under Deployment are those of 2026-09-17; the transcript deployment has run
eight consumers since 2026-09-20. See [operations](../operations.md).

The connected endpoint already authorizes a fixed namespace. Requiring the LLM
to repeat an undisclosed namespace creates an unnecessary integration failure.
Likewise, `query` was ambiguous beside `question` when it meant an entity selector.

Scoped MCP endpoints now omit namespace from advertised schemas and inject their
configured value before validation, including nested transcript ingestion. A
caller-supplied different namespace remains forbidden. Unbound administrative
endpoints retain the explicit field. No endpoint changes its graph or searches
across scopes. Correct legacy namespace arguments continue to work.

Recall advertises `entity` plus optional `question`; legacy `query` is accepted
but conflicting selectors are rejected. Internal engine and CLI schemas are
unchanged. Add the read-only `memory_search_entities` for ranked lexical candidate
discovery by names, aliases and query words. It returns stable keys, kinds,
bounded aliases and pagination without merging identities. Exact keys and aliases
rank before partial matches; no semantic or typo-correction model is introduced.
Historical search resolves entities against the requested journal snapshot.

Validation: 98 deterministic tests passed in disposable Neo4j, including namespace
injection, nested ingestion, namespace rejection, legacy callers, ambiguous names,
partial/alias matching, pagination and historical search. Both live MCP endpoints
passed search-to-recall-to-evidence calls with no namespace argument. The eight-tool
catalog exposes the corrected schemas. The initial driver parameter collision was
caught in integration tests and fixed before deployment.

Deployment: `graph-memory:discovery-c9f05164db3d` on MCP only. Ingestion remains
12 transcript consumers and 2 markdown consumers on their existing images, with
start times verified unchanged during the MCP upgrade. Evidence is in
`evals/reports/entity-discovery-v1.json`.
