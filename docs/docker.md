# Docker deployment

`compose.yaml` runs the complete local stack:

| Service | Responsibility | Local address |
| --- | --- | --- |
| `neo4j` | Persistent graph, queue, evidence, and Neo4j Browser | http://127.0.0.1:17474/browser/; Bolt 127.0.0.1:17687 |
| `worker` | Scan a read-only memory-bank mount; process queued messages with Luna | No published port |
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
class names. It does not log private message text or model credentials. Compose
rotates worker logs and restarts services unless explicitly stopped. Named database
and credential volumes survive container replacement. Do not use `down -v` when
preserving an installation.

For this Mac's source mounts, existing database volume, and cutover evidence, see
[local ingestion](local-ingestion.md). Its generated deployment lives outside the
worktree so ongoing edits cannot change the running engine.

References: [Docker Compose service configuration](https://docs.docker.com/reference/compose-file/services/),
[Codex headless authentication](https://developers.openai.com/codex/auth/).
