# Rollout: English summaries, held quarantines, Claude feed permissions

A short runbook for this release. Nothing here is run against production by a
script; each gate is a person checking a result. Commands that change a host are
not given: the permission pattern is described and tested in a container, and
applying it is a separate, reviewed step.

## What changes

- New extractions write `Fact.summary` in English. Evidence quotes stay verbatim
  in the source language. Stored facts are not rewritten and nothing is
  reprocessed.
- The prompt change is an engine change. Quarantined episodes now stay
  quarantined across engine changes; before, the new engine would have released
  all of them at once (the transcripts graph had 901) and they would have filled
  the intake queue gate ahead of new sessions. See
  [ingestion pipeline](ingestion-pipeline.md#quarantine).

## Gates before the deploy

1. **Suite and lint** on the release commit, against a disposable database:

   ```sh
   uv sync --locked
   make lint typecheck
   MEMORY_TEST_NEO4J_URI=bolt://127.0.0.1:<test port> uv run pytest -q
   MEMORY_TEST_FEED_CANARY=1 uv run pytest -q tests/test_feed_permission_canary.py
   ```

2. **Real-model eval** on the same commit, subscription CLI login only, synthetic
   sessions only, one suite at a time per database:

   ```sh
   env -u OPENAI_API_KEY -u CODEX_API_KEY MEMORY_LLM=codex \
     MEMORY_TEST_NEO4J_URI=bolt://127.0.0.1:<test port> \
     uv run python -m evals.english_summaries --runs 2 --output .local/english.json
   ```

   The report records commit, engine, prompt and case hashes, CLI version,
   model, effort, the model's raw output and the committed extraction. A failed
   case holds the deploy until someone has read why.

3. **Held IDs, read-only, kept private.** Export the held episode IDs to a
   file outside the repository; it names private sessions and is not committed:

   ```cypher
   MATCH (e:MemoryEpisode {namespace:'transcripts'})
   WHERE e.status <> 'complete' AND e.quarantine_engine IS NOT NULL
   RETURN e.id, e.quarantine_engine = $running AS same_engine, e.retry_after
   ORDER BY e.id
   ```

   with `$running` the engine of the running worker (`graph-memory --version`).

4. **Backup** as in [operations](operations.md#backup).

## Gates after the deploy

- Every exported ID is still held: the same query restricted to
  `e.id IN $exported` returns all of them with `quarantine_engine` set and
  `status <> 'complete'`.
- No exported ID appears among the `episode_id`s of the worker's `processed`
  events since the deploy. Compare IDs; a missing `quarantine_reason` in a log
  line proves nothing.
- One new session's facts have English summaries and literal quotes, checked by
  ID as in the session gate below.
- **Rollback.** Under a tag from before this release, every exported ID whose
  `same_engine` is false becomes eligible as soon as its lease has expired and its
  `retry_after` has passed; the rest wait for their backoff. Keep every old
  worker, daemon and `work` process stopped on this graph, or accept that.
- Requalifying the old quarantine is a separate decision: retry a handful with
  `retry-quarantined EPISODE_ID`, read the outcomes, then decide on the rest.

## Claude feed permissions

The canary checks a least-privilege inbox: the uploader (UID 10002) owns the
inbox and rsyncs Claude sessions into it, the reader (UID 10001, the image's
`memory` user; `compose.transcripts.yaml` runs the worker as `HOST_UID`, which
must then be that reader) may list and read, and nobody else gets anything. Sources keep
Claude's modes, directories 0700 and files 0600. It runs only in a disposable
container on synthetic files, with no network and no watcher:

```sh
evals/feed_permissions/run.sh          # anonymous volume
FS=tmpfs evals/feed_permissions/run.sh # tmpfs
```

It ends with `CANARY PASS 39 checks`, or `CANARY FAIL` and exit 1. It covers
create, append, swap by rename, rename, a new 0700 project directory and rsync
after each; that the reader cannot create, append, remove, rename, chmod or
widen the ACL; that an unrelated UID and a member of the uploader's group are
denied; that the uploader keeps full control; that no owner, group or inode
changes; and that no extended ACL exists outside the inbox. Negative controls
show what fails: plain `rsync -a`, no ACL, and an inbox that held sessions before
its ACL (N3, N4), and the opt-in test also checks that a weaker setup
fails the canary.

What it established:

- `rsync -a` of 0600 files into an inbox with the reader's ACL leaves the reader
  out: the copied mode sets the ACL mask to `---`. So does `rsync -rlt`.
- The tested transfer is `rsync -a --chmod=D750,F640`, into an inbox whose owner
  has set, on that directory only,
  `u:10001:rx,d:u::rwx,d:u:10001:rx,d:g::-,d:m::rx,d:o::-`. `ls` then shows
  0750/0640 with `+`, but the owning group's entry is `---`; the 0750/0640 is the
  mask that lets the reader's entry work.
- The uploader sets the ACL on its own directory; neither root, nor `chown`,
  nor a recursive or parent ACL is needed when the parents are traversable. If a
  parent is not, it needs `u:10001:x` on that one directory, nothing wider.

Not established: the real host's filesystem and parent permissions. Run the
canary's checks there only on a scratch directory beside the inbox, never on the
inbox or `/srv/graph-memory` itself, and only after review.

### One real session before the backlog

An ACL set on a directory that already holds sessions does not reach them, and
`rsync --chmod` then gives the uploader's group read on those old inodes. The
canary shows both (N3, N4). So the pattern is only for a new, empty inbox, and
the backlog's existing permissions are never widened:

1. The uploader creates a new, empty inbox and sets the ACL on it before the
   first transfer (`the inbox is empty when its ACL is set`). It is the
   directory the worker will keep reading, under the same label, so a session's
   source key does not change later. A new root is subject to
   [feed identity](ingestion-pipeline.md#feed-identity); watch for
   `feed_identity_blocked` and `feed_identity_refused`.
2. One new Claude session arrives: the uploader transfers exactly that path
   (for example with `--files-from` listing it alone) with the tested flags.
3. As the reader UID, read that file, and check with `getfacl` that the entry
   for 10001 is effective `r--` and the group and other entries are `---`.
4. Find its feed and episodes by key, read-only:

   ```cypher
   MATCH (f:MemoryFeed {namespace:'transcripts', source_key:$key})
   OPTIONAL MATCH (e:MemoryEpisode {namespace:'transcripts'})
   WHERE e.source_id STARTS WITH 'records:' + f.id + ':'
   OPTIONAL MATCH (x:MemoryFact)-[:SUPPORTED_BY]->(e)
   RETURN f.id, f.message_count, e.id, e.status, collect(x.id) AS facts
   ```

   Then call `memory_evidence` with those fact IDs and `"detail":"full"`:
   summaries in English, quotes literal. `inventory --once` is not this check:
   it writes the namespace's aggregate `MemoryInventory` and proves nothing
   about one file.
5. Only then move the backlog: transfer it into the new inbox with the same
   flags, so each file arrives as a new inode under the inbox ACL, and let
   intake drain it within `MEMORY_INTAKE_FILES` and `MEMORY_INTAKE_QUEUE`.
   Never `setfacl -R` or `chmod` the old tree.

The 299 Claude paths without a feed cursor are that backlog. While files in a
mounted root are unreadable, each one fails with `PermissionError`, stays unseen
and is retried, and each failure uses one of the scan's `MEMORY_INTAKE_FILES`
slots (`follow.py`), so many of them can delay a new session by several scans.
This is read from the code, not measured.
