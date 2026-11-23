# Rollout: the selected session-records-v1 writer prompt

A runbook for the writer prompt Rob selected: a one-session canary in a one-off
container first, then, as a separate decision, the transcripts worker.
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

| Variable | Default | One-off canary container | Shared transcripts worker |
| --- | --- | --- | --- |
| `MEMORY_WRITER_PROMPT` | unset: the app's instructions | `selected-v1` | unset (`TRANSCRIPT_WRITER_PROMPT` empty) |
| `MEMORY_MODEL` | `gpt-5.6-terra` (unchanged) | `gpt-6.1-terra` | as deployed |
| `MEMORY_REASONING_EFFORT` | `low` | `low` | as deployed |
| `MEMORY_LLM` | `codex` | `codex` (subscription login) | as deployed |

With `MEMORY_WRITER_PROMPT` set and any other provider, model or effort,
`configured_llm()` raises and the process does not start. No default changes: an
unset variable behaves exactly as before. The canary sets these only on its one-off
container. `TRANSCRIPT_WRITER_PROMPT`, `TRANSCRIPT_MODEL` and
`TRANSCRIPT_REASONING_EFFORT` switch the shared worker, which is
[widening](#widening), a separate decision. The personal stack reads
`MEMORY_WRITER_PROMPT` directly; leave it unset there.

Unlike the eval's single draws, production keeps the app's schema retry and its
one correction pass.

The image pins codex-cli 0.171.0 (`CODEX_VERSION`, one build argument for the
install, its version check and the image label), the version the eval and the
pilot ran. It is the CLI for all extraction, default model included.

## Revisions

A revision records the writer prompt it was created under (`writer`, absent for
the app's own). Building, comparing, validating and promoting it under another
writer prompt is refused like a model change ("Model settings changed"), in both
directions. A golden eval report records the writer prompt too, and `validate`
accepts only a report made under the worker's. Revisions and reports from before
this release carry none and stand for the app's prompt, so they keep working
under an unset variable and never pass for `selected-v1`.

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

## Before the canary

1. Suite, lint and typecheck on the release commit, against a disposable database
   (`make lint typecheck`, `MEMORY_TEST_NEO4J_URI=… uv run pytest -q`), `uv build`,
   and the image build.
2. Deploy the release with the variable unset. Held IDs exported privately and
   checked after the deploy, as in the
   [previous runbook](english-summaries-rollout.md#gates-before-the-deploy).
   Backup as in [operations](operations.md#backup).
3. Every feed carries a source key, or a one-off reading one root would find older
   feeds it cannot name and stage nothing (`feed_identity_blocked`):

   ```cypher
   MATCH (f:MemoryFeed {namespace:'transcripts'}) WHERE f.source_key IS NULL RETURN count(f)
   ```

   must be 0. Otherwise stamp them as in [operations](operations.md#feed-identity).

## Canary: one session, one one-off container

The shared worker is never recreated or reconfigured for the canary. It keeps the
environment it was deployed with, so every other session stays on the app's
prompt. One finished Claude session is staged and extracted by a one-off
container of the worker's own image that sees only that file. Afterwards the
daemon finds the file already fed. `tests/test_writer_canary.py` runs the same
sequence offline.

The worker is stopped, not recreated, while the one-off runs. A worker claims any
due episode in the namespace, and nothing limits it to some IDs, so a running
daemon would extract the canary's episodes with the app's prompt as soon as they
are staged (read from `feeds._tick` and `store.claim`). `stop` keeps the container
and its configuration; `start` resumes that same container. Do not use `up`,
which can recreate it.

Run from the transcripts deployment directory with its `.env` loaded into the
shell (the commands read `NEO4J_PASSWORD`, `TRANSCRIPT_AUTH_PATH`, `HOST_UID` and
`HOST_GID`, and print none of them).

1. Stop the worker and confirm the canary's settings are on no running process:

   ```sh
   dc() { docker compose -f compose.transcripts.yaml "$@"; }
   dc stop worker
   WORKER=$(dc ps -aq worker)
   docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$WORKER" \
     | grep -E '^MEMORY_(MODEL|REASONING_EFFORT|WRITER_PROMPT)='
   ```

   `MEMORY_WRITER_PROMPT=` must be empty. Keep this output to compare in step 8.
2. Choose one finished Claude session the graph does not hold, small enough to
   stage in one pass. `REL` is its path below the Claude root, as in
   `-Users-rob/SESSION.jsonl`. Neither its key nor its file name is known:

   ```cypher
   MATCH (f:MemoryFeed {namespace:'transcripts'})
   WHERE f.source_key = 'claude:' + $rel OR f.source_uri ENDS WITH '/' + $name
   RETURN f.id
   ```

   must return nothing. No episode carries the selected writer yet:
   `MATCH (e:MemoryEpisode {namespace:'transcripts'}) WHERE e.model_info CONTAINS
   '"writer": "selected-v1"' RETURN count(e)` is 0.
3. Stage it alone, at its original relative path, byte for byte with its times, in
   a new private directory outside every mounted root:

   ```sh
   STAGE=$(mktemp -d)/claude
   mkdir -p "$STAGE/$(dirname "$REL")"
   cp -p "$SOURCE_FILE" "$STAGE/$REL"
   ```

   Mounted as `/sessions/claude`, it gets the label, key (`claude:$REL`) and
   stored path the daemon gives the same file. The historical Claude and Codex
   directories are not mounted, so the one-off can read no other session.
4. The exact image the worker runs, and its network:

   ```sh
   IMAGE=$(docker inspect --format '{{.Image}}' "$WORKER")
   NET=$(docker inspect --format '{{range $n, $_ := .NetworkSettings.Networks}}{{$n}}{{end}}' "$WORKER")
   canary() {
     docker run --rm --read-only --tmpfs /tmp --cap-drop ALL \
       --security-opt no-new-privileges --user "$HOST_UID:$HOST_GID" --network "$NET" \
       -e NEO4J_URI=bolt://neo4j:7687 -e NEO4J_PASSWORD -e MEMORY_NAMESPACE=transcripts \
       -e HOME=/home/memory -e MEMORY_LLM=codex -e MEMORY_MODEL=gpt-6.1-terra \
       -e MEMORY_REASONING_EFFORT=low -e MEMORY_WRITER_PROMPT=selected-v1 \
       -v "$STAGE:/sessions/claude:ro" -v "$TRANSCRIPT_AUTH_PATH:/home/memory/.codex" \
       "$@"
   }
   canary --entrypoint /usr/local/bin/codex "$IMAGE" --version
   canary --entrypoint python "$IMAGE" -I -c "from graph_memory.models import Transcript; from graph_memory.llm import extraction_instructions as x; import hashlib; t=Transcript(namespace='x',source_id='s',session_id='s',source_format='session-records-v1',messages=[{'id':'u','role':'user','source_type':'user_assertion','content':'x'}]); print(hashlib.sha256(x(t,'selected-v1').encode()).hexdigest())"
   ```

   They print `codex-cli 0.171.0` and `f154a2e3…`.
5. Stage the one file:

   ```sh
   canary "$IMAGE" --namespace transcripts follow --once --source-records /sessions/claude
   ```

   The output has one feed with `"caught_up": true`; its receipts' `episode_id`s are
   the allowed IDs. Keep them privately. `"caught_up": false` means a longer
   file: run it again and add the new receipts. An empty `feeds` list staged
   nothing: either the due queue is at `MEMORY_INTAKE_QUEUE` (the one-off may be
   given a higher value; it bounds only this file), or the output names
   `feed_identity_blocked` or `feed_identity_refused`, and the canary stops.
6. Extract the allowed IDs, and only those:

   ```sh
   for id in $ALLOWED; do
     canary "$IMAGE" --namespace transcripts call memory_extract \
       "{\"namespace\":\"transcripts\",\"episode_id\":\"$id\"}"
   done
   ```

   Each run leases its episode first; a held or leased one is refused without a
   model call.
7. Read the result:

   ```cypher
   MATCH (e:MemoryEpisode {namespace:'transcripts'})
   WHERE e.model_info CONTAINS '"writer": "selected-v1"'
   OPTIONAL MATCH (f:MemoryFact)-[:SUPPORTED_BY]->(e)
   RETURN e.id, e.status, e.model_info, count(f) AS facts ORDER BY e.id
   ```

   Exactly the allowed IDs, each `complete`. Call `memory_evidence` with the fact
   IDs and `"detail":"full"` and read them beside the session: English
   summaries, literal quotes, done fixes `active` (the eval saw this prompt
   hedge some to `uncertain`), no proposal stored as done, no resolved issue
   left open.
8. Start the same worker and check it is unchanged:

   ```sh
   dc start worker
   ```

   Its environment matches step 1, the held IDs are still held, and when the
   session is in the daemon's Claude root (at `REL`, same bytes) its feed keeps
   its message count and no new episode appears for it: the daemon resolves the
   file to the canary's feed by key and its cursor is already at the end. A
   session that continues after the canary has its continuation staged on the
   app's prompt; that is why the session must be finished.
9. Delete `$STAGE`: it holds a real transcript.

Stop on any of: a refusal to start, `model_unavailable` or authentication errors,
an allowed ID failed or quarantined, an episode outside the allowed IDs with the
selected writer, a stored fact the source does not support. Stopping is step 8,
then step 9.

## Rollback

The canary changes no configuration: the worker never had the variable, and the
one-off container is gone when it exits. If the canary stops before step 8, start
the worker (step 8); it is the container deployed with the release. No data
migration: the canary's facts stay, identifiable by
`model_info CONTAINS '"writer": "selected-v1"'`, and can be reviewed or retracted
explicitly. Their cached extractions are not reused under the app's prompt,
because their model info differs.

## Widening

Switching the shared worker is a separate, explicit decision after a passing
canary: set `TRANSCRIPT_WRITER_PROMPT=selected-v1`, `TRANSCRIPT_MODEL=gpt-6.1-terra`
and `TRANSCRIPT_REASONING_EFFORT=low` together and recreate the worker, which then
applies the prompt to every new session. It must start; a refusal names the
mismatched setting. Its rollback is unsetting `TRANSCRIPT_WRITER_PROMPT`, setting
`TRANSCRIPT_MODEL` back (or unsetting it for `gpt-5.6-terra`) and recreating the
worker.
