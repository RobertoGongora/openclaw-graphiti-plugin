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
 * - CLI: `openclaw graphiti status|search|episodes|ingest|logs|backfill`
 * - CLI bridge: `openclaw memory status` (built-in file-based memory)
 * - Slash command: /graphiti
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import { Type } from "@sinclair/typebox";
import path from "node:path";
import os from "node:os";
import { GraphitiClient, formatEpisodeCount, type GraphitiEpisode } from "./client.js";
import { DebugLog, NOOP_LOG } from "./debug-log.js";
import { resolveMemoryWrite, ingestIndexEpisode, scanMemoryFiles, readIndexState, writeIndexState, readMemoryFileMeta, buildIndexContent, indexEpisodeName, isIndexableFile, DEFAULT_INDEX_EXTENSIONS } from "./memory-index.js";
import { buildProvenance, extractTextsFromMessages, buildEpisodeName, formatFactsAsContext, sanitizeForCapture, type SessionMeta } from "./shared.js";
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
  threadId?: string;
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
  /**
   * File extensions to index (default: [".md", ".txt"]). Only relevant when autoIndex is enabled.
   * The schema says array, but a comma-separated string (".md, .txt") is tolerated.
   */
  autoIndexExtensions?: string[] | string;
}

/** Config after defaults and coercion — the single object every consumer reads. */
export interface NormalizedConfig {
  url: string;
  groupId: string;
  autoRecall: boolean;
  autoCapture: boolean;
  recallMaxFacts: number;
  minPromptLength: number;
  apiKey?: string;
  autoIndex: boolean;
  autoIndexExtensions: string[];
  debug: boolean;
  logFile?: string;
}

/** Cap for the session-start map; the oldest entry is evicted past this. */
export const MAX_SESSION_STARTS = 1000;

/** Flush backfill progress to the index state file every N indexed files. */
const BACKFILL_FLUSH_EVERY = 25;

/**
 * Coerce `autoIndexExtensions` to a normalised string array. Accepts an array
 * or a comma-separated string; drops non-string / empty entries. Returns
 * `coerced: true` when the input was not already a clean string array.
 */
export function normalizeIndexExtensions(raw: unknown): { extensions: string[]; coerced: boolean } {
  if (raw === undefined || raw === null) return { extensions: [...DEFAULT_INDEX_EXTENSIONS], coerced: false };

  let coerced = false;
  let items: unknown[];
  if (Array.isArray(raw)) {
    items = raw;
  } else if (typeof raw === "string") {
    items = raw.split(",");
    coerced = true;
  } else {
    return { extensions: [...DEFAULT_INDEX_EXTENSIONS], coerced: true };
  }

  const extensions: string[] = [];
  for (const item of items) {
    const e = typeof item === "string" ? item.trim().toLowerCase() : "";
    if (!e || e === ".") { coerced = true; continue; }
    extensions.push(e.startsWith(".") ? e : `.${e}`);
  }
  if (extensions.length === 0) return { extensions: [...DEFAULT_INDEX_EXTENSIONS], coerced: true };
  return { extensions, coerced };
}

/** Parse a CLI `--limit` value; returns null unless it is a positive integer. */
function parseLimit(raw: string): number | null {
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : null;
}

