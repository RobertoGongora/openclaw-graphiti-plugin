# Docker deployment

`compose.yaml` runs the complete local stack:

| Service | Responsibility | Local address |
| --- | --- | --- |
| `neo4j` | Persistent graph, queue, evidence, and Neo4j Browser | http://127.0.0.1:17474/browser/; Bolt 127.0.0.1:17687 |
| `worker` | Scan a read-only memory-bank mount; process queued messages with Terra low | No published port |
| `mcp` | Five session-facing tools over stateless HTTP | http://127.0.0.1:8765/mcp |

Colima is the Linux VM running Docker on macOS; all three services are Docker
containers. Source files and model credentials are never copied into the image.
The production image has only the two direct Python dependencies plus the optional
Codex CLI. The `eval` build target adds pytest and fixtures for isolated validation.

## First installation

1. Copy `.env.example` to `.env`. Set `MEMORY_BANK_PATH` to an existing directory
   and replace `MEMORY_HTTP_TOKEN` with a random private token. Keep `.env` private.
2. Build: `docker compose build`.
3. Authenticate the worker: `docker compose run --rm --no-deps --entrypoint codex worker login --device-auth`.
4. Start: `docker compose up -d`.

Alternatively configure `MEMORY_LLM=compatible`, `MEMORY_LLM_URL`, `MEMORY_MODEL`,
and `MEMORY_LLM_API_KEY`; Codex authentication is then unnecessary.

The MCP HTTP endpoint requires `Authorization: Bearer <MEMORY_HTTP_TOKEN>` and the
protocol headers documented in [mcp.md](mcp.md). No credentials are needed for the
loopback-only development Neo4j Browser: connect to `bolt://127.0.0.1:17687`.
An internet/shared deployment requires a separate authenticated database/network setup.

## Sources and restart behavior

The default mount is `/bank`, scanned every 30 seconds. Changed files are read
into immutable source versions, checked against existing graph records, and only
new content is queued. Original files are never edited. File deletion does not
retract evidence. Changed notes preserve previous claims as history/evidence;
file modification dates do not invent event times or silently resolve conflicts.

The scanner and four queue consumers run independently in one worker container.
Neo4j stores completed jobs, retries, leases, and the original evidence. Container
restarts do not need a local cursor file. Graceful shutdown finishes active jobs;
a forcibly killed worker's jobs become eligible when their leases expire.

For multiple roots, override the worker's `command` and mount each source directory
read-only. For Claude's project memory bank, preserve a path ending in
`.claude/projects`; the scanner selects only `*/memory/*.md`. A general bank root
recursively includes `.md` files. Explicit files may also be `.txt` or `.jsonl`.
Use `--transcripts /sessions` for ongoing append-only Claude/Codex JSONL sessions;
this uses durable append cursors and skips an unfinished final line.

**When moving an existing installation, preserve the original absolute source
paths inside the container.** Source identity includes its path. Mounting the
same bank at an unrelated new path intentionally gives it a new identity.
Creation timestamps can differ between macOS and Linux; the scanner recognizes
identical source content and retains the timestamps already in Neo4j.

Extraction, commit, retry, structural repair, and dream operations remain Python
engine/CLI responsibilities. The MCP catalog exposes only recall, latest, ingest,
retract, and merge. Dream scheduling/promotion remains an explicit engine operator
action (`graph-memory dream ...`), not an automatic ingestion side effect.

## Operations

```sh
docker compose ps
docker compose logs --tail 50 worker
docker compose exec worker graph-memory --namespace personal recall Atlas
# Rebuild only after validation; completed source records are preserved.
docker compose up -d --build
```

The worker logs scan counts, episode IDs, completion/failure status, and exception
class names. Failed `processed` events also include a bounded `diagnostic` object:
stage, reason code, extraction attempt, elapsed seconds, engine fingerprint, and
schema/evidence field locations where available. `failed_attempts` and `retry_after`
identify the durable retry count and next eligible Unix timestamp. Examples of
reason codes are `evidence_quote_mismatch`, `schema_validation`, `ambiguous_identity`,
`model_timeout`, and `model_invocation_failed` (with its CLI exit code).

Validation diagnostics include at most ten issues and twelve path components per
issue. Unknown field names are masked. Raw exceptions, rejected model output,
source quotes, Pydantic input/context, and credentials are excluded. Unknown errors
use `unclassified_error`; this is not a claim that their cause was diagnosed.
These details live in the existing Docker log stream, not the knowledge journal.
Old errors cannot acquire details retroactively. Existing correction attempts,
evidence/schema safeguards, and retry delays are unchanged. Review with:

```sh
~/.local/share/graph-memory/bin/compose logs --since 1h worker
```

The worker does not log private message text or model credentials. Compose
rotates worker logs and restarts services unless explicitly stopped. Named database
and credential volumes survive container replacement. Do not use `down -v` when
preserving an installation.

For this Mac's source mounts, existing database volume, and cutover evidence, see
[local ingestion](local-ingestion.md). Its generated deployment lives outside the
worktree so ongoing edits cannot change the running engine.

References: [Docker Compose service configuration](https://docs.docker.com/reference/compose-file/services/),
[Codex headless authentication](https://developers.openai.com/codex/auth/).

## Durable rejection feedback

Queued extraction retries retain the latest actionable validation failure on the
source episode as private `retry_feedback`. The next attempt receives its reason,
field location, and rejected candidate alongside the original transcript and fresh
graph context. Feedback is untrusted repair context, never evidence. Both Codex
and compatible HTTP adapters retain final schema-rejected output; evidence failures
retain the final rejected extraction. Immediate corrections also receive the
structured diagnostic location.

Only one failure context is kept. Candidates are limited to 64,000 encoded bytes
and the complete serialized feedback to 80,000 bytes; oversized or malformed
candidates are omitted while diagnostic feedback remains. Timeouts and invocation
failures do not overwrite earlier validation feedback. Successful commits clear it.
A new source version has a different episode ID and cannot inherit old feedback.
An engine fingerprint change makes old feedback ineligible for reuse.

Feedback is excluded from journal snapshots/deltas, current facts, and diagnostic
logs. It is operational data, like leases and retry timing, not a new memory fact.
Existing failed jobs acquire it after their next validation rejection; historical
logs do not contain enough information to reconstruct rejected candidates.

Upgrade all graph readers/writers (MCP, workers, and host CLI) before restarting
workers, so every journal capture excludes this new operational field. Keep the
validated image pinned. Rolling back to a build unaware of `retry_feedback` after
new failures would misclassify that property as a knowledge change.
