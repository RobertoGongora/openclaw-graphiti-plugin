/**
 * OpenClaw Graphiti Plugin
 *
 * Temporal knowledge graph memory for AI conversations.
 * Uses Graphiti (by Zep AI) for entity extraction, relationship tracking,
 * and semantic search over a Neo4j graph database.
 *
 * Provides:
 * - graphiti_search: Semantic + graph search over extracted facts
 * - graphiti_ingest: Manual episode ingestion
 * - Auto-recall: Injects relevant facts before each conversation (via before_agent_start)
 * - Auto-capture: Ingests conversation content before compaction/reset
 * - CLI: `openclaw graphiti status|search|episodes|ingest`
 * - CLI bridge: `openclaw memory status` (built-in file-based memory)
 * - Slash command: /graphiti
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";
import path from "node:path";
import os from "node:os";
import { GraphitiClient } from "./client.js";
import { DebugLog, NOOP_LOG } from "./debug-log.js";
import { extractMemoryPath, upsertIndexEpisode, scanMemoryFiles, readIndexState, writeIndexState, readMemoryFileMeta, buildIndexContent, indexEpisodeName } from "./memory-index.js";
import { buildProvenance, extractTextsFromMessages, buildEpisodeName, type SessionMeta } from "./shared.js";
import { GraphitiContextEngine } from "./context-engine.js";

// Re-export public types from shared.ts for backwards compatibility
export type { SessionMeta } from "./shared.js";
export { buildEpisodeName } from "./shared.js";

function formatTimeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return "just now";
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Minimal context shape passed by OpenClaw to lifecycle hooks. */
interface HookContext {
  sessionKey?: string;
  sessionId?: string;
  agentId?: string;
  messageProvider?: string;
  messageChannel?: string;
  [key: string]: unknown; // allow extension without breaking
}

interface PluginConfig {
  url?: string;
  groupId?: string;
  autoRecall?: boolean;
  autoCapture?: boolean;
  recallMaxFacts?: number;
  minPromptLength?: number;
  /** Optional API key sent as Bearer token for reverse proxy auth. */
  apiKey?: string;
  /** Enable debug log file (default: true). */
  debug?: boolean;
  /** Custom path for the debug log file. */
  logFile?: string;
  /** Create index episodes when files are written to memory/ (default: true). */
  autoIndex?: boolean;
}

