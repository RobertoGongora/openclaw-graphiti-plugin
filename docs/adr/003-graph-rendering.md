# ADR-003: Read-only graph images over MCP

Status: Implemented and locally validated, 2026-09-17.

**Updated 2026-09-21.** The default limits are now 300 nodes and 1,000
relationships, not the 10,000 / 30,000 stated below, because a large namespace
did not lay out within the time limit. A server bound to a bearer token now
refuses custom Cypher, since the output filter described below does not stop a
query from revealing counts about other namespaces. See
[rendering](../rendering.md) and [operations](../operations.md).

Rob wants the graph views used in Neo4j Browser available directly in agent chats:
a whole-graph default with higher limits, or a focused view from optional Cypher.

Add `memory_render` as the sixth public MCP tool, also available in read-only mode.
It reads the requested namespace's knowledge nodes, including disconnected episodes,
and returns a PNG image content block plus counts, timestamp, and truncation flags.
Default limits are 10,000 nodes / 30,000 relationships. Large images show entity hub
labels; focused images show fact summaries and relationship names. This extends the
five-tool decision in ADR-001 without exposing internal extraction/dream workflows.

Use Graphviz's system executable for layout/rasterization, packaged with a font in
Docker. No Python dependency, browser process, image-generation LLM, hosted renderer,
or persisted image session is needed. Default reads fetch only topology/captions.

Custom Cypher is for trusted graph operators. Statements must pass lexical feature
restrictions and Neo4j's read-only EXPLAIN classification. Procedures, imports,
namespaced functions, and multiple statements are rejected; transactions always
roll back. Only matching namespace graph values are rendered; scalars are ignored.
This filter is not a substitute for database-level tenant isolation. Time, output,
row, node, relationship, and nested-value budgets bound the operation.

All 67 deterministic tests passed against isolated Neo4j, covering graph selection,
orphan inclusion, scoped paths and relationship-only results, caps, mutation
rejection, PNG content, and unchanged journal state. Live authenticated MCP discovery
and whole/focused rendering passed; images were visually inspected. Journal change
163 verified. The existing worker kept running. Extraction/dream prompts and schemas
were hash-identical, so no model eval rerun or golden expectation change was needed.
See docs/rendering.md and evals/baselines/render-validation.json.
