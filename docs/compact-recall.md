# Concise recall and evidence on demand

The public MCP recall/latest tools return compact JSON by default. The Python
store and CLI retain their complete temporal projections, so this change does
not alter extraction, persisted facts, freshness decisions, or benchmark inputs.

```json
{"namespace":"transcripts","query":"Atlas","question":"Which database does Atlas use?"}
```

`query` identifies an entity by name/key. Optional `question` selects relevant
facts within its graph. This follows Context7's subject-plus-question interface;
it is deterministic lexical selection, not a second LLM generating an answer.
It does not resolve arbitrary natural-language prompts into entity names.
If vocabulary does not match, omit the question or use a more specific term.

The default response contains at most five facts/derived conclusions total.
Each fact has its ID, text, temporal category, date and source episode ID.
Identical facts in the same temporal category are grouped, with support counts.
Repeated events with different dates remain distinct. Summary excerpts are capped
at 500 characters and explicitly marked when truncated. Plans, uncertain claims,
undated documents and conflicting claims never become current facts by formatting.

Questions select the strongest token matches. Matching an older value also
selects other facts in the same subject/relation/exclusive role, so asking about
an old database can surface its replacement. This relies on the existing entity
and relationship identities; it cannot repair misclassified or missing facts.
Historical rows are hidden by default; `include_history:true` makes them eligible.
Conflicts, source backlog counts, entity ambiguity, omitted-result counts and
snapshot revision remain visible. `no_matching_facts` means selection found
nothing, not that the subject has no relevant real-world facts.

Use `offset` and the returned `next_offset` to retrieve more compact results.
Every call resolves the graph again. Check the revision between pages, or use
`known_at`/`at_change` for a fixed historical view. Derived conclusions have their
own availability counts and support IDs; `detail:"full"` exposes their full lists.

For original records, call `memory_recall` with `detail:"full", limit:30`.
This is the legacy entity projection: compact-only `question`, `offset` and
`include_history` do not filter this diagnostic view. The same full detail option
is available on `memory_latest`. Latest retains ties/conflicts/uncertainty before
applying its response limit; truncation cannot manufacture a single winner.

## Verify a fact

`memory_evidence` has the intent description:

> Use when you need to verify a recalled fact or inspect the evidence behind it.

```json
{"namespace":"transcripts","fact_ids":["ID_RETURNED_BY_RECALL"]}
```

It accepts up to ten fact IDs, returns exact conversational quotes separately
from tool-validation quotes, and includes message roles/timestamps and recorded
source URI when available. It reads durable graph evidence, not today's source
file. The fact may be historical or retracted; evidence retrieval does not assert
that it remains current. Missing IDs/sources are explicit. IDs from another
namespace are not returned. For historical recall, pass the same `known_at` or
`at_change` to evidence retrieval.

This adds a seventh public tool, also permitted by retrieval-only MCP servers.
Clients with cached catalogs may need to reconnect. Tool results remain JSON in
both the MCP text and structured-content representations.

## Validation

`tests/test_retrieval.py` covers state/plan separation, conflicts under response
limits, duplicate grouping, pagination, old-value questions, lexical misses,
explicit excerpts, source quotes, retractions, namespace isolation and historical
evidence. Integration tests run in a disposable database, not the live imports.

`python -m evals.recall_payloads PRIVATE_SNAPSHOT.json` compares the old 30-per-lane
response with the default compact response on the same frozen graph records.
The snapshot is a list of `{query, question?, raw}` records obtained from complete
recall. Do not commit the private snapshot. The report stores its hash, counts,
JSON byte sizes and formatting times, never private facts or quotes. These are
payload measurements, not a golden retrieval-accuracy baseline or token counts.
Source content and model expectations are not changed by these checks.

Reference: [Context7 tool parameters](https://github.com/upstash/context7#available-tools).
