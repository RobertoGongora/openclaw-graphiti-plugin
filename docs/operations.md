# Operations runbook

Practical steps for running the two Compose stacks. For the rules behind the
behaviour described here see [ingestion pipeline](ingestion-pipeline.md).

The examples use the transcripts stack. Set a shell alias first:

```sh
alias gm='docker compose -f compose.transcripts.yaml'
```

For the personal stack use plain `docker compose` and namespace `personal`.

## Configuration

`.env.example` is the reference for every variable that the Compose files read.
Copy it to `.env`, which is git-ignored. Both Compose files read the same `.env`.
Variables prefixed `TRANSCRIPT_` belong to `compose.transcripts.yaml` only.

Neo4j requires a password. Both stacks refuse to start without `NEO4J_PASSWORD`,
and the transcripts stack also refuses to start without `GRAPH_MEMORY_TAG`.

**Note.** `MEMORY_INTAKE_FILES` is read by the daemon, but neither Compose file
passes it to the worker container yet. Setting it in `.env` has no effect until
the worker's `environment` block lists it.

| Stack | Bolt | MCP | Browser (profile `browser`) |
| --- | --- | --- | --- |
| Personal (`compose.yaml`) | 127.0.0.1:17687 | 127.0.0.1:8765 | 127.0.0.1:17474 |
| Transcripts (`compose.transcripts.yaml`) | 127.0.0.1:27687 | 127.0.0.1:8766 | 127.0.0.1:27474 |

The Neo4j Browser port is not published by default. Start it on request and stop
it when you are done:

```sh
gm --profile browser up -d neo4j-browser
gm --profile browser stop neo4j-browser
```

## Deploy

The deployment is a tagged image plus a Compose file and its `.env`. Keep the
deployed Compose file and `.env` outside the working tree, so that edits in a
checkout cannot change what is running.

```sh
make image                      # builds graph-memory:<short commit>
graph-memory --version          # package version and engine identity of a build
```

