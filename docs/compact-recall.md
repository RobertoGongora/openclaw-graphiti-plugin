# Concise recall and evidence on demand

The public MCP recall/latest tools return compact JSON by default. The Python
store and CLI retain their complete temporal projections, so this change does
not alter extraction, persisted facts, freshness decisions, or benchmark inputs.

```json
{"entity":"Atlas","question":"Which database does Atlas use?"}
```

`entity` identifies an entity by name/key. Optional `question` selects relevant
facts within its graph. This follows Context7's subject-plus-question interface;
it is deterministic lexical selection, not a second LLM generating an answer.
It does not resolve arbitrary natural-language prompts into entity names.
If vocabulary does not match, omit the question or use a more specific term.

The default response contains at most five facts/derived conclusions total.
Each fact has its ID, text, temporal category, date and source episode ID.
Identical facts in the same temporal category are grouped, with support counts.
Repeated events with different dates remain distinct. Different uncertain
wordings also remain distinct: a shorter later summary cannot replace a detailed
earlier report just because they name the same endpoints. Identical uncertain
reports expose `report_count` rather than an independent-support count, and
identical records in any category expose `source_sessions` when they come from
more than one conversation. Repetition across sessions can include memory-derived
echoes; it is never a promotion out of the uncertain category. `reported_at` is
the source message's time and orders comparable reports; it does not fill a
missing event date or prove that a later report corrected an earlier one. Dated
records still order by their event time first. A fact the user confirmed
with `memory_confirm` carries `confirmed_by_user: true`. Summary excerpts are capped
at 500 characters and explicitly marked when truncated. Plans, uncertain claims,
undated documents and conflicting claims never become current facts by formatting.

Questions rank individual facts by weighted lexical overlap, with conservative
word-form normalizations and a preference for measured outcomes when asking for
results. Both question search and entity recall use the same scorer. Named
subjects receive a bounded preference based on canonical names, not arbitrary
aliases. Longer summaries receive a bounded length penalty, with an allowance
for short factual statements. Topically matching records with stored validation
or user confirmation receive a bounded preference; repeated reports do not.
None of these signals changes a fact's evidence or uncertainty category.
The response echoes `question_terms`, the
words that ranked; when a question holds only stop words or words the entity's
own name already covers after normalization, the list is empty and the facts are
unranked, as if no question were given.
Partial matches remain available through
pagination instead of being discarded by a highest-score-only filter. Explicit
database/framework/language questions receive an extra bonus for matching
schema relationships, not merely an endpoint's kind: a validated framework
build event must not outrank the actual framework state on that basis alone.
Those schema roles receive the bonus only when the question has no other
matching topic words. For example, "which database?" benefits from the role;
"database disk size?" ranks lexical topic matches without the broad role bonus.
Matching an older value also selects other facts in the same genuine exclusive
state role, so asking about
an old database can surface its replacement. A role is a slot that at least two
facts of that subject and relation share; a slot only one fact carries is a
label, and the fact is grouped by its target instead. This relies on the existing entity
and relationship identities; it cannot repair misclassified or missing facts.
For plan/decision questions about one unambiguous entity, recall also considers
decisions attached to topics or decision nodes whose canonical name/key contains
that entity's name. Discovery is bounded to eight related entities and twelve
decision nodes; arbitrary aliases and recursive neighbor traversal do not expand
scope. A person's `decided` edge loads its decision, not every fact about the
person. Complete outgoing decision roles still resolve replacements/conflicts
before selection. Related facts compete within the same response limit and carry
their subject key; no extra entity list or separate decision section is returned.
Unquestioned recall and ordinary non-decision lookups retain their original scope.
This is conservative name/word matching, including a small rebuild/recreate
equivalence, not general semantic retrieval. Related decisions without a matching
canonical name and unsupported paraphrases can still be missed; see the
[ranking follow-ups](recall-ranking-follow-ups.md).
Historical rows are hidden by default; `include_history:true` makes them eligible.
Conflicts, source backlog counts, entity ambiguity, omitted-result counts and
snapshot revision remain visible. `status:"conflict"` means a disagreement
matches the question as strongly as the best result, so it leads the first page.
A weaker partial match does not change the status, but is never hidden:
`counts.conflicts_matching` counts the matching conflicting records, and
`conflict_fact_ids` lists them with the other side of each disagreement, up to
ten, ready for one `memory_evidence` call. Without a question every record ties,
so any conflict sets the status. `no_matching_facts` means selection found
nothing, not that the subject has no relevant real-world facts.

Use `offset` and the returned `next_offset` to retrieve more compact results.
Every call resolves the graph again. Check the revision between pages, or use
`known_at`/`at_change` for a fixed historical view. Derived conclusions have their
own availability counts and support IDs; the legacy full view without a question
exposes their full lists.

With a `question`, `detail:"full"` uses exactly the same ranking, total limit,
offset, history selection and `facts` envelope as compact, expanding each chosen
fact to its original record plus the same `report_count`/`support_count` and
`source_sessions` fields. Detail changes the amount of evidence returned,
not which question is answered. For the legacy unranked diagnostic projection,
omit `question` and pass `detail:"full", limit:30`; its limit remains per lane,
and offset/history filtering do not apply. The same full detail option
is available on `memory_latest`. Latest retains ties/conflicts/uncertainty before
applying its response limit; truncation cannot manufacture a single winner.

