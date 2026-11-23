# Rollout: the selected session-records-v1 writer prompt

A runbook for switching the transcripts worker to the writer prompt Rob selected.
Nothing here is run against production by a script; each gate is a person reading
a result. Deploying the code changes nothing until the variable below is set.

## What it is

`MEMORY_WRITER_PROMPT=selected-v1` makes session-records-v1 extraction send the
app's own instructions, two newlines, then the guidance in
`graph_memory/prompts/session-records-writer-v1.txt`, byte for byte: sha256
`f154a2e3910b880e8ad0f0921af6efb97e5894ab9f10e70342e1d6881a023d9c` of the whole
text (the guidance file alone is `c68ffb9c…`). It is the variant that won a frozen
local eval (Promptfoo, 6 dev and 3 blind holdout synthetic cases, one
`gpt-6.1-terra` / `low` draw per cell) and then one isolated real-session pilot.
Those are small samples: evidence for a canary, not a reliability estimate.

- Only session-records-v1. direct-mcp-v1, legacy and other formats, the caller
  provider (`memory_prepare`) and `compatible` keep their instructions.
- Static text. Nothing is fetched or generated at runtime: no other model, agent,
  eval tool or network call is involved, and there is no fallback prompt.
- The worker checks the composed text against the digest when it starts and
  refuses to start if either part changed. A later edit to the app's instructions
  therefore needs a new eval and a new digest before this can stay on.

## Environment

| Variable (transcripts stack) | Default | For the canary |
| --- | --- | --- |
| `TRANSCRIPT_WRITER_PROMPT` → `MEMORY_WRITER_PROMPT` | unset: the app's instructions | `selected-v1` |
| `TRANSCRIPT_MODEL` → `MEMORY_MODEL` | `gpt-5.6-terra` (unchanged) | `gpt-6.1-terra` |
| `TRANSCRIPT_REASONING_EFFORT` → `MEMORY_REASONING_EFFORT` | `low` | `low` |
| `TRANSCRIPT_LLM` → `MEMORY_LLM` | `codex` | `codex` (subscription login) |

Set the three together. With `MEMORY_WRITER_PROMPT` set and any other provider,
model or effort, `configured_llm()` raises and the worker does not start. The
personal stack reads `MEMORY_WRITER_PROMPT` directly; leave it unset there. No
default changes: an unset variable behaves exactly as before.

Unlike the eval's single draws, production keeps the app's schema retry and its
one correction pass.

## Engine and quarantine

The release changes the engine identity (5899353 `7cb95603…`, this release
`01b0733b…`; the guidance file is an engine file). Measured on this branch:

- **Quarantined episodes are not released by the new engine.** The worker's
  claim, `pending`, the tick query and synchronous extraction all require
  `quarantine_engine IS NULL`, whatever engine set it. Shown by
  `tests/test_quarantine_rollout.py` and, with this writer switched on,
  `tests/test_writer_prompt.py::test_a_quarantine_from_the_old_engine_stays_held_under_the_selected_writer`
  (zero CLI calls for a held episode). Only `retry-quarantined EPISODE_ID`
  releases one. No new guard was needed.
- Validated extractions cached under the old engine are not reused; those
  pending episodes are extracted again (one model call each).
- Retry budgets of failed, unquarantined episodes restart under the new engine.
- Switching the variable does not change the engine; the committed episode's
  `model_info` records `"writer": "selected-v1"`, and a cached extraction is only
  reused when its model info (provider, model, effort, writer) matches.
- **Rolling the code back** to a tag from before #206 releases quarantines again
  (see [the previous runbook](english-summaries-rollout.md)). Roll back with the
  variable instead.

## Before the switch

1. Suite, lint and typecheck on the release commit, against a disposable database
   (`make lint typecheck`, `MEMORY_TEST_NEO4J_URI=… uv run pytest -q`), `uv build`,
   and the image build. In the image:

   ```sh
   docker run --rm --entrypoint python IMAGE -I -c "from graph_memory.models import Transcript; from graph_memory.llm import extraction_instructions as x; import hashlib; t=Transcript(namespace='x',source_id='s',session_id='s',source_format='session-records-v1',messages=[{'id':'u','role':'user','source_type':'user_assertion','content':'x'}]); print(hashlib.sha256(x(t,'selected-v1').encode()).hexdigest())"
   ```

   prints `f154a2e3…`.
2. The image's Codex CLI (`CODEX_VERSION`, 0.154.0 in the Dockerfile) must serve
   `gpt-6.1-terra`. The eval and pilot ran codex-cli 0.171.0 on t3-dev; this was
   not checked with 0.154.0. An unavailable model is a provider outage: episodes
   are not charged, the breaker opens and work waits. Read the first processed
   event before going further.
3. Held IDs exported privately, as in the
   [previous runbook](english-summaries-rollout.md#gates-before-the-deploy).
4. Backup as in [operations](operations.md#backup).

## Canary (bounded)

Deploy the release with the variable unset first, and check the held IDs as
before. Then:

1. Set the three variables for the transcripts worker only and recreate that one
   service. It must start; a refusal names the mismatched setting.
2. Let a small, fixed number of new sessions through (for example the next 10
   episodes or 24 hours, whichever comes first) and read them by ID:

   ```cypher
   MATCH (e:MemoryEpisode {namespace:'transcripts'})
   WHERE e.completed_at >= $switched AND e.model_info CONTAINS '"writer": "selected-v1"'
   OPTIONAL MATCH (f:MemoryFact)-[:SUPPORTED_BY]->(e)
   RETURN e.id, e.model_info, count(f) AS facts ORDER BY e.completed_at
   ```

   For a few, read the facts beside their sources: English summaries, literal
   quotes, done fixes `active` (the eval saw this prompt hedge some to
   `uncertain`), no proposal stored as done, no resolved issue left open.
3. Stop and roll back on any of: the worker refusing to start, `model_unavailable`
   or authentication errors, quarantines rising above their level before the
   switch, a validation failure rate clearly above the old prompt's, or a stored
   fact the source does not support.
4. Widening beyond the canary is a separate, explicit decision.

## Rollback

Unset `TRANSCRIPT_WRITER_PROMPT` and set `TRANSCRIPT_MODEL` back (or unset it for
`gpt-5.6-terra`), then recreate the worker. No data migration: facts already
committed with the selected writer stay, identifiable by
`model_info CONTAINS '"writer": "selected-v1"'`, and can be reviewed or retracted
explicitly. Cached extractions from the canary are not reused by the old settings,
because their model info differs.
