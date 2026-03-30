# Changelog

## [Unreleased]

### Changed

- **SDK import modernisation**: migrated type import from root `openclaw/plugin-sdk`
  to narrow subpath `openclaw/plugin-sdk/plugin-entry`; removed `(api as any)` casts
  on `registerContextEngine` (now typed on `OpenClawPluginApi`).
- **Published package metadata**: `README.md` and `skills/` are now included in the
  npm tarball via the `files[]` array.
- **Plugin manifest**: added `"skills": ["./skills"]` for agent skill discovery.

### Added

- **SKILL.md**: new `skills/graphiti/SKILL.md` with tool reference table, usage
  guidance, and memory-core complementarity notes.
- **Install guidance**: README now documents stable (pinned version) vs beta
  (`@beta` tag) install paths.

### Added

- **Smart autoRecall** (#164): `assemble()` is now a two-stage continuity-aware
  pipeline that only fires when context loss is detected — not every turn.
  - **Stage A** reads the tail of the JSONL session transcript (bounded 128KB chunk)
    to recover what was actually discussed.
  - **Stage B** uses the recovered continuity text as the `/search` query for
    targeted semantic fact retrieval. Falls back to `/get-memory` with the current
    message window when no session file is available.
  - **Trigger conditions**: continuity gaps (bootstrap, compaction, ≤3 messages)
    and deictic references ("continue", "as I mentioned", "where we left off").
    Normal turns with sufficient message history get zero auto-injection.
  - **Output**: separate `<graphiti-continuity>` and `<graphiti-context>` blocks
    for clear debugging and feedback-loop prevention via `sanitizeForCapture()`.
  - **Episode-based fallback** (Stage A.5): when the session file is empty or
    missing (after `/new`, timeout, or resume), `assemble()` now queries recent
    episodes from the knowledge graph and filters by `session_key` provenance to
    recover what was discussed in the same session. This closes the reset/timeout
    gap where Stage A had nothing to recover.

- **`thread_id` wired end-to-end**: `thread_id` is now populated from runtime
  context (`HookContext.threadId`), stored in episode provenance across all
  capture paths (ingest, afterTurn, compact, ingestBatch, onSubagentEnded), and
  used by episode-based continuity recovery to prefer same-thread episodes.

### Fixed

- **`autoRecall: false` now honored in ContextEngine mode**: `assemble()` returns
  a pass-through when `autoRecall` is not explicitly `true`, matching the
  documented opt-in default. Capture and compaction still work regardless.
- **Session-scoped `thread_id` scoring**: `extractEpisodeContinuity()` now
  requires a `session_key` match before considering `thread_id` as a tiebreaker,
  preventing cross-session content leakage via reused thread IDs.

### Changed

- Legacy hooks `before_agent_start` now uses shared `formatFactsAsContext()`
  instead of inline fact formatting.

## [0.7.0-beta.2] — 2026-03-22

> Beta release with fix for episode UUID display.

### Fixed

- **`graphiti_episodes` output format**: Episode output now includes `**uuid: ...**`
  prefix for each episode, making it easier to use with `graphiti_forget`. When an
  episode has no `name`, the tool displays `(unnamed)` instead of repeating the
  UUID twice. Previously, episodes without names rendered as `**uuid: <id>** <id>`,
  which was confusing and broke copy-paste workflows for `graphiti_forget`.

## [0.7.0-beta.1] — 2026-03-22

> Beta release for P1 quick wins.

### Added

- **`graphiti_forget` tool**: Delete facts or episodes from the knowledge graph.
  Supports direct deletion by UUID (`type: "fact" | "episode"`) or search-then-delete
  by query. Note: query-based search currently supports facts only — to delete an
  episode, use its UUID directly. Auto-deletes single fact matches and lists
  candidates for multiple matches. Includes UUID format validation for defense-in-depth.
  New client methods: `deleteEdge(uuid)` and `deleteEpisode(uuid)`.

- **`graphiti_episodes` tool**: List recent ingestion records as an agent tool.
  Accepts `limit` (max 50) and optional `sessionKey` filter. Reuses the same
  session-key filtering logic as the CLI `episodes` command.

- **Multi-group search**: `graphiti_search` tool and `client.search()` now accept
  an optional `groupIds` parameter to search across multiple graph groups in a
  single call. Falls back to the configured `groupId` when omitted.

- **Input sanitization**: New `sanitizeForCapture()` function strips plugin-injected
  metadata before graph ingestion, preventing feedback loops where recalled context
  (`<graphiti-context>` blocks), conversation metadata JSON, `[Subagent Context]`
  lines, and timestamps are re-ingested as new knowledge. Applied at all 4
  ContextEngine ingestion points (`ingest`, `ingestBatch`, `afterTurn`, `compact`).

- **Temporal info in CLI search**: `openclaw graphiti search` now displays
  `valid_at` and `invalid_at` alongside each fact.

### Changed

- **`recallMaxFacts` default raised from 1 to 10**: Aligns `openclaw.plugin.json`
  and the `index.ts` fallback with the ContextEngine default (which already used
  10). When auto-recall is enabled, the agent now receives a useful amount of
  context by default.

## [0.6.2] — 2026-03-12

### Fixed

- **`ownsCompaction` set to `false`**: The plugin no longer claims ownership of
  compaction. With `ownsCompaction: true`, `compact()` was called without
  `messages` — causing it to always return `{ compacted: false }` — and the
  runtime trusted this, so sessions grew unbounded. Setting it to `false`
  defers to OpenClaw's built-in auto-compaction.

- **`afterTurn` compaction sweep**: When auto-compaction fires mid-prompt,
  `prePromptMessageCount` becomes stale (pre-compaction count) while `messages`
  is post-compaction. `messages.slice(prePromptMessageCount)` returned `[]`,
  silently losing the current turn's content from the graph. `afterTurn` now
  detects when `prePromptMessageCount > messages.length` and sweeps all current
  messages. Sweep ingestions use the `"after_turn_sweep"` provenance event, and
  sweep truncation keeps the tail (`slice(-12000)`) so the newest messages
  survive when the survivor set exceeds 12k chars.

- **`autoCapture: false` honored in ContextEngine capture paths**: `afterTurn()`,
  `ingest()`, and `ingestBatch()` now check `this.cfg.autoCapture` and return
  early when disabled. Previously, setting `autoCapture: false` had no effect
  on ContextEngine methods — every turn was still ingested into Graphiti.

### Changed

- **`compact()` accepts `runtimeContext` and `customInstructions` params**:
  Forward-compatible with a future OpenClaw `runtimeContext.compactBuiltIn`
  callback. No logic changes — the params are accepted but unused.

## [0.6.1] — 2026-03-10

### Fixed

- **autoIndex skips non-prose files** (#13): Memory file indexing now filters
  by file extension before sending content to Graphiti. Only `.md` and `.txt`
  files are indexed by default; `.json`, `.png`, and other structured/binary
  files are skipped. This eliminates ~15% noise entities (file paths, dates,
  metadata field names, URLs) and ~187 junk edges traced to non-prose files in
  production graph audits.

### Added

- **`autoIndexExtensions` config option**: User-configurable array of file
  extensions to index (default: `[".md", ".txt"]`). Applies to both the
  `after_tool_call` hook and the `backfill` CLI command.
- **`file_type` in episode provenance**: Memory-index episodes now include the
  file extension in `source_description` for traceability.
- **`[filtered]` label in backfill dry-run**: `openclaw graphiti backfill --dry-run`
  now shows which files would be skipped due to extension filtering.
- **Separate `unreadable` counter in backfill**: `openclaw graphiti backfill` now
  distinguishes files that couldn't be read (too large, missing, etc.) from files
  that were simply unchanged. Non-zero counts are shown only when present.

## [0.6.0] — 2026-03-08

### Added

- **`GraphitiContextEngine` class** (`context-engine.ts`): First-class
  ContextEngine implementation for OpenClaw v2026.3.7+. When the host supports
  `registerContextEngine`, the engine replaces the sidecar hooks
  (`before_agent_start`, `before_compaction`, `before_reset`) with structured
  lifecycle methods. All 9 interface methods are fully implemented:
  - **`ingest`** / **`ingestBatch`**: Ingest individual or batched messages into
    the knowledge graph, filtering by role and content length.
  - **`afterTurn`**: Ingest new messages from each conversation turn (replaces
    the per-turn capture that hooks couldn't do).
  - **`assemble`**: Recall relevant facts via `/get-memory` and inject them as a
    `<graphiti-context>` system prompt addition.
  - **`compact`**: Graph-aware compaction — safety-ingests messages being
    compacted, then signals the runtime to truncate. Falls back to legacy bridge
    when the server is unhealthy.
  - **`bootstrap`**: Health-checks the server and reports graph population
    (episode count) on session start.
  - **`onSubagentEnded`**: Cross-agent propagation — ingests a subagent's
    summary or raw messages into the parent's graph scope when it finishes.
  - **`prepareSubagentSpawn`**: Injects relevant facts from the graph into a
    child agent's initial context based on its task description.
  - **`dispose`**: No-op (no persistent connections).

- **`shared.ts` module**: Extracted shared helpers to avoid circular
  dependencies between `index.ts` and `context-engine.ts`:
  - `buildProvenance()` — JSON-encoded provenance for episode source_description.
  - `extractTextContent()` — extract text from string or content-block-array.
  - `extractTextsFromMessages()` — extract user/assistant texts from a message
    array.
  - `formatFactsAsContext()` — format facts as a `<graphiti-context>` block
    (shared by `assemble()` and `prepareSubagentSpawn()`).

- **`kind: "context-engine"` in plugin manifest** (`openclaw.plugin.json` and
  `index.ts`): Declares the plugin as a context engine provider so OpenClaw can
  route lifecycle calls to it when supported.

- **40 new tests** in `test/context-engine.test.ts` covering all engine methods
  against the mock HTTP server.

### Changed

- **Deduplicated `index.ts`**: The local `buildProvenance()` function and inline
  text extraction in `before_compaction`/`before_reset` hooks are replaced with
  imports from `shared.ts`. Behavior is identical; no hook output changes.

- **Hooks are skipped when ContextEngine is active**: When the host supports
  `registerContextEngine`, the plugin does not register `before_agent_start`,
  `before_compaction`, or `before_reset` hooks (the engine handles these).
  `session_start` and `after_tool_call` hooks are still registered.

## [0.5.0] — 2026-03-06

### Added

- **Session metadata on auto-captured episodes**: Episodes ingested by
  `before_compaction`, `before_reset`, and the `graphiti_ingest` tool now embed
  session context in the JSON provenance `source_description`. The fields
  `session_key`, `agent`, `channel`, and `session_start` are added when
  available, enabling queries like "what happened in this session?" and
  per-session episode filtering.
- **`session_start` hook** (always registered): Records session start timestamps
  so `session_start` metadata is available to subsequent capture hooks.
- **`SessionMeta` interface** and **`buildEpisodeName()` helper** exported from
  the plugin for downstream use. Episode names now include the session key when
  available (`compaction-<sessionKey>-<ts>`), making them more traceable.
- **Factory pattern for `graphiti_ingest`**: The ingest tool is now registered
  via a factory function receiving tool context (session key, agent, channel),
  so every manual ingest episode carries full session provenance automatically.
- **`openclaw graphiti episodes --session-key <key>`**: Filter episodes by
  session key. Works with JSON provenance format; falls back to name-based
  matching for legacy episodes.
- **Session fields in `episodes` display**: `openclaw graphiti episodes` now
  shows `agent=` and `channel=` from provenance when available.
- **Auto-index memory files**: When the agent writes a file to `memory/`, the
  plugin automatically creates a lightweight index episode in Graphiti via the
  `after_tool_call` hook. Episodes include YAML frontmatter (type, file path,
  mtime, size) and a ~500-char excerpt. Controlled by the new `autoIndex`
  config option (default: `true`).
- **Idempotent state tracking**: Index state is persisted to
  `~/.openclaw/state/graphiti/graphiti-memory-index.json` with atomic writes.
  Files are only re-indexed when their mtime changes.
- **`openclaw graphiti backfill`** CLI command: Scans a memory directory
  (default: `./memory`) and indexes all new or modified files into Graphiti.
  Supports `--dir <path>` and `--dry-run` flags.
- **`autoIndex`** config option and UI hint for toggling memory file indexing.
- New `memory-index.ts` module with `extractMemoryPath`, `indexEpisodeName`,
  `buildIndexContent`, `readMemoryFileMeta`, state read/write, `upsertIndexEpisode`,
  and `scanMemoryFiles`.
- 25 new unit tests in `test/memory-index.test.ts` covering path extraction,
  naming, content format, state persistence, upsert integration, and scanning.
- 4 new hook tests in `test/hooks.test.ts` for `after_tool_call` behavior.

## [0.4.0] — 2026-03-06

### Added

- **Source provenance metadata**: All episodes ingested into Graphiti now carry a
  JSON-encoded provenance object in `source_description` with `plugin`, `event`,
  `ts`, `group_id`, and optional `session_key`/`file`/`source` fields. This
  replaces the previous hard-coded strings (e.g. `"OpenClaw auto-capture:
  pre-compaction conversation"`) with structured, parseable metadata for auditing
  and debugging the knowledge graph.
- **`buildProvenance()` helper** inside `register()` ensures consistent
  provenance across all ingestion paths (manual tool, compaction hook, reset
  hook, CLI ingest).
- **`openclaw graphiti ingest`** CLI subcommand for file-based and text-based
  ingestion with `--source-file`, `--content`, and `--name` options.
- **`openclaw graphiti episodes`** now shows human-readable provenance summaries
  per episode. Legacy plain-text `source_description` values display gracefully.
  Use `--json` for raw output (previous default behavior).
- **Structured debug log file** (`~/.openclaw/logs/graphiti-plugin.log`):
  Append-only log recording HTTP status codes, timing, and result counts for
  all Graphiti operations. Never logs conversation content, search queries, or
  PII. Designed for sharing in bug reports.
- **`openclaw graphiti logs`** and **`openclaw graphiti logs --clear`** CLI
  commands to view and truncate the debug log.
- **`openclaw graphiti status`** now includes the last 20 debug log entries.
- **`debug`** config option (default: `true`) to enable/disable the debug log.
- **`logFile`** config option to customize the debug log file path.
- All client methods (`search`, `ingest`, `getMemory`, `healthy`, `episodes`)
  now log structured diagnostics: status codes, timing, result counts, and
  errors.
- Lifecycle hooks (`before_agent_start`, `before_compaction`, `before_reset`)
  log skip reasons, capture counts, and timing.
- `episodes()` errors are now observable via the debug log instead of being
  silently swallowed (still returns `[]` for backward compatibility).

## 0.3.1

### Fixed

- Remove failing `plugin.kind === "memory"` assertion from test suite.

## 0.3.0

### Breaking

- **Removed `kind: "memory"` slot claim** from `openclaw.plugin.json` and `index.ts`.
  Graphiti no longer occupies the exclusive `plugins.slots.memory` slot, so
  `memory-core` (or any other memory plugin) can now run alongside it without being
  auto-disabled.

  **Migration:** If you were relying on Graphiti as your memory slot provider (i.e.
  `plugins.slots.memory = "graphiti"`), you must now explicitly re-enable `memory-core`:

  ```json
  {
    "plugins": {
      "slots": { "memory": "memory-core" },
      "entries": { "memory-core": { "enabled": true } }
    }
  }
  ```

  All Graphiti tools (`graphiti_search`, `graphiti_ingest`), hooks (`before_compaction`,
  `before_reset`), and the CLI still function identically — only the slot claim is removed.

### Changed

- **Graphiti is now a complementary plugin, not a replacement memory backend.**
  The recommended setup is `memory-core` as the slot owner (providing `memory_search`
  and `memory_get` over workspace Markdown files) with Graphiti running alongside for
  temporal knowledge graph queries via `graphiti_search`. Both operate independently
  on different data sources: file-based notes vs. entity/relationship graph.

- **Memory CLI bridge** (`openclaw memory status`) is retained for backwards
  compatibility but is now a no-op redirect — `memory-core` handles the `memory`
  CLI directly when it holds the slot.


## 0.2.1

### Added

- **Remote setup docs**: README now covers non-localhost deployments, including
  custom URLs, external Neo4j instances, and authentication.
- **Auth docs**: README documents the `apiKey` option with config examples for
  reverse proxy and API gateway scenarios.
- **Auto-recall docs**: New "Auto-recall vs on-demand search" section explains
  the tradeoff and recommends on-demand `graphiti_search` as the default approach.

### Changed

- **`autoRecall` defaults to `false`** (was `true`): Auto-recall injected facts on
  every turn regardless of relevance, adding token cost and context noise. The
  recommended approach is now on-demand search via the `graphiti_search` tool. Set
  `autoRecall: true` to opt back in.
- **`recallMaxFacts` defaults to `1`** (was `10`): When auto-recall is enabled,
  injecting fewer facts reduces noise. Increase as needed.

## 0.2.0

First-class OpenClaw memory slot integration.

### Breaking

- Removed `captureRoles` config field (was declared in manifest but never read by plugin code).

### Added

- **Plugin discovery**: `openclaw.extensions` field in `package.json` so OpenClaw can
  auto-discover the plugin from `~/.openclaw/extensions/` and workspace directories
  without requiring explicit `plugins.load.paths`.
- **Memory CLI bridge**: Registers `openclaw memory status|index|search` commands via
  `api.runtime.tools.registerMemoryCli()`. When Graphiti holds the memory slot and
  memory-core is auto-disabled, users retain access to `openclaw memory status` for
  built-in file-based memory reporting.
- **UI hints**: `uiHints` in manifest for all config properties (`url`, `groupId`,
  `autoRecall`, `autoCapture`, `recallMaxFacts`, `minPromptLength`). Settings UI now
  renders labels, help text, and marks advanced fields.
- **Status stats**: `openclaw graphiti status` now shows episode count and last
  capture timestamp when the graph has data.
- **Optional auth header**: New `apiKey` config field. When set, all HTTP requests
  to the Graphiti server include an `Authorization: Bearer <apiKey>` header. Useful
  for reverse proxy / API gateway scenarios. Marked `sensitive` in UI hints.
- **npm publish ready**: Package renamed to `@robertogongora/graphiti`. The
  unscoped part (`graphiti`) matches the manifest `id`, which is required by
  OpenClaw's `plugins install` command. Added `files` array to `package.json`
  for clean tarball. Install via `openclaw plugins install @robertogongora/graphiti`.
- **Test suite**: 41 tests across 4 files (`test/plugin.test.ts`,
  `test/client.test.ts`, `test/tools.test.ts`, `test/hooks.test.ts`) using Vitest
  with a shared mock HTTP server (`test/helpers.ts`) matching the real Graphiti
  FastAPI contract. Covers plugin shape, registration, GraphitiClient, tool
  execution, and hook behavior.

### Fixed

- Replaced `/tmp/graphiti-plugin.log` file logging with `api.logger` throughout.
  All log output now flows through the standard OpenClaw log stream instead of
  writing to an external file.
- `GraphitiMessage.role` is now required (was `role?: string`). The Graphiti server's
  Pydantic model has no default for this field and returns 422 if omitted.
- `GraphitiClient.ingest()` documents that the server returns HTTP 202 (Accepted),
  not 200. `GraphitiClient.episodes()` documents the bare-array response format.
- `openclaw graphiti` (no subcommand) now prints help and exits cleanly instead of
  exiting with code 1.

### Removed

- `plugins/` directory (contained symlinked reference copies of `memory-core` and
  `memory-lancedb` used during development; not shipped with the plugin).

### Changed

- Auto-capture doc comment corrected: fires on `before_compaction` and `before_reset`,
  not `after_compaction`.

## 0.1.0

Initial release.

- `graphiti_search` and `graphiti_ingest` tools.
- Auto-recall via `before_agent_start` hook.
- Auto-capture via `before_compaction` and `before_reset` hooks.
- CLI: `openclaw graphiti status|search|episodes`.
- Slash command: `/graphiti`.
- Service with health check on start.
