# Graph images

`memory_render` renders the live knowledge graph as a PNG in the MCP response.
The image is produced locally; graph data is not sent to an LLM or hosted image
service. Pink entities, olive facts, yellow episodes, orange dreams, and pink
insights follow the Neo4j palette. Fact captions use `summary`, never confidence.

Whole namespace:

```json
{"namespace":"personal"}
```

A focused graph around Alfred, DailyAI, and Unearth, including incoming facts:

```json
{
  "namespace": "personal",
  "cypher": "MATCH (e:MemoryEntity {namespace:$namespace}) WHERE toLower(e.name) IN $names OPTIONAL MATCH p=(e)-[:HAS_FACT|TARGET*1..2]-(other) RETURN e,p",
  "parameters": {"names": ["alfred", "dailyai", "unearth"]}
}
```

Use `RETURN n`, `RETURN r`, or `RETURN p` (or lists/maps containing these graph
values). Scalar values and property-only projections do not create graph nodes.
An empty selection produces an empty-state image. All graph values displayed must
belong to the requested namespace and one of the five knowledge node labels;
operational cursors/revisions and audit nodes are outside this visualization.
`$namespace` and `$ns` are bound by the tool, overriding supplied parameter values.

Defaults are **300 nodes / 1,000 relationships**, with optional `max_nodes`
(up to 20,000) and `max_relationships` (up to 60,000). The defaults were lowered
from 10,000 / 30,000 on 2026-09-21 so that a large namespace renders within the
layout limit; raise them explicitly for a bigger view. Disconnected nodes are
included. Exceeding a node, relationship, row, or nested-value budget marks the
image and metadata as partial. Large views label the main entity hubs; selections
of at most 100 nodes show captions and relationship names. Captions are shortened
for the image, without changing stored facts. Image bounds are about 3200 × 2240
pixels; actual dimensions depend on the layout's aspect ratio.

## Query and rendering boundaries

A server started with a bearer token refuses custom Cypher (`-32001`) and hides
`cypher` and `parameters` from the tool schema. Render Cypher runs against the
whole database and only its output is filtered by namespace, so a query could
reveal counts or booleans about other namespaces through what gets drawn. A
token-bound server is a namespace boundary; a local server without a token is
not. The examples above therefore apply to local unbound use and to the CLI
(`graph-memory call memory_render ...`). A server runs at most two renders at a
time and answers `-32002` with `Retry-After` when busy.

Queries use the configured database connection and route to its writer, as recall
does, to avoid stale follower reads. This is a trusted operator query feature,
not a general-purpose Cypher endpoint or a database row-security mechanism.
Only namespace-matching graph nodes/edges are displayed; scalar query results and
source payloads are not exposed in the response. Use dedicated databases/roles
when isolating mutually untrusted tenants.

The tool rejects multiple statements, mutation keywords, procedure calls, imports,
and namespaced functions. Neo4j `EXPLAIN` must classify the statement as read-only
before execution. An explicit transaction is always rolled back. Database work is
limited to 15 seconds; layout to 45 seconds, after which the layout process
and anything it started are killed. Unsupported Cypher extensions should
be rewritten as ordinary `MATCH`/`WHERE`/`RETURN` queries. Custom queries should use
namespace filters and sensible path depths to avoid expensive global traversals.
The default view fetches only captions and topology, not full source transcripts.

Graphviz's [sfdp](https://graphviz.org/docs/layouts/sfdp/) performs the layout and
[PNG renderer](https://graphviz.org/docs/outputs/png/) creates the image. The Docker
image includes Graphviz and DejaVu fonts. For a host Python installation, install
Graphviz separately (`brew install graphviz` on macOS, or the `graphviz` package on
Debian); missing Graphviz produces an actionable tool error. No Python dependencies
were added. Captions are quoted as plain DOT strings, not executable HTML or paths.

The MCP result follows the [2026-07-28 image content contract](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#image-content):
one `image` content block (`image/png`, base64 data).
`structuredContent` contains only counts, dimensions, limits, and the render time.
There is no server-side image session, persistent screenshot file, or screenshot
cache. A client can save the returned bytes or display them directly in chat.