const graphitiPlugin = {
  id: "graphiti",
  name: "Graphiti Knowledge Graph",
  description: "Temporal knowledge graph for persistent agent memory",
  kind: "context-engine" as const,

  register(api: OpenClawPluginApi) {
    const raw = (api.pluginConfig ?? {}) as PluginConfig;
    const indexExt = normalizeIndexExtensions(raw.autoIndexExtensions);
    if (indexExt.coerced) {
      api.logger.warn(
        `graphiti: autoIndexExtensions should be an array of strings like [".md", ".txt"]; ` +
        `coerced to ${JSON.stringify(indexExt.extensions)}`,
      );
    }
    // One normalised config: tools, hooks, CLI and the ContextEngine all read this.
    const config: NormalizedConfig = {
      url: raw.url ?? "http://localhost:8100",
      groupId: raw.groupId ?? "core",
      autoRecall: raw.autoRecall === true,
      autoCapture: raw.autoCapture !== false,
      recallMaxFacts: raw.recallMaxFacts ?? 10,
      minPromptLength: raw.minPromptLength ?? 10,
      apiKey: raw.apiKey,
      autoIndex: raw.autoIndex !== false,
      autoIndexExtensions: indexExt.extensions,
      debug: raw.debug !== false,
      logFile: raw.logFile,
    };
    const { url, groupId, autoRecall, autoCapture, recallMaxFacts, minPromptLength, apiKey, autoIndex, autoIndexExtensions } = config;
    const debugLog = config.debug ? new DebugLog(config.logFile) : NOOP_LOG;
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
      if (ctx.threadId) meta.threadId = ctx.threadId;
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
          groupIds: Type.Optional(
            Type.Array(Type.String(), { description: "Search across specific group IDs (default: current group)" })
          ),
        }),
        async execute(_toolCallId, params) {
          const { query, limit = 10, groupIds } = params as { query: string; limit?: number; groupIds?: string[] };

          try {
            const facts = await client.search(query, limit, groupIds);

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

    api.registerTool(
      {
        name: "graphiti_forget",
        label: "Graphiti Forget",
        description:
          "Delete a fact or episode from the knowledge graph. " +
          "Deletion is irreversible. Delete directly by UUID (supports both facts and episodes). " +
          "A query never deletes on its own: it lists the matching facts with their UUIDs so you can call again " +
          "with the uuid (or repeat the query with confirm: true when exactly one fact matches). " +
          "Query-based search only supports facts — to delete an episode, use its UUID directly.",
        parameters: Type.Object({
          query: Type.Optional(Type.String({ description: "Search query to find the fact/episode to delete" })),
          uuid: Type.Optional(Type.String({ description: "Direct UUID of the fact/episode to delete" })),
          type: Type.Optional(
            Type.Union([Type.Literal("fact"), Type.Literal("episode")], {
              description: "Type of entity to delete (default: fact)",
              default: "fact",
            })
          ),
          confirm: Type.Optional(
            Type.Boolean({ description: "Query path only: set true to delete when the query matches exactly one fact (default: false — list matches, delete nothing)" })
          ),
        }),
        async execute(_toolCallId, params) {
          const { query, uuid, type = "fact", confirm = false } = params as {
            query?: string; uuid?: string; type?: "fact" | "episode"; confirm?: boolean;
          };

          if (!query && !uuid) {
            return {
              content: [{ type: "text", text: "Please provide either a query or uuid parameter." }],
              details: { deleted: false, reason: "missing_params" },
            };
          }

          try {
            if (uuid) {
              // Validate UUID format before sending to the server (defense-in-depth for destructive endpoint)
              const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
              if (!UUID_RE.test(uuid)) {
                return {
                  content: [{ type: "text", text: `Invalid UUID format: "${uuid}". Expected format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` }],
                  details: { deleted: false, reason: "invalid_uuid", uuid },
                };
              }
              if (type === "episode") {
                await client.deleteEpisode(uuid);
              } else {
                await client.deleteEdge(uuid);
              }
              return {
                content: [{ type: "text", text: `Deleted ${type} ${uuid}.` }],
                details: { deleted: true, uuid, type },
              };
            }

            // Query-based search only supports facts — episode-by-query is not yet implemented
            if (type === "episode") {
              return {
                content: [{
                  type: "text",
                  text: `Query-based episode deletion is not supported. Use a UUID to delete an episode directly, or use graphiti_episodes to find the episode UUID first.`,
                }],
                details: { deleted: false, reason: "episode_query_not_supported" },
              };
            }

            const facts = await client.search(query!, 10);
            if (facts.length === 0) {
              return {
                content: [{ type: "text", text: `No matching facts found for "${query}".` }],
                details: { deleted: false, reason: "no_matches" },
              };
            }

            // A fuzzy search hit is not consent to an irreversible delete:
            // without confirm, report the candidate and delete nothing.
            if (facts.length === 1 && confirm !== true) {
              return {
                content: [{
                  type: "text",
                  text:
                    `Found 1 matching fact — nothing deleted yet:\n\n` +
                    `1. [${facts[0].uuid}] **${facts[0].name}**: ${facts[0].fact}\n\n` +
                    `To delete it, call graphiti_forget again with uuid "${facts[0].uuid}" (or the same query with confirm: true).`,
                }],
                details: { deleted: false, reason: "confirmation_required", uuid: facts[0].uuid, type: "fact" },
              };
            }

            if (facts.length === 1) {
              await client.deleteEdge(facts[0].uuid);
              return {
                content: [{
                  type: "text",
                  text: `Deleted fact: "${facts[0].name}: ${facts[0].fact}" (${facts[0].uuid}).`,
                }],
                details: { deleted: true, uuid: facts[0].uuid, type: "fact" },
              };
            }

            const list = facts
              .map((f, i) => `${i + 1}. [${f.uuid}] **${f.name}**: ${f.fact}`)
              .join("\n");
            return {
              content: [{
                type: "text",
                text: `Found ${facts.length} matching facts. Please specify a UUID to delete:\n\n${list}`,
              }],
              details: { deleted: false, reason: "multiple_matches", count: facts.length },
            };
          } catch (err) {
            return {
              content: [{
                type: "text",
                text: `Graphiti forget failed: ${err instanceof Error ? err.message : String(err)}`,
              }],
              details: { deleted: false, reason: "error", error: err instanceof Error ? err.message : String(err) },
            };
          }
        },
      },
      { name: "graphiti_forget" },
    );

    api.registerTool(
      {
        name: "graphiti_episodes",
        label: "Graphiti Episodes",
        description:
          "List recent episodes (ingestion records) from the knowledge graph. " +
          "Useful for understanding what has been captured and when. " +
          "When filtering by sessionKey, more episodes are fetched server-side to compensate for client-side filtering.",
        parameters: Type.Object({
          limit: Type.Optional(
            Type.Number({ description: "Max episodes to return (default: 10, max: 50)", minimum: 1, maximum: 50 })
          ),
          sessionKey: Type.Optional(
            Type.String({ description: "Filter episodes by session key" })
          ),
        }),
        async execute(_toolCallId, params) {
          const { limit = 10, sessionKey } = params as { limit?: number; sessionKey?: string };

          try {
            // When filtering by sessionKey, fetch more episodes server-side to
            // compensate for the client-side filter reducing the result set.
            const fetchLimit = sessionKey ? Math.min(limit * 5, 250) : limit;
            let eps = await client.episodes(fetchLimit);

            if (sessionKey) {
              eps = eps.filter((ep: GraphitiEpisode) => {
                try {
                  const prov = JSON.parse(ep.source_description ?? "");
                  return prov.session_key === sessionKey;
                } catch {
                  return ep.source_description?.includes(`session=${sessionKey}`) ||
                    ep.name?.includes(sessionKey);
                }
              }).slice(0, limit);
            }

            if (eps.length === 0) {
              return {
                content: [{ type: "text", text: "No episodes found." }],
                details: { count: 0 },
              };
            }

            const lines = eps.map((ep: GraphitiEpisode, i: number) => {
              let desc = "";
              try {
                const prov = JSON.parse(ep.source_description ?? "");
                const parts: string[] = [];
                if (prov.event) parts.push(`event=${prov.event}`);
                if (prov.session_key) parts.push(`session=${prov.session_key}`);
                if (prov.file) parts.push(`file=${prov.file}`);
                desc = parts.join(" ");
              } catch {
                desc = ep.source_description ?? "";
              }
              const age = ep.created_at ? formatTimeAgo(ep.created_at) : "";
              const content = ep.content ? ep.content.slice(0, 100) : "";
              return `${i + 1}. **uuid: ${ep.uuid}** ${ep.name ? `**${ep.name}**` : "(unnamed)"} ${desc} (${age})${content ? `\n   ${content}` : ""}`;
            });

            return {
              content: [{ type: "text", text: `${eps.length} episode(s):\n\n${lines.join("\n")}` }],
              details: { count: eps.length },
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Graphiti episodes failed: ${err instanceof Error ? err.message : String(err)}` }],
              details: { count: 0, reason: "error", error: err instanceof Error ? err.message : String(err) },
            };
          }
        },
      },
      { name: "graphiti_episodes" },
    );

    // ========================================================================
    // ContextEngine registration (OpenClaw v2026.3.7+)
    // ========================================================================

    const registerContextEngine = api.registerContextEngine;
    const hasEngineSupport = typeof registerContextEngine === "function";

    if (registerContextEngine && hasEngineSupport) {
      registerContextEngine.call(api, "graphiti", () =>
        new GraphitiContextEngine(client, config, groupId, debugLog, api.logger),
      );
    } else {
      debugLog.log("register", { contextEngine: false, reason: "api_version" });
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

          api.logger.info?.(`graphiti: recalled ${facts.length} facts for context injection`);
          debugLog.log("recall", { group: groupId, count: facts.length, ms: Date.now() - start });

          return { prependContext: formatFactsAsContext(facts) };
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

          const episode = sanitizeForCapture(texts.join("\n\n")).slice(0, 12000);

          await client.ingest([{
            content: episode,
            role_type: "user",
            role: "conversation",
            name: buildEpisodeName("compaction", meta),
            timestamp: new Date().toISOString(),
            source_description: buildProvenance(groupId, {
              event: "before_compaction",
              session_key: meta.sessionKey,
              thread_id: meta.threadId,
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
          const sample = sanitizeForCapture(texts.slice(-20).join("\n\n"));

          await client.ingest([{
            content: sample.slice(0, 12000),
            role_type: "user",
            role: "conversation",
            name: buildEpisodeName("session-reset", meta),
            timestamp: new Date().toISOString(),
            source_description: buildProvenance(groupId, {
              event: "before_reset",
              session_key: meta.sessionKey,
              thread_id: meta.threadId,
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
        // Evict the oldest entries at the cap — clearing would wipe the
        // provenance of every live session.
        sessionStarts.delete(ctx.sessionId);
        while (sessionStarts.size >= MAX_SESSION_STARTS) {
          sessionStarts.delete(sessionStarts.keys().next().value as string);
        }
        sessionStarts.set(ctx.sessionId, new Date().toISOString());
      }
    });

    // Auto-index: create index episodes for memory file writes
    if (autoIndex) {
      api.on("after_tool_call", async (event: any) => {
        if (event.error) return;

        // Only files inside THIS workspace's memory dir count: a write to
        // another checkout's memory/ folder must not index the local file.
        const write = resolveMemoryWrite(event.toolName, event.params, api.resolvePath("memory"));
        if (!write) return;
        const memPath = write.filePath;

        if (!isIndexableFile(memPath, autoIndexExtensions)) {
          debugLog.log("mem-index", { skipped: true, reason: "extension_filtered", file: memPath });
          return;
        }

        try {
          const healthy = await client.healthy();
          if (!healthy) return;

          await ingestIndexEpisode({
            client,
            filePath: memPath,
            absolutePath: write.absolutePath,
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
            console.log(`  Episodes: ${formatEpisodeCount(stats.count)}`);
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
            const limit = parseLimit(opts.limit);
            if (limit === null) {
              console.error(`Invalid --limit "${opts.limit}": expected a positive integer`);
              process.exitCode = 1;
              return;
            }
            try {
              const facts = await client.search(query, limit);
              if (facts.length === 0) { console.log("No facts found."); return; }
              for (const f of facts) {
                const valid = f.valid_at ?? "ongoing";
                const invalid = f.invalid_at ? ` \u2192 ${f.invalid_at}` : "";
                console.log(`\u2022 ${f.name}: ${f.fact}  [${valid}${invalid}]`);
              }
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
            const limit = parseLimit(opts.limit);
            if (limit === null) {
              console.error(`Invalid --limit "${opts.limit}": expected a positive integer`);
              process.exitCode = 1;
              return;
            }
            let eps = await client.episodes(limit);
            // NOTE: --session-key filters the fetched set client-side.
            // If your session has many episodes and --limit is low, increase
            // --limit or use --json to retrieve all and filter externally.
            const sessionKey = opts.sessionKey;
            if (sessionKey) {
              eps = eps.filter((ep: GraphitiEpisode) => {
                try {
                  const prov = JSON.parse(ep.source_description ?? "");
                  return prov.session_key === sessionKey;
                } catch {
                  // Legacy plain-text format fallback
                  return ep.source_description?.includes(`session=${sessionKey}`) ||
                    ep.name?.includes(sessionKey);
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
              let filteredCount = 0;
              for (const f of files) {
                if (!isIndexableFile(f, autoIndexExtensions)) {
                  filteredCount++;
                  console.log(`  [filtered] ${f}`);
                  continue;
                }
                const absPath = path.join(memoryDir, path.relative(prefix, f));
                const meta = readMemoryFileMeta(absPath);
                const existing = state[f];
                if (!existing) { newCount++; console.log(`  [new] ${f}`); }
                else if (meta && existing.lastModified !== meta.lastModified) { updatedCount++; console.log(`  [updated] ${f}`); }
                else { unchangedCount++; console.log(`  [unchanged] ${f}`); }
              }
              console.log(`\nDry run: ${files.length} files (${newCount} new, ${updatedCount} updated, ${unchangedCount} unchanged, ${filteredCount} filtered)`);
              return;
            }

            const ok = await client.healthy();
            if (!ok) { console.log("Graphiti server unreachable. Aborting backfill."); return; }

            // Read state once and accumulate updates. Progress is flushed every
            // BACKFILL_FLUSH_EVERY files and in `finally`, so a failure (or a
            // crash) never loses what was already ingested — otherwise the next
            // run would ingest those files again as duplicate episodes.
            const state = readIndexState(stateDir);
            let indexed = 0;
            let skipped = 0;
            let unreadable = 0;
            let filtered = 0;
            let failed = 0;
            let unflushed = 0;
            try {
              for (const f of files) {
                if (!isIndexableFile(f, autoIndexExtensions)) { filtered++; continue; }
                const absPath = path.join(memoryDir, path.relative(prefix, f));
                const meta = readMemoryFileMeta(absPath);
                if (!meta) { unreadable++; continue; }

                const existing = state[f];
                if (existing && existing.lastModified === meta.lastModified) {
                  skipped++;
                  continue;
                }

                try {
                  const episodeContent = buildIndexContent(f, meta.lastModified, meta.excerpt, meta.fileSize);
                  const fileType = path.extname(f).toLowerCase() || "unknown";
                  await client.ingest([{
                    content: episodeContent,
                    role_type: "system",
                    role: "memory-index",
                    name: indexEpisodeName(f),
                    timestamp: meta.lastModified,
                    source_description: buildProvenance(groupId, { event: "memory_index", file: f, file_type: fileType }),
                  }]);
                } catch (err) {
                  failed++;
                  console.error(`  [failed] ${f}: ${err instanceof Error ? err.message : String(err)}`);
                  continue;
                }

                state[f] = {
                  lastModified: meta.lastModified,
                  lastIndexed: new Date().toISOString(),
                };
                indexed++;
                if (++unflushed >= BACKFILL_FLUSH_EVERY) {
                  writeIndexState(stateDir, state);
                  unflushed = 0;
                }
              }
            } finally {
              if (unflushed > 0) writeIndexState(stateDir, state);
            }
            const parts = [`Indexed ${indexed} files`];
            if (skipped) parts.push(`${skipped} unchanged`);
            if (unreadable) parts.push(`${unreadable} unreadable`);
            if (filtered) parts.push(`${filtered} filtered`);
            if (failed) parts.push(`${failed} failed`);
            console.log(parts.length > 1 ? `${parts[0]} (${parts.slice(1).join(", ")})` : parts[0]);
            if (failed) process.exitCode = 1;
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
