# Transcript source graph

The markdown import remains on the original Docker stack with twelve Terra/low
consumers. It continues toward completion and is not rewritten by this work.
The transcript graph is a separate Neo4j Community container and persistent
volume. Community supports one standard database per instance; namespaces are
logical separation, not a replacement for database isolation.

## Evidence contract

Original conversational claims are the origin of facts. Tool outputs are only
validation evidence, never sources of independent facts. Memory reads, writes,
patches and compaction summaries are contextual, potentially stale material.

Every source-record fact must quote a user or assistant statement in `evidence`.
`validation_evidence` separately quotes tool results. An assistant claim without
such validation is retained only as uncertain and undated. A memory read/write
cannot validate it. Reading yesterday's memory today does not renew its date.
These structural constraints are deterministic; whether a quote semantically
supports a claim still depends on extraction quality and the fixed evals.

## Intake

Use `daemon --source-records --transcripts DIRECTORY` or
`feed FILE --session-id ID --source-records`. This explicitly selects the versioned
source parser, separate from the old text-only feed. Claude Code JSONL and Codex
response-item JSONL are supported, including Codex custom tool calls/outputs.
Memory MCP recall results also remain derived context, not fresh verification.
Provider duplicate event mirrors, reasoning, and system/developer instructions
are not claim sources. Non-text attachments are gaps, not interpreted content.

A session stays a single source identity with multiple bounded episodes. Each
batch contains at most eight new text chunks (90,000 characters), four preceding
chunks of context, and available earlier calls for late results. Individual
records are split at 24,000 characters without dropping remaining text. Each
chunk retains a record ID, role, timestamp, call ID and source classification.
The durable cursor and prefix hash reject rewritten history; repeated intake or
process restart does not replay completed chunks. A partial final JSONL line
waits for completion. Up to four changed sources/four batches each are staged
per scan. Intake pauses with 32 pending episodes to bound the extraction backlog.
Source files must remain available until intake has caught up.

## Historical memory artifacts

Explicit Read/Write/Edit tools, apply_patch, static cat/sed/head/tail/rg/grep
references and literal nested exec arguments are recognized without executing
source commands. Relative paths resolve against recorded working directories;
unknown relative paths are scoped to their session to avoid cross-project merges.

The importer never opens a memory path mentioned in a transcript. Read results
are captured excerpts, not guaranteed complete file versions. Writes preserve
submitted content; edits preserve the available patch or before/after strings.
An invocation is not proof that the write succeeded. No before-version is
invented. Redaction occurs before graph storage, so these are redacted evidence
records, not byte-identical source archives.

Compound command output is retained on its source message, but it is not assigned
as a file snapshot when individual outputs cannot be attributed. The artifact
observation records `captured=unavailable` and a gap. Dynamic paths, arbitrary
scripts, missing calls/results, and non-text output remain explicit coverage
limitations. The engine does not execute, fetch, or reconstruct missing history.

## Graph and inspection

| Node | Display name | Links |
|---|---|---|
| MemorySession | First conversational request | HAS_EPISODE, HAS_MESSAGE |
| MemoryEpisode | Session title and source date | CONTAINS |
| MemoryMessage | Role/type, date, tool or text excerpt | RESULT_OF, TOUCHED_MEMORY |
| MemoryArtifact | File basename (full path remains a property) | Incoming VERSION_OF |
| MemoryArtifactObservation | Read/write/patch, filename, date | VERSION_OF |
| MemoryFact | Fact summary | CITES claims, VALIDATED_BY tool results, SUPPORTED_BY episode |
| MemoryEntity | Entity name | Existing semantic graph links |
| MemoryDream / MemoryInsight | Dream subject / insight summary | Existing inference links |

Source nodes and their reconstructible links participate in audit verification,
historical replay and candidate revisions. Existing live markdown engines are
not upgraded as part of this deployment. Do not share the new database with an
old engine unaware of source labels.

```cypher
MATCH p=(s:MemorySession)-[:HAS_EPISODE]->(e)-[:CONTAINS]->(m)
RETURN p LIMIT 200;
```

```cypher
MATCH p=(f:MemoryFact)-[:CITES|VALIDATED_BY]->(m:MemoryMessage)
RETURN p LIMIT 200;
```

## Deployment and validation

`compose.transcripts.yaml` starts Neo4j, the transcript worker and MCP separately
from the markdown stack. Default transcript concurrency is two; it does not
change the twelve markdown workers or claim a throughput comparison.

- Browser: http://127.0.0.1:27474/browser/
- Bolt: bolt://127.0.0.1:27687
- MCP: http://127.0.0.1:8766/mcp (private token; namespace `transcripts`)

Set the paths/token named in the Compose file; mount sources read-only. Do not
mount model-worker-generated session directories, which would ingest extraction
prompts and outputs recursively.

`tests/test_session_sources.py` covers source parsing, identity, historical
artifacts, append/restart, tool-only rejection and audit replay. Frozen real-model
canaries live in `evals/transcripts/cases.json`; run
`MEMORY_LLM=codex python -m evals.transcripts.run`. They make no graph writes.
A passing canary is not certification of full historical coverage or semantic
accuracy. Preserve failed runs as well as successful evidence.

After both imports reach the agreed fixed source coverage, benchmark the same
questions and expected facts against both graphs. No memory-system superiority
claim or new golden baseline is established by this implementation.

On the local Colima instance, automatic forwarding did not expose the new ports.
Ports 27474, 27687 and 8766 were added to its existing SSH control connection,
without restarting Colima. Check forwarding again after a VM restart. The local
private Compose helper is `~/.local/share/graph-memory/bin/compose-transcripts`;
the original `compose` helper still operates the markdown stack.
