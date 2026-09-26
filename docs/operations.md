# Operations runbook

Practical steps for running the two Compose stacks. For the rules behind the
behaviour described here see [ingestion pipeline](ingestion-pipeline.md).

The examples use the transcripts stack. Set a shell alias first:

```sh
alias gm='docker compose --env-file /path/outside/repo/transcripts.env -f compose.transcripts.yaml'
```

For the personal stack use `compose.yaml`, its own env file and namespace
`personal`.

## Configuration

`.env.example` is the reference for every variable that the Compose files read.
For a trial, copy it to `.env`, which is git-ignored. For a deployment keep one
env file per stack outside the repository and pass it with `--env-file`. A
checkout, a branch switch or a `git clean` then cannot change or remove what a
running stack depends on, and the two stacks cannot pick up each other's values.
Variables prefixed `TRANSCRIPT_` belong to `compose.transcripts.yaml` only.

Neo4j requires a password. The transcripts stack refuses to start without
`NEO4J_PASSWORD`, `TRANSCRIPT_MCP_TOKEN` and `GRAPH_MEMORY_TAG`. The personal stack
falls back to the password and token `graph-memory`, and its services then stop
with instructions until both are changed; see [Secrets](#secrets).

| Stack | Bolt | MCP | Browser (profile `browser`) |
| --- | --- | --- | --- |
| Personal (`compose.yaml`) | 127.0.0.1:17687 | 127.0.0.1:8765 | 127.0.0.1:17474 |
| Transcripts (`compose.transcripts.yaml`) | 127.0.0.1:27687 | 127.0.0.1:8766 | 127.0.0.1:27474 |

The Neo4j Browser port is not published by default. A plain `up` never starts
it. It needs the `browser` profile. Start it on request and stop it when you are
done:

```sh
gm --profile browser up -d neo4j-browser
gm --profile browser stop neo4j-browser
```

## Deploy

The deployment is a tagged image plus a Compose file and its env file, both kept
outside the working tree so that edits in a checkout cannot change what is
running.

```sh
make image                      # builds graph-memory:<short commit>
graph-memory --version          # package version and engine identity of a build
```

1. Run the tests against the disposable database (see [Testing](#testing)).
2. Build the image and note its tag.
3. Stop every writer that runs the old code: `gm stop worker mcp inventory`.
   The worker finishes active jobs first. Its stop grace period is 45 minutes.
4. Take a backup if the release changes the journal format, the engine or the
   Neo4j configuration (see [Backup](#backup)).
5. If the Neo4j service definition changed, stop the database yourself with a
   long timeout before Compose recreates it:

   ```sh
   gm stop -t 120 neo4j
   ```

   Compose stops the old container with the old container's settings. A
   container created before the two minute grace period was added still has
   Docker's 10 second default. That ends in a kill during a Neo4j checkpoint and
   a recovery on the next start.
6. Set `GRAPH_MEMORY_TAG` in the env file to the new tag.
7. Bring services up by name, in order. Do not run a bare `gm up -d`, which
   starts the worker along with everything else.

   ```sh
   gm up -d neo4j
   gm up -d mcp inventory
   gm ps                         # wait for healthy
   gm exec mcp graph-memory --namespace transcripts history verify-live
   ```

8. Start the worker as an explicit step, once the checks above pass:

   ```sh
   gm up -d worker
   ```

9. Read the first lines of the worker log. `daemon_start` shows the engine
   identity, the settings in force and how many leases were reclaimed.

Stop the old code before the first new write. A release that changes the journal
format migrates the journal on its first write. From then on an older process
fails its own journal check and cannot write. An old MCP server left running
would start refusing `memory_ingest`, `memory_retract` and `memory_merge`. The
worker is the service that writes without being asked, which is why it starts
last and by name.

## Deploying the reference journal, name lookup and feed identity release

This release changes the journal format, adds the indexed name lookup and
changes how transcript files are identified. Follow the general steps above with
this order. Do not skip or reorder steps.

1. Stop every writer on older code: `gm stop worker mcp inventory`. An older
   writer after step 2 would be refused by the journal, and an older writer after
   step 4 would add entities that the name lookup cannot find.
2. Deploy the new tag with the transcript mounts unchanged: the same host
   directories at the same container paths (`/sessions/claude`,
   `/sessions/codex`). Feeds staged before this release are stored under those
   absolute paths, and the first scan names them from the paths. Change mounts
   in a later deploy, never in this one.
3. Bring up `neo4j`, then `mcp` and `inventory`, then the worker, by name. The
   first transcript scan must log `feed_identity` with `unmatched: 0` and
   `conflicts: 0`:

   ```sh
   gm logs worker | grep -E 'feed_identity|feed_identity_blocked'
   ```

   If it logs `feed_identity_blocked` instead, nothing is being staged. See
   [Feed identity](#feed-identity). Extraction of the existing queue continues
   meanwhile.
4. Rebuild the name lookup. It takes seconds:

   ```sh
   gm exec worker graph-memory --namespace transcripts aliases rebuild
   ```

   Until it has run, lookups use the older list scans.
   `memory_status.entity_names.indexed` is `true` afterwards, and `lookup_nodes`
   matches `listed_names`.
5. Take one checkpoint by hand. Expect 20 to 40 seconds on a large namespace,
   during which writers wait:

   ```sh
   gm exec worker graph-memory --namespace transcripts history checkpoint
   ```

   Until a version 3 checkpoint exists, every historical read loads the old
   checkpoint, which is one string of several hundred megabytes.
6. Run the routine check:

   ```sh
   gm exec worker graph-memory --namespace transcripts history verify-live
   ```

Irreversible in this release:

- Older engines can never write to the namespace again. The first write by the
  new code puts a `v3:` prefix on the journal head, and an older engine refuses
  to write when it sees it.
- Feed keys, once stamped, are the identity of every transcript file. They were
  derived from the labels and roots in force at the first scan.

## Rollback

Set `GRAPH_MEMORY_TAG` back to the previous tag and bring the services up by
name again. This is safe when the release changed only operational code.

After a release that fenced the journal, switching the tag back gives a stack
that answers current recall but cannot write. Ingest, retract, confirm and merge
are refused, the worker reports a journal mismatch, and historical reads fail on
version 3 events. Rollback then means restoring the backup taken before the
deploy and losing the writes made since, or rolling forward with a fix.

## What is irreversible

- **Journal format.** After the first write by the current code, the journal head
  is fenced and older images can never write to that namespace again. Roll
  forward, or restore the backup taken before the deploy and lose the writes made
  since.
- **Feed keys.** Once a feed is stamped with its source key, that key names the
  file for good. Renaming a label, or adding a root below an existing one,
  changes the key that files would get and makes known files look new. See
  [Feed identity](#feed-identity).
- **Completed episodes.** A new engine identity releases quarantined episodes
  and ignores cached extractions made by the old one. Returning to the old tag
  restores the old identity, but episodes that the new engine already completed
  stay completed with the facts it extracted.
- **The stored Neo4j password.** The volume keeps the password it was first
  started with. See [Secrets](#secrets).
- **`history checkpoint --accept-live`.** The journal then vouches for a state it
  never recorded. See [Journal maintenance](#journal-maintenance).
- **`down -v`.** It deletes the database volume. Never use it on a stack you want
  to keep.

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
| `worker` | The worker heartbeat is younger than 180 s |
| `inventory` | The last inventory finished less than three refresh intervals plus 120 s ago |
| `mcp` | A `ping` to the local HTTP endpoint returns 200 |

The daemon writes the heartbeat to a `MemoryWorker` node from its own timer, every
30 seconds at most. A long scan or a long drain therefore does not look like a
dead worker, and a failed heartbeat write logs `heartbeat_error` without stopping
the daemon. Each recreated container has a new host name and so a new record.
Records of other hosts older than a day are pruned on each beat. The record
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
| `bank_scan` | `files`, `changed_files`, `staged`, `existing`, `failures`, or `skipped: queue_paused` |
| `transcript_scan` | `feeds` (files that staged work, or a `feed_identity_blocked` or `feed_identity_refused` entry), `seconds` |
| `processed` | `status`, `timings`, `model_calls`, `cached`, `skipped`, `claim_seconds`, and on failure `diagnostic`, `failed_attempts`, `retry_after`, `quarantined`, `validation_failures` |
| `provider_unavailable`, `provider_recovered` | `open`, `reason`, `retry_in` |
| `namespace_fault`, `namespace_recovered` | Same fields. The reason is `journal_state_mismatch`, `engine_changed` or `database_unavailable` |
| `journal_checkpoint` | `seconds`, `sequence`, `records`, `accepted_untracked_state` |
| `journal_checkpoint_error` | `error`, `diagnostic`. Reported once, then not retried for an hour |
| `feed_identity` | `feeds`, `stamped`, `already_stamped`, `unmatched`, `conflicts` |
| `worker_error`, `heartbeat_error` | `error`, and `diagnostic` for a worker |
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
- No `processed` events, no `provider_unavailable` and no `namespace_fault` means
  the queue is empty
  or everything is waiting for a retry time. Check `memory_status.processing`.
- Many `processed` failures with `evidence_quote_mismatch` point at extraction
  quality, not capacity. Adding workers will not help.

## Provider outage

When the model provider fails, the breaker opens. The log shows
`provider_unavailable` with a `reason`, workers stop claiming episodes and all
staging pauses, including the memory-bank scan. Episodes are not charged for the
outage. The daemon probes with one episode per wait. The wait starts at 60 s and
doubles only after a failed probe, to at most 900 s. A probe that found nothing
to send to the model is retried after about 5 s. Only a model call closes the
break, and the log then shows `provider_recovered`.

| Reason | What to do |
| --- | --- |
| `usage_limit` | Wait for the window to reset, or add credit |
| `authentication` | Log in again: `gm run --rm --no-deps --entrypoint codex worker login --device-auth` |
| `model_unavailable` | Check `TRANSCRIPT_MODEL` or `MEMORY_MODEL` against what the account can use |
| `rate_limit`, `network`, `timeout`, `unknown` | Usually passes without action. Check connectivity if it lasts |

No restart is needed after the cause is fixed. The next probe closes the breaker.
A restart is harmless and makes the first probe immediate.

A timeout or crash that happens while other episodes succeed is not an outage.
It is charged to that episode as `infra_failures`, and three of them quarantine
it with reason `model_timeout` or `model_invocation_failed`.

A `namespace_fault` event is a different pause. Its reason is
`journal_state_mismatch` (see [Journal maintenance](#journal-maintenance)),
`database_unavailable`, or `engine_changed`, after which the daemon exits and the
supervisor restarts it on the new code. `database_unavailable` covers a lock wait
that ran out, a deadlock and a dropped connection. No episode is charged. It
usually clears with the next probe. If it persists, look at Neo4j's memory and at
long transactions such as a `history verify` in progress. `namespace_recovered` marks the end of the pause.

## Journal maintenance

```sh
gm exec worker graph-memory --namespace transcripts history list --limit 20
gm exec worker graph-memory --namespace transcripts history verify-live
gm exec worker graph-memory --namespace transcripts history verify
gm exec worker graph-memory --namespace transcripts history checkpoint
```

**Routine check.** `history verify-live` streams the live graph and compares its
hash with the journal head. It takes seconds and constant memory, and it finds
untracked writes. It holds the namespace lock while it runs, so writers wait for
those seconds. Run it after a deploy, after any manual work in the database and
whenever a mismatch is suspected. It refuses a journal that has not yet migrated
to the per-node hash. One write or a checkpoint migrates it. The sampled audit
(`MEMORY_JOURNAL_AUDIT`) runs the same comparison during normal writes.

**Full audit in a window.** `history verify` also reads every journal event from
change 0, checks the whole hash chain, rebuilds the state from the latest
checkpoint and compares the live graph with it node by node, hashing every
referenced text. It holds the reconstructed state in memory without the long
texts, and reads events 25 at a time. On a journal that still contains snapshots
embedded by older versions those batches are large. It holds the namespace lock
for as long as the history takes to read. Stop the worker first and run it when nobody is waiting on
ingestion. What each command proves is listed in
[history](history.md#what-each-command-proves).

**Audit cadence.** The transcripts stack defaults to `MEMORY_JOURNAL_AUDIT=503`.
Every 503rd journal write streams the live graph and compares its hash with the
journal head. A lower number finds an untracked write sooner and costs more.
`1` checks every write and is meant for tests. `0` turns the audit off.

**Checkpoint cadence.** Historical reads replay from the latest checkpoint, so
their cost grows with the number of changes since then. The daemon takes a
checkpoint between scans when one is due: after 64 MB of changes, or the size of
the last checkpoint if that is larger, or after 2,000 events. It logs
`journal_checkpoint` with `seconds`, `sequence` and `records`. Workers wait on the
namespace lock while it runs. A checkpoint is stored as compressed parts of about
4 MB, with long write-once text left out, so it is far smaller than the single
strings of several hundred megabytes that older versions wrote.

A checkpoint compares the live graph with the journal before it writes. If they
differ, the daemon logs `journal_checkpoint_error` once, with diagnostic code
`journal_state_mismatch`, and does not try again for an hour. Treat that event
like a `namespace_fault` and follow the recovery below. A checkpoint by hand is
still useful after a bulk import and before an upgrade. Each one stays in the
store for good.

**Recovering from a journal mismatch.** The worker log shows `namespace_fault`
with reason `journal_state_mismatch`. The message is "Graph differs from its
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
5. Run `history verify-live`, start the services by name and confirm that
   `processed` events resume. A worker left running logs `namespace_recovered`
   after its next successful probe.

Do not use `--accept-live` when you have not identified the change, when the
change is one you would undo if you could, or as a routine fix to make the
error go away. It makes the journal vouch for a state it never recorded.
History before that checkpoint stays readable, but the step from the previous
event to the checkpoint is not explained by any recorded change.

## Feed identity

A transcript file is identified by `LABEL:relative/path`, where the label comes
from the transcript root. The rules are in
[ingestion pipeline](ingestion-pipeline.md#feed-identity). The worker logs three
events about it inside `transcript_scan.feeds`, and `feed_identity` on its own
line.

| Event | Meaning | Action |
| --- | --- | --- |
| `feed_identity` | Older feeds were stamped with their keys on the first scan | Check `unmatched: 0` and `conflicts: 0` |
| `feed_identity_blocked` | Some older feeds are stored under paths outside the current roots. Intake stages nothing | Stamp them, below |
| `feed_identity_refused` | One file is new by key and by path but has the name of a known feed. It was not read | Find out why the file moved. Fix the mount or the label so it gets its old key |

To unblock, stamp the feeds with the roots they were written under. The path in
`LABEL=PATH` is the prefix of the stored paths. Matching is on the text of the
path, so it need not exist where the command runs:

```sh
gm exec worker graph-memory --namespace transcripts feeds stamp \
  --root claude=/sessions/claude --root codex=/sessions/codex
```

The output counts `feeds`, `stamped`, `already_stamped`, `unmatched` and
`conflicts`. The command changes no feed id and can be repeated. The next scan
looks again and continues when nothing is unmatched. A conflict means two feeds
claim one file, and a person has to decide which one is kept.

`MEMORY_FEED_ACCEPT_UNMATCHED=1` makes intake continue regardless. Every file it
cannot match then gets a new feed, so sessions the graph already holds are staged
and extracted again under new ids, with duplicate facts as the result. Use it
only when the unmatched feeds belong to files that are gone for good. The Compose
files do not pass this variable to the worker.

**Never set `MEMORY_FEED_ACCEPT_UNMATCHED=1` on a CT receiving remote pushes.**
When a Mac forwarder reconnects with different roots or labels, the CT would mint
new feeds for every file, duplicating the entire graph. The Tailscale overlay
(`compose.transcripts.tailscale.yaml`) explicitly unsets it. See
[remote push](remote-push.md) for the full remote transcript setup.

Do not rename a label, and do not add a root below an existing root, without
planning it as a migration. Both change the key that existing files would get.
The daemon validates its roots at start and exits with a message when two
different directories share a label. Give one of them as `LABEL=PATH`.

## Name lookup

```sh
gm exec worker graph-memory --namespace transcripts aliases rebuild
```

The rebuild makes the `MemoryAlias` nodes equal to the entities' alias lists. It
runs in batches under the namespace lock, takes seconds, can be repeated, and
reports `entities`, `aliases`, `created` and `removed`. `repair` also runs it.
Run it once for a namespace written before the lookup existed, after every
writer is on the current code. Run it again when `memory_status.entity_names`
shows fewer `lookup_nodes` than `listed_names`, which means an older process
wrote entities. The alias nodes are outside the journal, so the rebuild creates
no journal entries and `verify-live` is unaffected.

## Scaling workers and intake

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRANSCRIPT_WORKERS`, `MEMORY_WORKERS` | 8, 4 | Concurrent extraction workers, 1 to 16 |
| `MEMORY_INTAKE_QUEUE` | 32 | Due episodes at which transcript intake stops staging |
| `MEMORY_INTAKE_FILES` | 4 | Transcript files with work that one scan may open. Both Compose files pass it to the worker |
| `MEMORY_LLM_TIMEOUT` | 420 | Seconds per model call, 10 to 600. The lease is four timeouts plus 60 s |

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
| `TRANSCRIPT_NEO4J_HEAP_MAX` | 4g | Dream writes capture the namespace, and reading a checkpoint written by an older version loads one very large string |
| `TRANSCRIPT_NEO4J_TX_MEMORY_MAX` | 2g | The same old checkpoint events are one string of several hundred megabytes. Do not go below 1g while they are still read |
| `TRANSCRIPT_NEO4J_MEM_LIMIT` | 8g | Heap max plus page cache plus about 2 GB of JVM overhead |

**Relaxing heap and transaction memory.** The 4 GB heap and 2 GB transaction
memory exist because of those old single-string checkpoints. From version 3 a
checkpoint is written and read in parts of about 4 MB, and long text is not in
it. Once a version 3 checkpoint exists, historical reads no longer load the old
one, and the two values can come down. Decide from evidence, not from the
release note:

1. Confirm the checkpoint: a `journal_checkpoint` event in the worker log, or the
   output of the manual `history checkpoint`.
2. Run one historical read (`recall NAME --at-change N` with a recent `N`) and
   `history verify-live`, and watch the Neo4j container's memory while they run.
3. Lower one value at a time in the env file, for example transaction memory to
   1g first, then heap to 2g. Recreate Neo4j with the long stop timeout.
4. Watch for `MemoryPoolOutOfMemoryError` in the Neo4j log, for
   `database_unavailable` faults and for failed `journal_checkpoint` events over
   a day of normal ingestion, including one automatic checkpoint. Go back up if
   any appear.

A historical read of a change before the first version 3 checkpoint still loads
an old checkpoint and needs the old values. So does `history verify`: it reads
every event from change 0, in batches of 25, and that includes every old event
with an embedded snapshot. Raise the values again for the window in which you
run it. The Compose defaults are unchanged.

Both stacks set a 60 s lock acquisition timeout and a 10 minute transaction
timeout, so one stuck lock holder cannot block every writer indefinitely.
Neo4j gets two minutes to stop. A shorter grace period ends in a kill during a
checkpoint and a recovery on the next start.

The personal stack uses a 512 MB heap, a 256 MB page cache and a 1,536 MB limit.

The transcripts MCP container has a 2 GB memory limit. Each client session
attached through `docker exec` is charged to that container, at about 56 MB per
session before query working sets. Historical requests can use substantially
more. A process-shared nonblocking file lock serializes historical MCP handlers;
competing callers get BUSY and must retry with the same cutoff. Locks release
automatically when a session dies. Entity/evidence history selectively retains
nodes while validating the complete journal; broad historical recall still has
a namespace-sized working set. This reduces concurrent peaks, not idle allocator
retention. A healthy main process does not establish that every stdio session
survived: inspect container OOM events when a client reports a closed transport.
The personal MCP container keeps 1 GB.

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
| `evidence_quote_mismatch`, `schema_validation`, other validation codes | Three runs failed validation, or a cached extraction no longer validates | Usually wait for an engine change that fixes the prompt or the validator. Retry one by hand to confirm |
| `model_timeout`, `model_invocation_failed` | Three model failures followed this episode while the provider served others | Look at the episode's size and content. Retry once the cause is understood |
| Any other code with eight `attempts` | Eight charged failures of any kind | Read the `diagnostic` of its last `processed` event before retrying |
| `ambiguous_identity` | The extraction names an entity that matches several | Merge or disambiguate the entities, then retry |
| `extraction_conflict` | The episode was committed with a different extraction | Retract the wrong facts explicitly. Do not retry blindly |

Release one episode:

```sh
gm exec worker graph-memory --namespace transcripts retry-quarantined EPISODE_ID
```

This clears the quarantine mark and resets the attempts, the validation failures
and the model failures. It keeps the rejection feedback. It clears the cached
extraction, which would otherwise be rejected again, unless the episode was set
aside for a model timeout or crash. It refuses an
episode that holds a live lease. An engine change releases every quarantined episode without
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

The personal stack starts with no `.env`: `NEO4J_PASSWORD` and `MEMORY_HTTP_TOKEN` both
default to `graph-memory`. While either still has that value the services stop
before touching the database, exit with code 2, and print the steps to change it.
`MEMORY_ALLOW_DEFAULT_PASSWORD=1` accepts the default for a throwaway graph. The
transcripts stack has no default and requires both values.

- The env file holds `NEO4J_PASSWORD` and the MCP bearer tokens. Keep one per
  stack outside the repository, readable only by you. A `.env` in the checkout is
  git-ignored.
  Generate values with `openssl rand -hex 32`.
- The stored Neo4j password wins over a later change of `NEO4J_PASSWORD`. The
  variable sets the password only on the first authenticated start of a volume.
  Changing it later leaves the stored password as it was, and every service then
  fails to connect. Rotate in Neo4j first, then update the env file and recreate
  the clients by name:

  ```sh
  gm exec neo4j cypher-shell -u neo4j -p "$OLD" -d system \
    "ALTER CURRENT USER SET PASSWORD FROM '$OLD' TO '$NEW'"
  ```
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

Tests and evals create and delete data. Never point `MEMORY_TEST_NEO4J_URI` at
17687 or 27687. Those are the live graphs. The test suite refuses to run against
either port and exits with code 2, unless `MEMORY_TEST_ALLOW_LIVE` is set. Do not
set it on a machine that runs the stacks. The guard is in the pytest fixtures. `evals.run` starts with pytest and is
covered; `evals.ab` has no such guard.

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
