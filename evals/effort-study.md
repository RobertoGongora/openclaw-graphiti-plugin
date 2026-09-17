# Luna reasoning-effort comparison

## 2026-09-17 real-bank failure comparison

Two frozen preparation packets were selected from real-bank failures: one rejected
for an undeclared relationship endpoint, the other for a non-verbatim evidence
quote. Each profile ran once against identical packet bytes and production
validation/correction logic, with no graph writes.

| Profile | Endpoint case | Quote case | Total elapsed |
| --- | --- | --- | --- |
| Luna medium | Rejected, 219.831 s | Rejected, 330.159 s | 549.990 s |
| Luna high | Rejected, 434.761 s | Rejected, 474.015 s | 908.776 s |
| Terra low | Passed, 39.770 s (3 facts) | Passed, 69.823 s (6 facts) | 109.593 s |

Fast mode was not requested for any profile. The engine, prompts, schema, evidence
checks, and correction policy were unchanged. Higher Luna effort did not resolve
either selected failure. Terra passed both schema/evidence checks, but these probes
do not establish semantic completeness, graph identity resolution, or retrieval
accuracy. They are two selected failures, not a representative model benchmark.
Timings include corrections and CLI overhead with uncontrolled concurrent load;
Luna runs used reversed source order, and Terra ran later.

Sanitized results and private-report digests are in
[the model probe report](baselines/2026-09-17-model-probe.json). Private source
packets and reports stay under ignored `.local/high-trial/`.

The live medium window captured 2 successful attempts and 32 rejected attempts;
the subsequent high window captured 1 success and 7 rejections before shutdown.
Their durations and queue contents differ, so these are operational observations,
not comparable failure-rate estimates. Shutdown interruptions are excluded.
Private logs remain in the deployment's `high-effort-trial/` directory.

A proposed two-pass Luna design would separate source reading from structured
relationship construction. It is not implemented or evaluated. Any such experiment
must preserve source message IDs and verbatim quotes between passes, validate
against the original transcript, and compare factual coverage as well as rejection
rate and total latency. A formatting pass alone may not repair missing entities or
inaccurate evidence.

## Terra deployment trial

Terra low subsequently passed all 50 deterministic tests and all six fixed model
scenarios (one repetition), including the dreaming case, in a separate temporary
Neo4j container. The native-memory versus MCP Claude A/B was not rerun. No golden
baseline was approved. Rob subsequently chose to retain Terra low, with fast mode
off. Code, Compose, and setup defaults now match that choice. The deployed image
stays pinned with explicit Terra/low configuration; changing repository defaults
does not require interrupting ingestion or rebuilding historical facts.

The local worker was switched to four Terra/low consumers with fast mode off.
Three remaining Luna calls were cancelled during graceful drain; normal failure
cleanup preserved their source jobs for retry. The deployment image and engine
fingerprint remain unchanged. Journal integrity was verified through change 119
before and after the switch, with 2,264 knowledge records preserved. This is an operational trial, not a conclusion that Terra has
solved all real-bank extraction failures.

## Earlier default selection

The earlier selected default was **`gpt-5.6-luna` with `medium` effort**. Both lower settings
passed the fixed suite; medium also passed the real-note retrieval comparison
on the schema-aligned engine. Higher effort did not consistently improve the
observed outcomes. This is a small local trial, not a general model ranking.

## Same-engine comparisons

Six fixed scenarios were repeated twice per setting. The Atlas scenario includes
an actual dreaming pass. Each run used isolated Neo4j namespaces. The real-note
comparison used the same source bytes in independent Claude sessions: native
memory enabled versus native memory disabled with retrieval-only MCP access.

Before aligning the exported schema with the existing event-time validator:

| Effort | Model cases | Suite elapsed | Extra suite calls | Real-note A/B |
| --- | --- | --- | --- | --- |
| Extra High | 12/12 | Not instrumented | Not instrumented | Extraction rejected: undated events |
| High | 12/12 | 5:00 | 0 | Passed |
| Medium | 12/12 | 4:11 | 2 | Extraction rejected: undated events |

Extra High was still running after 20 minutes and subsequently failed validation.
No invalid source facts were published. The same time rule existed in Python but
was missing from the model-facing JSON Schema. We expressed its valid combinations
in the schema without relaxing runtime validation or changing golden expectations.

After that schema change, on the same engine for both efforts:

| Effort | Model cases | Suite elapsed | Extra suite calls | Dense-note model time | Real-note A/B |
| --- | --- | --- | --- | --- | --- |
| High | 12/12 | 5:04 | 1 | 5:46, two calls | Extraction rejected: non-exact quote |
| Medium | 12/12 | 4:02 | 1 | 2:16, two calls | Passed |

The individual dreams averaged 11.3 seconds on high and 10.3 seconds on medium;
all four passed. Dense transcript extraction and corrections dominated latency.
The chosen medium configuration received a separate final release-gate run;
see [executed validation](../docs/validation.md).

## Interpretation and evidence

The schema fix removed one shared failure mode. It does not establish perfect
extraction reliability: quote grounding still needs validation and bounded retry.
Failed outputs stayed isolated, and the failed trials remain in
[the comparison report](baselines/effort-comparison.json). The baseline is a
review candidate, not an automatically accepted golden standard.

Model-call times include corrections and CLI overhead. Two effort jobs ran in
parallel, and service load was not controlled; treat the timing differences as
observations. The dense-note column measures Luna calls only, excluding Claude
answering and database work. There was one real-note trial per setting per engine;
Extra High was not rerun after the schema change. No token-cost comparison was made.

Private reports and full tool traces remain under ignored `.local/`. The checked-in
report contains aggregates and report digests, without private source excerpts.

## Repeat the comparison

Use the same checkout, explicit isolated Neo4j endpoint, and source snapshot:

```sh
export MEMORY_TEST_NEO4J_URI=bolt://127.0.0.1:27687 # separately started test instance
export MEMORY_LLM=codex
# Repeat with high and medium, keeping separate reports.
export MEMORY_REASONING_EFFORT=medium
uv run python -m evals.run --runs 2 --output .local/medium-suite.json
uv run python -m evals.ab --memory-dir /path/to/focused-memory-snapshot \
  --output .local/medium-ab.json
```

The schema implementation follows Pydantic's
[JSON Schema customization API](https://docs.pydantic.dev/latest/concepts/json_schema/#implementing-__get_pydantic_json_schema__)
and the documented nested `anyOf` support in
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
Runtime validation remains the final authority for every provider and MCP caller.