1. Run the tests against the disposable database (see [Testing](#testing)).
2. Build the image and note its tag.
3. Take a backup if the release changes the journal format or the engine (see
   [Backup](#backup)).
4. Stop every writer that runs the old code: `gm stop worker mcp inventory`.
   The worker finishes active jobs first. Its stop grace period is 45 minutes.
5. Set `GRAPH_MEMORY_TAG` in `.env` to the new tag.
6. Start: `gm up -d`. Check `gm ps` until every service is `healthy`.
7. Read the first lines of the worker log. `daemon_start` shows the engine
   identity, the settings in force and how many leases were reclaimed.

Stop the old code before the first new write. A release that changes the journal
format migrates the journal on its first write. From then on an older process
fails its own journal check and cannot write. An old MCP server left running
would start refusing `memory_ingest`, `memory_retract` and `memory_merge`.

## Rollback

Set `GRAPH_MEMORY_TAG` back to the previous tag and run `gm up -d`. This is safe
when the release changed only operational code.

Two things are not reversible by switching the tag:

- **Journal format.** After the first write by a release that migrated the
  journal, older images cannot write to that namespace. Roll forward, or restore
  the backup taken before the deploy and lose the writes made since.
- **Engine identity.** A new engine identity releases quarantined episodes and
  ignores cached extractions made by the old one. Returning to the old tag
  restores the old identity, but episodes that the new engine already completed
  stay completed.

Never use `down -v` on a stack you want to keep. It deletes the database volume.

## Health

```sh
gm ps
gm exec worker graph-memory health --role worker
gm exec mcp graph-memory health --role mcp
gm exec inventory graph-memory health --role inventory
```

`health` prints one JSON line and exits 0 or 1. It does not create schema.

| Role | Healthy when |
| --- | --- |
| `worker` | The worker heartbeat is younger than six scan intervals, and at least 180 s |
| `inventory` | The last inventory finished less than three refresh intervals plus 120 s ago |
| `mcp` | A `ping` to the local HTTP endpoint returns 200 |

The daemon writes the heartbeat once per scan loop to a `MemoryWorker` node. It
holds the worker count, the engine identity, the process id and whether the
provider breaker is open. `memory_status` returns it under `workers`, with
`alive`, `same_engine` and `provider_unavailable`. `same_engine: false` means the
MCP server and the worker run different engine code.

## Reading the worker log

```sh
gm logs --since 1h worker
```

Every line is one JSON event with a `ts` field in Unix seconds.

| Event | Fields that matter |
| --- | --- |
| `daemon_start` | `engine`, `workers`, `settings`, `reclaimed_leases` |
| `bank_scan` | `files`, `changed_files`, `staged`, `existing`, `failures` |
| `transcript_scan` | `feeds` (files that staged work), `seconds` |
| `processed` | `status`, `timings`, `model_calls`, `cached`, `skipped`, `claim_seconds`, and on failure `diagnostic`, `failed_attempts`, `retry_after`, `quarantined`, `validation_failures` |
| `provider_unavailable`, `provider_recovered` | `open`, `reason`, `retry_in` |
| `worker_error` | `error`, `diagnostic` |
| `draining` | The daemon is finishing active jobs |
| `daemon_exit` | `reason`, `max_rss` (kilobytes on Linux, bytes on macOS) |

`timings` holds seconds per stage, summed over the passes of one run. Time spent
waiting for a database lock counts in the stage where the wait happened.

| Key | What it covers | If it is large |
| --- | --- | --- |
| `prepare` | Reading the episode and the graph context | Neo4j reads are slow. Check the page cache |
| `model` | Model calls, including correction calls | Provider latency. Normal variance is wide |
| `validation` | Schema and quote checks | Rarely significant |
| `checkpoint` | Saving the validated extraction on the episode | Lock contention or a slow database |
| `commit` | Graph write plus journal entry | The journal is doing a full capture or an audit, or writers are queued on the namespace lock |
| `claim_seconds` | Winning the lease | Lock contention between workers |

`model_calls` counts extraction passes (1 or 2), not CLI calls. `cached: true`
means a stored extraction was reused. `skipped: no_claim_in_focus` means the
episode could not yield facts and no model was called.

Throughput and percentiles for the last hour:

```sh
gm logs --since 1h worker | grep -o '{"event": "processed".*' \
  | jq -s '[.[] | select(.status=="complete")] as $ok
      | {completed: ($ok|length),
         model_p50: ($ok | map(.timings.model // 0) | sort | .[length/2|floor]),
         commit_p50: ($ok | map(.timings.commit // 0) | sort | .[length/2|floor])}'
```

**A healthy hour** on the local transcripts deployment with 8 workers completes
about 650 episodes. Commit p50 is about 0.02 s. Model p50 is about 7 s and p90
about 70 s. These were measured on 2026-09-20 and 2026-09-21 with Terra at low
effort. Use them as a reference point for this machine, not as a guarantee.

Reading a slow hour:

- Commit times of seconds instead of hundredths mean the journal is reading the
  whole graph. Check that `MEMORY_JOURNAL_AUDIT` is not `1`, and that nobody is
  running `history verify` or `history checkpoint`.
- `transcript_scan.seconds` near 120 means the scan is hitting its time limit.
  After a restart this is expected for a few scans while files are confirmed.
- No `processed` events and no `provider_unavailable` means the queue is empty
  or everything is waiting for a retry time. Check `memory_status.processing`.
- Many `processed` failures with `evidence_quote_mismatch` point at extraction
  quality, not capacity. Adding workers will not help.

## Provider outage

When the model provider fails, the breaker opens. The log shows
`provider_unavailable` with a `reason`, workers stop claiming episodes and
transcript staging pauses. Episodes are not charged for the outage. The daemon
probes with one episode per cooldown, starting at 60 s and doubling to at most
900 s, and logs `provider_recovered` when a probe succeeds.

| Reason | What to do |
| --- | --- |
| `usage_limit` | Wait for the window to reset, or add credit |
| `authentication` | Log in again: `gm run --rm --no-deps --entrypoint codex worker login --device-auth` |
| `model_unavailable` | Check `TRANSCRIPT_MODEL` or `MEMORY_MODEL` against what the account can use |
| `rate_limit`, `network`, `timeout`, `unknown` | Usually passes without action. Check connectivity if it lasts |
| `journal_state_mismatch` | Not a provider problem. See [Journal maintenance](#journal-maintenance) |

No restart is needed after the cause is fixed. The next probe closes the breaker.
A restart is harmless and makes the first probe immediate.

## Journal maintenance

```sh
gm exec worker graph-memory --namespace transcripts history list --limit 20
gm exec worker graph-memory --namespace transcripts history verify
gm exec worker graph-memory --namespace transcripts history checkpoint
```

**Verify in a window.** `history verify` holds the namespace lock, reads every
journal event and captures the whole graph. On a large namespace it pauses all
writers and needs memory in proportion to the graph. Stop the worker first and
run it when nobody is waiting on ingestion. The streamed check `verify_live`
exists in the engine but has no CLI command. The sampled audit
(`MEMORY_JOURNAL_AUDIT`) runs the same streamed comparison during normal writes.

**Audit cadence.** The transcripts stack defaults to `MEMORY_JOURNAL_AUDIT=503`.
Every 503rd journal write streams the live graph and compares its hash with the
journal head. A lower number finds an untracked write sooner and costs more.
`1` checks every write and is meant for tests. `0` turns the audit off.

**Checkpoint cadence.** Historical reads replay from the latest checkpoint, so
their cost grows with the number of changes since then. A checkpoint embeds the
full state in one event. On the transcripts graph that event is hundreds of
megabytes, which is why `TRANSCRIPT_NEO4J_TX_MEMORY_MAX` must stay at 1g or more.
Take a checkpoint after a bulk import finishes and before an upgrade, with the
worker stopped. Do not schedule it frequently. Each one adds its full size to
the store for good.

**Recovering from a journal mismatch.** The message is "Graph differs from its
journal; investigate an untracked write before continuing", reason code
`journal_state_mismatch`. Something changed journaled nodes outside the engine:
manual Cypher, an older image, or a restore of part of the data.

1. Stop the worker and the MCP server.
2. Find the cause. `history list` shows the last recorded changes. Compare with
   what was run against the database since the last successful verify or audit.
3. If the untracked change was a mistake and you have a backup from before it,
   restore the backup.
4. If the live graph is the state you want to keep, record it as the new truth:

   ```sh
   gm run --rm worker --namespace transcripts history checkpoint --accept-live
   ```

   The checkpoint event records `accepted_untracked_state: true`.
5. Start the services and confirm that `processed` events resume.

Do not use `--accept-live` when you have not identified the change, when the
change is one you would undo if you could, or as a routine fix to make the
error go away. It makes the journal vouch for a state it never recorded.
History before that checkpoint stays readable, but the step from the previous
event to the checkpoint is not explained by any recorded change.

## Scaling workers and intake

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRANSCRIPT_WORKERS`, `MEMORY_WORKERS` | 8, 4 | Concurrent extraction workers, 1 to 16 |
| `MEMORY_INTAKE_QUEUE` | 32 | Due episodes at which transcript intake stops staging |
| `MEMORY_INTAKE_FILES` | 4 | Files with work that one scan may open (see the note under Configuration) |
| `MEMORY_LLM_TIMEOUT` | 420 | Seconds per model call. The lease is four timeouts plus 60 s |

The local deployment runs 8 transcript workers. Earlier allocations of 12 and 2
workers recorded in other documents are history.

Workers spend most of their time waiting for the model, so throughput follows
worker count until the provider's rate limits or the namespace lock intervene.
Commits are serialised per namespace. At a commit p50 of 0.02 s the lock is not
the limit with 8 workers.

Keep `MEMORY_INTAKE_QUEUE` at several times the worker count. If it is too low,
workers idle between scans, which run every 30 seconds. If it is too high, a
large archive is staged long before it can be extracted and the database grows
early for no benefit.

Run one daemon per namespace. A second daemon reclaims the first one's leases
at start, and both then work the same episodes.

The worker container has a 6 GB memory limit and `MALLOC_ARENA_MAX=2`. Check
`daemon_exit.max_rss` after raising the worker count.

## Sizing Neo4j

The transcripts store is about 27 GB on disk. About 24 GB of that is cold: full
snapshots that earlier journal versions embedded every 100 changes. Normal
operation never reads them. The hot set is a few GB.

Size the page cache for the hot set, not the store. The transcripts default is
2 GB. At 256 MB the database thrashed. Raising the page cache toward the store
size would cache data nobody reads.

| Variable | Default | Reason |
| --- | --- | --- |
| `TRANSCRIPT_NEO4J_PAGECACHE` | 2g | Covers the hot set |
| `TRANSCRIPT_NEO4J_HEAP_MAX` | 4g | Full-capture writes and checkpoints build large states in memory |
| `TRANSCRIPT_NEO4J_TX_MEMORY_MAX` | 1g | A checkpoint event is one string of hundreds of megabytes |
| `TRANSCRIPT_NEO4J_MEM_LIMIT` | 8g | Heap max plus page cache plus about 2 GB of JVM overhead |

Both stacks set a 60 s lock acquisition timeout and a 10 minute transaction
timeout, so one stuck lock holder cannot block every writer indefinitely.
Neo4j gets two minutes to stop. A shorter grace period ends in a kill during a
checkpoint and a recovery on the next start.

The personal stack uses a 512 MB heap, a 256 MB page cache and a 1,536 MB limit.

## Quarantine review

```sh
gm exec worker graph-memory --namespace transcripts call memory_status '{"namespace":"transcripts"}'
```

`processing.quarantined` is the number of episodes paused under the running
engine. To list them with their reasons, run this in Neo4j Browser or
`cypher-shell`:

```cypher
MATCH (e:MemoryEpisode {namespace:'transcripts'})
WHERE e.status <> 'complete' AND e.quarantine_engine IS NOT NULL
RETURN e.id, e.name, e.quarantine_reason, e.validation_failures, e.attempts
ORDER BY e.ingested_at;
```

| Reason | Meaning | Action |
| --- | --- | --- |
| `evidence_quote_mismatch`, `schema_validation`, other validation codes | Three runs failed validation | Usually wait for an engine change that fixes the prompt or the validator. Retry one by hand to confirm |
| `ambiguous_identity` | The extraction names an entity that matches several | Merge or disambiguate the entities, then retry |
| `extraction_conflict` | The episode was committed with a different extraction | Retract the wrong facts explicitly. Do not retry blindly |
| A validation code with fewer than three `validation_failures` | The cached extraction no longer validates against its source | `retry-quarantined` keeps the cached extraction, so the retry meets the same failure. It clears only when the engine identity or the model settings change |

Release one episode:

```sh
gm exec worker graph-memory --namespace transcripts retry-quarantined EPISODE_ID
```

This clears the quarantine mark, the attempts and the validation failures. It
keeps the rejected candidate and the feedback. It refuses an episode that holds
a live lease. An engine change releases every quarantined episode without
operator action.

## Backup

Neo4j Community cannot stop one database while the server runs, and
`neo4j-admin database dump` needs the database stopped. Stop the whole stack and
run the dump in a one-off container on the same volume.

```sh
gm stop worker inventory mcp
gm stop neo4j
docker run --rm \
  -v graph-memory-transcripts_source-v1:/data \
  -v "$PWD/backups":/backups \
  neo4j:5.26-community \
  neo4j-admin database dump neo4j --to-path=/backups
gm up -d
```

Restore into a stopped stack:

```sh
docker run --rm \
  -v graph-memory-transcripts_source-v1:/data \
  -v "$PWD/backups":/backups \
  neo4j:5.26-community \
  neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true
```

Use the volume name from `TRANSCRIPT_VOLUME` or `MEMORY_VOLUME`, and the same
Neo4j image as the stack. The dump holds full transcript text. Store it like the
source sessions. Keep the image tag that wrote the data next to the dump,
because an older image cannot write to a migrated journal.

## Secrets

- `.env` holds `NEO4J_PASSWORD` and the MCP bearer tokens. It is git-ignored.
  Generate values with `openssl rand -hex 32`.
- A Neo4j volume keeps the password it was first started with. Changing
  `NEO4J_PASSWORD` later does not change the stored password, and the services
  then fail to connect. Change it in Neo4j first, then in `.env`.
- The Codex login lives in the mounted auth directory (`TRANSCRIPT_AUTH_PATH`)
  or the `codex_auth` volume. The worker writes to it when tokens refresh.
- The model CLI receives a minimal environment. It does not see the database
  password or the MCP token.
- `claude-config` writes `${NEO4J_PASSWORD}` as a reference for the client to
  expand. It never copies the value. The files are mode 0600.
- The MCP server does not log requests, arguments or credentials. Failures log
  an exception type and a request id.
- The MCP port is bound to loopback. To reach it under another host name, add
  the name to `MEMORY_HTTP_HOSTS` or `TRANSCRIPT_MCP_HOSTS`. Do not publish the
  port on a public interface. The bearer token is a private-service option.

## Testing

Tests and evals wipe what they touch. Never point `MEMORY_TEST_NEO4J_URI` at
17687 or 27687. Those are the live graphs.

```sh
make test          # unit tests. Database tests skip themselves
make test-db       # starts compose.test.yaml on 127.0.0.1:37687, then runs everything
make test-db-down  # removes the test database
```

`compose.test.yaml` runs Neo4j without authentication on a tmpfs. It holds
fixtures only and publishes no HTTP port.

## CLI exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Unexpected error, a failed receipt in the output, or an unhealthy `health` check |
| 2 | Invalid input or arguments, or an engine refusal such as a journal mismatch |
| 69 | The database is unavailable or rejected the credentials |
| 130 | Interrupted |

Pass `--debug` to get a traceback instead of the one-line message.
