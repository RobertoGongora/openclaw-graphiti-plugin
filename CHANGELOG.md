# Changelog

## 0.2.0 (unreleased)

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
- **Peer dependency**: `openclaw >= 2026.1.26` declared so package managers can
  validate compatibility.
- **Status stats**: `openclaw graphiti status` now shows episode count and last
  capture timestamp when the graph has data.
- **Optional auth header**: New `apiKey` config field. When set, all HTTP requests
  to the Graphiti server include an `Authorization: Bearer <apiKey>` header. Useful
  for reverse proxy / API gateway scenarios. Marked `sensitive` in UI hints.
- **npm publish ready**: Package renamed from `@openclaw-plugins/graphiti` to
  `openclaw-graphiti-plugin` (unscoped, no npm org required). Added `files` array
  to `package.json` for clean tarball. Install via `openclaw plugins install openclaw-graphiti-plugin`.
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