const graphitiPlugin = {
  id: "graphiti",
  name: "Graphiti Knowledge Graph",
  description: "Temporal knowledge graph for persistent agent memory",
  kind: "context-engine" as const,

  register(api: OpenClawPluginApi) {
    const cfg = (api.pluginConfig ?? {}) as PluginConfig;
    const url = cfg.url ?? "http://localhost:8100";
    const groupId = cfg.groupId ?? "core";
    const autoRecall = cfg.autoRecall === true;
    const autoCapture = cfg.autoCapture !== false;
    const recallMaxFacts = cfg.recallMaxFacts ?? 1;
    const minPromptLength = cfg.minPromptLength ?? 10;
    const apiKey = cfg.apiKey;
    const autoIndex = cfg.autoIndex !== false;
    const debugLog = cfg.debug !== false ? new DebugLog(cfg.logFile) : NOOP_LOG;
    const stateDir = path.join(os.homedir(), ".openclaw", "state", "graphiti");

    const client = new GraphitiClient(url, groupId, api.logger, apiKey, debugLog);

    // Session start timestamps keyed by sessionId for metadata enrichment.
    const sessionStarts = new Map<string, string>();

    /**
     * Extract session metadata from hook/tool context for provenance enrichment.
     * Returns whatever fields are available; missing fields are omitted from provenance.
     */
    function sessionMetaFromCtx(ctx: HookContext | undefined): SessionMeta {
      const meta: SessionMeta = {};
      if (!ctx) return meta;
      if (ctx.sessionKey) meta.sessionKey = ctx.sessionKey;
      if (ctx.agentId) meta.agent = ctx.agentId;
      if (ctx.messageProvider) meta.channel = ctx.messageProvider;
      else if (ctx.messageChannel) meta.channel = ctx.messageChannel;
      if (ctx.sessionId && sessionStarts.has(ctx.sessionId)) {
        meta.sessionStart = sessionStarts.get(ctx.sessionId);
      }
      return meta;
    }

    // ========================================================================
    // Tools
    // ========================================================================

    api.registerTool(
      {
        name: "graphiti_search",
        label: "Graphiti Search",
        description:
          "Search the knowledge graph for facts, entities, and relationships. " +
          "Use when you need context about people, projects, decisions, infrastructure, " +
          "or anything discussed in past conversations.",
        parameters: Type.Object({
          query: Type.String({ description: "Natural language search query" }),
          limit: Type.Optional(
            Type.Number({ description: "Max results (default: 10)", minimum: 1, maximum: 50 })
          ),
        }),
        async execute(_toolCallId, params) {
          const { query, limit = 10 } = params as { query: string; limit?: number };

          try {
            const facts = await client.search(query, limit);

            if (facts.length === 0) {
              return {
                content: [{ type: "text", text: "No relevant facts found in the knowledge graph." }],
                details: { count: 0 },
              };
            }

            const text = facts
              .map((f, i) => `${i + 1}. **${f.name}**: ${f.fact} [${f.valid_at ?? "ongoing"}]`)
              .join("\n");

            return {
              content: [{ type: "text", text: `Found ${facts.length} facts:\n\n${text}` }],
              details: {
                count: facts.length,
                facts: facts.map((f) => ({ uuid: f.uuid, name: f.name, fact: f.fact, valid_at: f.valid_at })),
              },
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Graphiti search failed: ${err instanceof Error ? err.message : String(err)}` }],
            };
          }
        },
      },
      { name: "graphiti_search" },
    );

    // Factory pattern: graphiti_ingest receives tool context (session/agent/channel)
    // so every manual ingest episode carries full provenance metadata.
    api.registerTool(
      (toolCtx: any) => {
        const meta = sessionMetaFromCtx(toolCtx ?? {});
        return {
          name: "graphiti_ingest",
          label: "Graphiti Ingest",
          description:
            "Manually ingest information into the knowledge graph. " +
            "Use for important facts, decisions, or context that should be remembered long-term.",
          parameters: Type.Object({
            content: Type.String({ description: "Content to ingest (rich natural language)" }),
            name: Type.Optional(Type.String({ description: "Episode name/label" })),
            source: Type.Optional(Type.String({ description: "Source description (default: manual)" })),
          }),
          async execute(_toolCallId: string, params: any) {
            const { content, name, source = "manual" } = params as {
              content: string; name?: string; source?: string;
            };

            try {
              const result = await client.ingest([{
                content,
                role_type: "system",
                role: "shiba",
                name: name ?? `manual-${Date.now()}`,
                timestamp: new Date().toISOString(),
                source_description: buildProvenance(groupId, {
                  event: "manual",
                  source,
                  session_key: meta.sessionKey,
                  agent: meta.agent,
                  channel: meta.channel,
                  session_start: meta.sessionStart,
                }),
              }]);

              return {
                content: [{ type: "text", text: `Ingested into knowledge graph: "${content.slice(0, 100)}${content.length > 100 ? "..." : ""}"` }],
                details: result,
              };
            } catch (err) {
              return {
                content: [{ type: "text", text: `Graphiti ingest failed: ${err instanceof Error ? err.message : String(err)}` }],
              };
            }
          },
        };
      },
      { name: "graphiti_ingest" },
    );

    // ========================================================================
    // ContextEngine registration (OpenClaw v2026.3.7+)
    // ========================================================================

    const hasEngineSupport = typeof (api as any).registerContextEngine === "function";

    if (hasEngineSupport) {
      (api as any).registerContextEngine("graphiti", () =>
        new GraphitiContextEngine(client, cfg, groupId, debugLog, api.logger),
      );
    }

    // ========================================================================
    // Lifecycle Hooks (skipped when ContextEngine handles them)
    // ========================================================================

    // Auto-recall: inject relevant facts before agent starts
    if (!hasEngineSupport && autoRecall) {
      api.on("before_agent_start", async (event: any) => {
        if (!event.prompt || event.prompt.length < minPromptLength) {
          debugLog.log("recall", { skipped: true, reason: "prompt_too_short", length: event.prompt?.length ?? 0 });
          return;
        }

        // Skip system-only sessions
        if (
          event.prompt.includes("HEARTBEAT") ||
          event.prompt.includes("boot check")
        ) {
          debugLog.log("recall", { skipped: true, reason: "heartbeat_or_boot_check" });
          return;
        }

        const start = Date.now();
        try {
          const healthy = await client.healthy();
          if (!healthy) return;

          const facts = await client.search(event.prompt, recallMaxFacts);
          if (facts.length === 0) return;

          const context = facts.map((f) => `- **${f.name}**: ${f.fact}`).join("\n");
          api.logger.info?.(`graphiti: recalled ${facts.length} facts for context injection`);
          debugLog.log("recall", { group: groupId, count: facts.length, ms: Date.now() - start });

          return {
            prependContext:
              `<graphiti-context>\nRelevant knowledge graph facts (auto-recalled):\n${context}\n</graphiti-context>`,
          };
        } catch (err) {
          api.logger.warn(`graphiti: recall failed: ${String(err)}`);
        }
      });
    }

    // Auto-capture: ingest conversations into the knowledge graph before compaction/reset.
    // This fires when OpenClaw compacts a long session, not on every turn.
    // Skipped when ContextEngine is active (afterTurn + compact handle this).
    if (!hasEngineSupport && autoCapture) {
      api.on("before_compaction", async (event: any, ctx: HookContext | undefined) => {
        const meta = sessionMetaFromCtx(ctx ?? {});
        if (!meta.sessionKey && event.sessionKey) meta.sessionKey = event.sessionKey;

        // Ingest the raw conversation BEFORE the agent compacts it.
        // Graphiti runs its own entity extraction (gpt-5-nano) and should
        // work from raw material, not pre-distilled summaries.
        const messages = event.messages;
        if (!messages || !Array.isArray(messages) || messages.length < 4) {
          debugLog.log("capture", { skipped: true, reason: "too_few_messages" });
          return;
        }

        const start = Date.now();
        try {
          const healthy = await client.healthy();
          if (!healthy) return;

          const texts = extractTextsFromMessages(messages);
          if (texts.length < 2) return;

          const episode = texts.join("\n\n").slice(0, 12000);

          await client.ingest([{
            content: episode,
            role_type: "user",
            role: "conversation",
            name: buildEpisodeName("compaction", meta),
            timestamp: new Date().toISOString(),
            source_description: buildProvenance(groupId, {
              event: "before_compaction",
              session_key: meta.sessionKey,
              agent: meta.agent,
              channel: meta.channel,
              session_start: meta.sessionStart,
            }),
          }]);

          api.logger.info?.(`graphiti: ingested pre-compaction conversation (${texts.length} messages, ${episode.length} chars)`);
          debugLog.log("capture", { status: 202, group: groupId, session: meta.sessionKey, messages: texts.length, ms: Date.now() - start });
        } catch (err) {
          api.logger.warn(`graphiti: compaction capture failed: ${String(err)}`);
        }
      });

      // Also capture on session reset (/new) — the before_reset hook includes messages
      // that are about to be lost, so we can extract knowledge before they disappear.
      api.on("before_reset", async (event: any, ctx: HookContext | undefined) => {
        const meta = sessionMetaFromCtx(ctx ?? {});
        if (!meta.sessionKey && event.sessionKey) meta.sessionKey = event.sessionKey;
        if (!event.messages || !Array.isArray(event.messages) || event.messages.length < 4) {
          debugLog.log("reset", { skipped: true, reason: "too_few_messages" });
          return;
        }

        const start = Date.now();
        try {
          const healthy = await client.healthy();
          if (!healthy) return;

          const texts = extractTextsFromMessages(event.messages, { maxPerMessage: 1000 });
          if (texts.length < 2) return;

          // Take a sample — last 20 exchanges max
          const sample = texts.slice(-20).join("\n\n");

          await client.ingest([{
            content: sample.slice(0, 12000),
            role_type: "user",
            role: "conversation",
            name: buildEpisodeName("session-reset", meta),
            timestamp: new Date().toISOString(),
            source_description: buildProvenance(groupId, {
              event: "before_reset",
              session_key: meta.sessionKey,
              agent: meta.agent,
              channel: meta.channel,
              session_start: meta.sessionStart,
            }),
          }]);

          api.logger.info?.(`graphiti: ingested session-reset conversation (${texts.length} messages, ${sample.length} chars)`);
          debugLog.log("reset", { status: 202, group: groupId, session: meta.sessionKey, messages: texts.length, ms: Date.now() - start });
        } catch (err) {
          api.logger.warn(`graphiti: reset capture failed: ${String(err)}`);
        }
      });
    }

    // Session start tracking (always registered): records the session start
    // timestamp so subsequent capture hooks can embed it in provenance metadata.
    api.on("session_start", async (_event: any, ctx: HookContext | undefined) => {
      if (ctx?.sessionId) {
        if (sessionStarts.size >= 1000) sessionStarts.clear();
        sessionStarts.set(ctx.sessionId, new Date().toISOString());
      }
    });

    // Auto-index: create index episodes for memory file writes
    if (autoIndex) {
      api.on("after_tool_call", async (event: any) => {
        if (event.error) return;

        const memPath = extractMemoryPath(event.toolName, event.params);
        if (!memPath) return;

        try {
          const healthy = await client.healthy();
          if (!healthy) return;

          const absolutePath = api.resolvePath(memPath);
          await upsertIndexEpisode({
            client,
            filePath: memPath,
            absolutePath,
            groupId,
            debugLog,
            stateDir,
          });
        } catch (err) {
          api.logger.warn(`graphiti: memory index failed: ${String(err)}`);
        }
      });
    }

    // ========================================================================
    // CLI
    // ========================================================================

    api.registerCli(
      ({ program }) => {
        const cmd = program.command("graphiti").description("Graphiti knowledge graph commands");
        cmd.action(() => { cmd.outputHelp(); });

        cmd.command("status").description("Check Graphiti server health").action(async () => {
          const ok = await client.healthy();
          console.log(ok ? "✅ Graphiti is healthy" : "❌ Graphiti unreachable");
          if (!ok) return;
          console.log(`  URL: ${url}`);
          console.log(`  Group: ${groupId}`);
          const stats = await client.episodeCount();
          if (stats.count > 0) {
            const countLabel = stats.count >= 10000 ? "10000+" : String(stats.count);
            console.log(`  Episodes: ${countLabel}`);
          }
          if (stats.latestAt) {
            console.log(`  Last capture: ${formatTimeAgo(stats.latestAt)}`);
          }
          const tail = debugLog.tail(20);
          if (tail) {
            console.log(`\nRecent debug log (${debugLog.filePath}):`);
            console.log(tail);
          }
        });

        cmd.command("search").description("Search the knowledge graph")
          .argument("<query>", "Search query")
          .option("-n, --limit <n>", "Max results", "10")
          .action(async (query: string, opts: { limit: string }) => {
            try {
              const facts = await client.search(query, parseInt(opts.limit));
              if (facts.length === 0) { console.log("No facts found."); return; }
              for (const f of facts) { console.log(`• ${f.name}: ${f.fact}`); }
            } catch (err) {
              console.error(`Search failed: ${err instanceof Error ? err.message : String(err)}`);
              process.exitCode = 1;
            }
          });

        cmd.command("episodes").description("List recent episodes")
          .option("-n, --limit <n>", "How many", "10")
          .option("--json", "Output raw JSON")
          .option("-s, --session-key <key>", "Filter episodes by session key")
          .action(async (opts: { limit: string; json?: boolean; sessionKey?: string }) => {
            let eps = await client.episodes(parseInt(opts.limit));
            // NOTE: --session-key filters the fetched set client-side.
            // If your session has many episodes and --limit is low, increase
            // --limit or use --json to retrieve all and filter externally.
            if (opts.sessionKey) {
              eps = eps.filter((ep: any) => {
                try {
                  const prov = JSON.parse(ep.source_description ?? "");
                  return prov.session_key === opts.sessionKey;
                } catch {
                  // Legacy plain-text format fallback
                  return ep.source_description?.includes(`session=${opts.sessionKey}`) ||
                    ep.name?.includes(opts.sessionKey);
                }
              });
            }
            if (opts.json) {
              console.log(JSON.stringify(eps, null, 2));
              return;
            }
            if (eps.length === 0) { console.log("No episodes found."); return; }
            for (const ep of eps) {
              let desc = ep.source_description ?? "";
              try {
                const prov = JSON.parse(desc);
                desc = `[${prov.event}]`;
                if (prov.source) desc += ` source=${prov.source}`;
                if (prov.file) desc += ` file=${prov.file}`;
                if (prov.session_key) desc += ` session=${prov.session_key}`;
                if (prov.agent) desc += ` agent=${prov.agent}`;
                if (prov.channel) desc += ` channel=${prov.channel}`;
              } catch { /* legacy plain-text — use as-is */ }
              const age = ep.created_at ? formatTimeAgo(ep.created_at) : "";
              console.log(`• ${ep.name ?? ep.uuid}  ${desc}  ${age}`);
            }
          });

        cmd.command("ingest").description("Ingest a file or text into the knowledge graph")
          .option("--source-file <path>", "Path to file to ingest")
          .option("--content <text>", "Text content to ingest directly")
          .option("--name <label>", "Episode name/label")
          .action(async (opts: { sourceFile?: string; content?: string; name?: string }) => {
            if (!opts.sourceFile && !opts.content) {
              console.error("Provide --source-file or --content");
              process.exitCode = 1;
              return;
            }
            try {
              let content: string;
              let filePath: string | undefined;
              const { resolve, basename } = await import("node:path");
              if (opts.sourceFile) {
                const { readFile } = await import("node:fs/promises");
                filePath = resolve(opts.sourceFile);
                content = await readFile(filePath, "utf-8");
                const MAX_FILE_CHARS = 12_000;
                if (content.length > MAX_FILE_CHARS) {
                  console.warn("File content truncated to 12,000 characters");
                  content = content.slice(0, MAX_FILE_CHARS);
                }
              } else {
                content = opts.content!;
              }
              const label = opts.name ?? (filePath ? basename(filePath) : `cli-${Date.now()}`);
              await client.ingest([{
                content,
                role_type: "system",
                role: "shiba",
                name: label,
                timestamp: new Date().toISOString(),
                source_description: buildProvenance(groupId, {
                  event: "cli_ingest",
                  file: filePath ? basename(filePath) : undefined,
                }),
              }]);
              console.log(`Ingested "${label}" (${content.length} chars)`);
            } catch (err) {
              console.error(`Ingest failed: ${err instanceof Error ? err.message : String(err)}`);
              process.exitCode = 1;
            }
          });

        cmd.command("logs").description("Show debug log")
          .option("--clear", "Truncate the debug log")
          .action(async (opts: { clear?: boolean }) => {
            console.log(`Log file: ${debugLog.filePath}`);
            if (opts.clear) { debugLog.clear(); console.log("Log cleared."); return; }
            const tail = debugLog.tail(50);
            console.log(tail || "(no log entries)");
          });

        cmd.command("backfill").description("Index existing memory files into Graphiti")
          .option("--dir <path>", "Memory directory to scan", "./memory")
          .option("--dry-run", "Show what would be indexed without ingesting")
          .action(async (opts: { dir: string; dryRun?: boolean }) => {
            const memoryDir = path.resolve(opts.dir);
            const prefix = path.basename(memoryDir);
            const files = scanMemoryFiles(memoryDir, prefix);
            if (files.length === 0) {
              console.log(`No files found in ${memoryDir}`);
              return;
            }

            if (opts.dryRun) {
              const state = readIndexState(stateDir);
              let newCount = 0;
              let updatedCount = 0;
              let unchangedCount = 0;
              for (const f of files) {
                const absPath = path.join(memoryDir, path.relative(prefix, f));
                const meta = readMemoryFileMeta(absPath);
                const existing = state[f];
                if (!existing) { newCount++; console.log(`  [new] ${f}`); }
                else if (meta && existing.lastModified !== meta.lastModified) { updatedCount++; console.log(`  [updated] ${f}`); }
                else { unchangedCount++; console.log(`  [unchanged] ${f}`); }
              }
              console.log(`\nDry run: ${files.length} files (${newCount} new, ${updatedCount} updated, ${unchangedCount} unchanged)`);
              return;
            }

            const ok = await client.healthy();
            if (!ok) { console.log("Graphiti server unreachable. Aborting backfill."); return; }

            // Batch state writes: read once, accumulate updates, write once
            const state = readIndexState(stateDir);
            let indexed = 0;
            let skipped = 0;
            for (const f of files) {
              const absPath = path.join(memoryDir, path.relative(prefix, f));
              const meta = readMemoryFileMeta(absPath);
              if (!meta) { skipped++; continue; }

              const existing = state[f];
              if (existing && existing.lastModified === meta.lastModified) {
                skipped++;
                continue;
              }

              const episodeContent = buildIndexContent(f, meta.lastModified, meta.excerpt, meta.fileSize);
              await client.ingest([{
                content: episodeContent,
                role_type: "system",
                role: "memory-index",
                name: indexEpisodeName(f),
                timestamp: meta.lastModified,
                source_description: buildProvenance(groupId, { event: "memory_index", file: f }),
              }]);

              state[f] = {
                lastModified: meta.lastModified,
                lastIndexed: new Date().toISOString(),
              };
              indexed++;
            }
            writeIndexState(stateDir, state);
            console.log(`Indexed ${indexed} files (${skipped} unchanged)`);
          });
      },
      { commands: ["graphiti"] },
    );

    // Bridge: expose built-in memory tools CLI so `openclaw memory status` works
    // even when memory-core is disabled (Graphiti holds the memory slot).
    // This reports on the file-based memory index (MEMORY.md etc.), not Graphiti.
    api.registerCli(
      ({ program }) => {
        api.runtime.tools.registerMemoryCli(program);
      },
      { commands: ["memory"] },
    );

    // ========================================================================
    // Slash Command
    // ========================================================================

    api.registerCommand({
      name: "graphiti",
      description: "Check Graphiti knowledge graph status",
      handler: async () => {
        const ok = await client.healthy();
        return {
          text: ok
            ? `✅ Graphiti healthy\n📍 ${url}\n🏷️ Group: ${groupId}`
            : `❌ Graphiti unreachable at ${url}`,
        };
      },
    });

    // ========================================================================
    // Service
    // ========================================================================

    api.registerService({
      id: "graphiti",
      async start() {
        const ok = await client.healthy();
        api.logger.info(`graphiti: service started (healthy: ${ok}, url: ${url}, group: ${groupId})`);
      },
      stop() { api.logger.info("graphiti: service stopped"); },
    });
  },
};

export default graphitiPlugin;
