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


# Changelog

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
