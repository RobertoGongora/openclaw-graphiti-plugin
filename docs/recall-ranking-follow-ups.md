# Recall ranking follow-ups

## Related decisions can be stored but missed by entity recall

Status: open. Observed 2026-09-24; preserve as a regression case before extending retrieval.

A plan can be attached to a service, while the user's decision to defer that plan is attached to a separate decision and topic. Recall scoped to the service then returns the plan without the deferral. The original user message and its extracted decision can both be present: this is a retrieval gap, not evidence that ingestion lost the decision.

A second failure compounds this: a question asking whether the user **deferred rebuilding** may not match a stored decision saying **not to recreate it for now**. Broadening entity scope alone does not solve the wording mismatch.

### Regression to freeze

Use a source-reviewed example with these distinct records:

1. A service has an assistant-reported plan to recreate its VM after exporting volumes.
2. A separate decision points to a VM-recreation topic and records the user's instruction to defer the work while retaining the option.
3. An unrelated decision about the same maintenance session provides a distractor.

Evaluate the exact service-scoped and cross-entity questions on the same fixed knowledge snapshot. Keep plan, deferral, user source, and assistant report separately identifiable. A retrieved procedure does not authorize execution.

### Acceptance criteria

- A question about whether to proceed retrieves the relevant deferral alongside the plan, even when their graph endpoints differ.
- Paraphrases such as defer/rebuild and not-now/recreate retrieve the same decision.
- The result retains the original evidence and temporal scope; later decisions do not leak into earlier snapshots.
- Related-entity expansion stays bounded and does not import unrelated decisions merely because they share generic words such as memory, backup, or task.
- Pagination, conflict handling, and compact/full consistency remain stable.
- Measure candidate coverage separately from reranker quality, plus payload growth and latency.

Candidate approaches: conservative entity/topic expansion and semantic candidate retrieval, evaluated before deciding whether a model reranker is needed. Do not rely solely on arbitrary entity aliases.

## Direct memory writes need clearer completion language

An unsourced `memory_ingest` write is retained as context only and cannot originate a fact. Agents should inspect `available_for_recall` and `context_only_message_ids`, check whether the original conversation already supplies the fact, and avoid claiming successful searchable recall from an ingestion receipt alone. Background processing is not a guarantee that any particular claim will be extracted.

When using direct writes with existing evidence, supply the verified source-message references and exact source content; do not invent a user message, source role, or timestamp for an agent-written paraphrase.

## Optional model reranking and language scope

Evaluate the largest locally practical Laya checkpoints against frozen candidate pools before enabling model reranking. Keep ingestion multilingual. A proposed initial rollout can restrict the optional reranker to English question/candidate text, with the deterministic ranker retained for Spanish, mixed-language, uncertain-language, timeout, and model-error cases. Do not silently remove candidates because their language is unsupported.

A language gate does not demonstrate ranking quality: English-only regression cases must still improve. Model relevance is not factual validation and must not promote uncertainty, override corrections, or supply execution authority. Measure candidate recall before model quality, and compare end-to-end latency rather than quoting single-model-forward-pass timings.
