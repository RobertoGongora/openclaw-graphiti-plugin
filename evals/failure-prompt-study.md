# Failed-extraction prompt study — 2026-09-20

The live engine's repeated failures provided a diagnostic set: two episodes with
unvalidated assistant claims, one quote mismatch, and one schema failure. Selection
was deterministic (episode-ID order within each category), not a random estimate
of archive-wide quality. Inputs, entity context, and expected coverage concepts
were frozen before the first run. Three synthetic controls check supported user
state plus a plan, tool-verified assistant state, and tool-only input with no facts.

Every call used Terra Low, one CLI invocation, no correction retry, and at most
two simultaneous study calls. The production worker's correction retries make
its final success rate different from this first-attempt measurement. The study
made 47 model calls across all variants and reused baseline outputs where noted.
No study result was committed to the live graph. Private inputs and raw outputs
are in `.local/failure-prompts*-20260920/`, excluded from Git. Reports include full
prompt strings, hashes, per-attempt scores and timings without raw transcripts.

| Variant | Real-case trials | Contract passes | Contract + coverage passes |
| --- | ---: | ---: | ---: |
| Existing instructions, two runs per case | 8 | 0 | 0 |
| v1: explicit source decision procedure | 4 | 1 | 0 |
| v2: self-contained entity declarations and concrete detail | 4 | 4 | 2 |
| v3: complete report coverage and actor attribution guidance | 8 | 6 | 5 |
| v4: v3 plus opaque context text omitted from model input | 8 | 8 | 7 |

Every variant passed all three controls in its single control run. The model
therefore did not gain its score simply by producing no facts or making every
supported claim uncertain. v4's median real-case time was 56.91 seconds versus
33.44 for the baseline; the baseline's outputs were all rejected. These times
are not token counts, billable costs, or a subscription-savings estimate.

## Findings

The existing prompt says to reuse entity keys without explicitly stating that
reused entities must also be declared in the output. Both baseline repeats hit
missing-endpoint validation before reaching the evidence checks. The revised
prompt makes that self-contained output requirement explicit.

Source-specific uncertainty rules must precede generic date guidance. Merely
having a timestamp does not validate an assistant report, and a message labeled
`context` cannot become corroborating `tool_result` evidence based on its content.

Instructions alone remained unreliable on the longest episode. The model mined
opaque command output and then, on repetition, misattributed its quote to an
assistant message. In v4 the full transcript stays durable and is used unchanged
by the validator; only `context` message text is replaced in the model's input
view. Message IDs and metadata remain. Conversational claims, tool results, tool
calls, and memory read/write context remain unchanged. This is an experimental
input transformation, not deletion or reclassification of graph evidence.

## Release decision: not deployed

Contract success is not sufficient. The repeat still omitted a required timing
diagnosis. Manual review also found an inferred actor and an extra precise count
not explicit in their cited claims. These remain blocking semantic-quality
findings in the v4 report. The production instruction selector and worker input
path are unchanged; `graph_memory/extraction_policy.py` is an experimental
candidate used only by this study. The optional `CodexLLM(max_attempts=1)` exists
for measurement; production defaults remain two schema attempts.

The next gate should test claim-level completeness and attribution explicitly,
then use additional untouched failure episodes before rollout. This set is a
regression seed, not an approved golden baseline. Do not weaken the validator,
drop failed facts, or mark omitted episodes complete to improve the score.

## Reproduction

Use the frozen private directory to replay the same cases. Changing a prompt
requires a new directory with `revise`; do not overwrite prior results.

```sh
python -m evals.probes.failure_prompts snapshot .local/new-study
# Review captured claims and create expectations.json before running.
python -m evals.probes.failure_prompts run .local/new-study
python -m evals.probes.failure_prompts revise .local/new-candidate --from-directory .local/new-study
python -m evals.probes.failure_prompts run .local/new-candidate --variant candidate --omit-ineligible-context
python -m evals.probes.failure_prompts run .local/new-candidate --variant candidate --omit-ineligible-context --real-only --repeat 2
python -m evals.probes.failure_prompts report .local/new-candidate --from-directory .local/new-study
```

The result cache avoids spending another call on an existing case/variant/repeat.
Use a new repeat number for a genuine repeat. The snapshot reads the live graph
without staging, committing, or modifying any knowledge. Reports v1–v4 under
`evals/reports/20260920-failure-prompts-*.json` preserve unsuccessful iterations.
