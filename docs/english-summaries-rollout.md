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

3. **Eligibility, read-only.** Record what the old and new rules see:

   ```cypher
   MATCH (e:MemoryEpisode {namespace:'transcripts'})
   WHERE e.status IN ['pending','failed'] AND e.quarantine_engine IS NOT NULL
   RETURN e.quarantine_engine = $running AS same_engine, e.quarantine_reason, count(*)
   ```

   with `$running` the engine of the running worker
   (`graph-memory --version`). Under the old rule every row with
   `same_engine = false` was already due; under the new one none is.

4. **Backup** as in [operations](operations.md#backup).

## Gates after the deploy

- `memory_status.processing.quarantined` is at least the count from gate 3, and
  `queued` did not jump by that count.
- Worker `processed` events show no episode with a `quarantine_reason` from
  before the deploy.
- A new session's facts have English summaries and literal quotes
  (`memory_evidence` on one of them).
- **Rollback.** A tag from before this release releases every episode
  quarantined under another engine. Stop the worker before running one.
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

It ends with `CANARY PASS 35 checks`, or `CANARY FAIL` and exit 1. It covers
create, append, swap by rename, rename, a new 0700 project directory and rsync
after each; that the reader cannot create, append, remove, rename, chmod or
widen the ACL; that an unrelated UID and a member of the uploader's group are
denied; that the uploader keeps full control; that no owner, group or inode
changes; and that no extended ACL exists outside the inbox. Two negative
controls must be denied, and the opt-in test also checks that a weaker setup
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

Files already in an inbox keep the modes they arrived with. A later ACL does not
reach them, so the 299 Claude paths without a feed cursor stay unreadable to the
reader until someone extends the grant to them. Do not do that first:

1. Apply the inbox pattern above for new transfers only.
2. Wait for one new Claude session to arrive through the uploader.
3. As the reader UID, read that one file, and confirm with `getfacl` that its
   effective entry for 10001 is `r--` and the group and other entries are `---`.
4. Confirm with a read-only `inventory --once` that its source key is counted,
   and, once the worker has fed it, that its facts have English summaries and
   literal quotes.
5. Only then extend read access to the backlog, and let intake drain it within
   `MEMORY_INTAKE_FILES` and `MEMORY_INTAKE_QUEUE`.

While backlog files are unreadable, each one fails with `PermissionError`,
stays unseen and is retried, and each failure uses one of the scan's
`MEMORY_INTAKE_FILES` slots (`follow.py`). Many unreadable files can delay a new
session by several scans. This is read from the code, not measured.