## Find the right entity

The connected MCP server supplies its configured namespace. Scoped tool schemas
omit that field, including inside `memory_ingest.transcript`. Explicit legacy
namespaces remain accepted only when they match the server scope; mismatches
are rejected. Unbound administrative servers still require explicit namespaces.
The `personal` and `transcripts` endpoints remain separate; no cross-graph search
or automatic cutover is implied.

Recall already matches names, aliases and substrings. When identity is unclear:

```json
{"query":"atlas","kind":"project"}
```

Call `memory_search_entities` with this input. It returns up to five matching
names, kinds, aliases and stable keys, with pagination. Exact keys/names/aliases
rank before substrings and matches covering all query words. This is lexical
lookup, not typo correction or semantic similarity. It does not merge identities
or silently choose between similarly named projects. Use the selected result's
`key` as recall's `entity`. The same historical cutoff options are supported.

Recall's old `query` parameter remains accepted for cached clients but is no
longer advertised. New clients see `entity` plus optional `question`. Search keeps
`query` because it searches for candidate entities instead of selecting one.

## Verify a fact

`memory_evidence` accepts one to ten IDs in a batch. The MCP default is compact;
internal Python/CLI requests retain full detail for compatibility.

```json
{"fact_ids":["ID_RETURNED_BY_RECALL"]}
```

Use `detail:"index"` for a claim/source inventory (240-character claim summaries,
source IDs and roles), `detail:"compact"` for up to three claim and three validation
excerpts per fact (400 characters each), or `detail:"full"` for all stored metadata.
Counts and truncation flags distinguish omitted material. A truncated quote is
called `quote_excerpt`, never `quote`; do not use an excerpt as an exact-source write.
An expansion call preserves the historical sequence when applicable. Full detail
returns exact conversational quotes separately from tool-validation quotes,
including message roles/timestamps and recorded
source URI when available. It reads durable graph evidence, not today's source
file. The fact may be historical or retracted; evidence retrieval does not assert
that it remains current. Missing IDs/sources are explicit. IDs from another
namespace are not returned. For historical recall, pass the same `known_at` or
`at_change` to evidence retrieval.

Each compact/full quote includes `source_context.kind`: for example `user_assertion`,
`assistant_report`, `memory_derived_report`, `documentation_lookup`,
`shell_output`, or `file_read`. These describe the recorded collection method,
not independent verification of the claim. Documentation lookup currently
recognizes Context7's `query-docs` and `get-library-docs`; unknown tools remain
`tool_output`. Context and memory-read labels take precedence over tool names.
In full detail, when its paired tool call is present in the episode, `source_context.tool_call`
includes its message ID, tool name, and up to 1,200 characters of recorded
arguments with `arguments_truncated`. Split source records also set that flag.
Captured artifact metadata and source gaps are preserved without duplicating
artifact contents. Missing calls are not reconstructed from today's files.

For example, a saved disk-growth note from a shell command reading `CLAUDE.md`
is shown as `shell_output` alongside the recorded command; an earlier Context7
result is `documentation_lookup`. A compound command's output is not attributed
to one particular file unless the source already supplies that attribution.
Neither label claims the external policy was checked during this recall.
These details are fetched on demand with `memory_evidence`; compact recall
does not load episode payloads or call a model to classify sources.

Evidence and entity search are also permitted by retrieval-only MCP servers.
The public catalog has twelve tools, including `memory_allow_alternatives` for
source-reviewed corrections of mistakenly exclusive slots. It preserves both
claims and their evidence; genuine exclusive conflicts must not be cleared.
Corrections must include the complete unretracted role history, including ended
records, so a partial correction cannot resurrect an earlier alternative.
Clients with cached catalogs may need to reconnect. Tool results remain JSON in
both the MCP text and structured-content representations.

Historical MCP requests are serialized across local session processes and HTTP
threads sharing a user/container. A competing request returns BUSY with retry
guidance; live requests remain available. Evidence and entity lookup selectively
retain checkpoint nodes while still validating every part and replayed delta.
Historical evidence resolves requested facts and their episodes at one pinned
sequence. General historical recall still reconstructs its complete dependency
graph; this change does not claim constant-memory historical recall.

## Validation

`tests/test_retrieval.py` covers state/plan separation, conflicts under response
limits, duplicate grouping, pagination, old-value questions, lexical misses,
explicit excerpts, source quotes, retractions, namespace isolation and historical
evidence. Integration tests run in the disposable database from
`compose.test.yaml` (port 37687), not the live imports.

`python -m evals.recall_regression PRIVATE_SNAPSHOT --output REPORT` replays
question-ranked selection on frozen projections with source-reviewed answer IDs;
see [evals](../evals/README.md#frozen-recall-regression).

`python -m evals.recall_payloads PRIVATE_SNAPSHOT.json` compares the old 30-per-lane
response with the default compact response on the same frozen graph records.
The snapshot is a list of `{query, question?, raw}` records obtained from complete
recall. Do not commit the private snapshot. The report stores its hash, counts,
JSON byte sizes and formatting times, never private facts or quotes. These are
payload measurements, not a golden retrieval-accuracy baseline or token counts.
Source content and model expectations are not changed by these checks.

Reference: [Context7 tool parameters](https://github.com/upstash/context7#available-tools).
