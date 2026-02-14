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
 * - Auto-capture: Ingests compaction summaries (via after_compaction)
 * - CLI: `openclaw graphiti status|search|episodes`
 * - Slash command: /graphiti
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";
import { GraphitiClient } from "./client.js";
import { appendFileSync, readFileSync } from "node:fs";

interface PluginConfig {
  url?: string;
  groupId?: string;
  autoRecall?: boolean;
  autoCapture?: boolean;
  recallMaxFacts?: number;
  minPromptLength?: number;
}

const LOG_FILE = "/tmp/graphiti-plugin.log";
const log = (msg: string) => {
  try { appendFileSync(LOG_FILE, `[${new Date().toISOString()}] ${msg}\n`); } catch {}
};

const graphitiPlugin = {
  id: "graphiti",
  name: "Graphiti Knowledge Graph",
  description: "Temporal knowledge graph for persistent agent memory",
  kind: "memory" as const,

  register(api: OpenClawPluginApi) {
    const cfg = (api.pluginConfig ?? {}) as PluginConfig;
    const url = cfg.url ?? "http://localhost:8100";
    const groupId = cfg.groupId ?? "shiba-core";
    const autoRecall = cfg.autoRecall !== false;
    const autoCapture = cfg.autoCapture !== false;
    const recallMaxFacts = cfg.recallMaxFacts ?? 10;
    const minPromptLength = cfg.minPromptLength ?? 10;

    const client = new GraphitiClient(url, groupId, api.logger);

    api.logger.info(`graphiti: plugin registered (url: ${url}, group: ${groupId})`);
    log(`REGISTER | url: ${url} | group: ${groupId}`);

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

    api.registerTool(
      {
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
        async execute(_toolCallId, params) {
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
              source_description: `OpenClaw agent: ${source}`,
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
      },
      { name: "graphiti_ingest" },
    );

    // ========================================================================
    // Lifecycle Hooks
    // ========================================================================

    // Auto-recall: inject relevant facts before agent starts
    if (autoRecall) {
      api.on("before_agent_start", async (event: any) => {
        if (!event.prompt || event.prompt.length < minPromptLength) return;

        // Skip system-only sessions
        if (
          event.prompt.includes("HEARTBEAT") ||
          event.prompt.includes("boot check")
        ) return;

        try {
          const healthy = await client.healthy();
          if (!healthy) return;

          const facts = await client.search(event.prompt, recallMaxFacts);
          if (facts.length === 0) return;

          const context = facts.map((f) => `- **${f.name}**: ${f.fact}`).join("\n");
          log(`RECALL: ${facts.length} facts injected`);

          return {
            prependContext:
              `<graphiti-context>\nRelevant knowledge graph facts (auto-recalled):\n${context}\n</graphiti-context>`,
          };
        } catch (err) {
          log(`RECALL ERROR: ${String(err)}`);
        }
      });
    }

    // Auto-capture: ingest compaction summaries into the knowledge graph
    // Compaction summaries are distilled conversation context — perfect for extraction.
    // This fires when OpenClaw compacts a long session, not on every turn.
    if (autoCapture) {
      api.on("after_compaction", async (event: any) => {
        log(`COMPACTION: keys=${Object.keys(event).join(",")}, sessionFile=${event.sessionFile ?? "none"}`);

        // Read the compaction summary from the session file.
        // After compaction, the first user message contains the summary.
        const sessionFile = event.sessionFile;
        if (!sessionFile) { log("COMPACTION SKIP: no sessionFile"); return; }

        try {
          const healthy = await client.healthy();
          if (!healthy) { log("COMPACTION SKIP: unhealthy"); return; }

          // Read JSONL session file, find the compaction summary
          // It's typically a user message containing "<summary>" or the compacted context
          const lines = readFileSync(sessionFile, "utf-8").split("\n").filter(Boolean);
          
          // Look for the summary — it's usually the last user message after compaction
          // which contains the compacted conversation context
          let summary = "";
          for (const line of lines.slice(-5)) {
            try {
              const entry = JSON.parse(line);
              const msg = entry?.message ?? entry;
              if (msg?.role === "user" && typeof msg?.content === "string" && msg.content.length > 200) {
                // The compaction summary is the longest recent user message
                if (msg.content.length > summary.length) {
                  summary = msg.content;
                }
              }
            } catch {}
          }

          if (summary.length < 100) {
            log(`COMPACTION SKIP: no summary found in session file (checked ${lines.length} lines)`);
            return;
          }

          await client.ingest([{
            content: summary.slice(0, 12000),
            role_type: "system",
            role: "shiba",
            name: `compaction-${Date.now()}`,
            timestamp: new Date().toISOString(),
            source_description: "OpenClaw auto-capture: compaction summary",
          }]);

          log(`COMPACTION INGESTED: ${summary.length} chars`);
          api.logger.info?.(`graphiti: ingested compaction summary (${summary.length} chars)`);
        } catch (err) {
          log(`COMPACTION ERROR: ${String(err)}`);
        }
      });

      // Also capture on session reset (/new) — the before_reset hook includes messages
      // that are about to be lost, so we can extract knowledge before they disappear
      api.on("before_reset", async (event: any) => {
        log(`RESET: keys=${Object.keys(event).join(",")}, messages=${event.messages?.length ?? 0}`);

        // Only capture if there's meaningful content
        if (!event.messages || event.messages.length < 4) {
          log("RESET SKIP: too few messages");
          return;
        }

        try {
          const healthy = await client.healthy();
          if (!healthy) return;

          // Extract user+assistant text from the last messages
          const texts: string[] = [];
          for (const msg of event.messages) {
            if (!msg || typeof msg !== "object") continue;
            const msgObj = msg as Record<string, unknown>;
            const role = msgObj.role as string;
            if (role !== "user" && role !== "assistant") continue;
            const content = msgObj.content;
            if (typeof content === "string" && content.length > 20) {
              texts.push(`${role}: ${content.slice(0, 1000)}`);
            }
          }

          if (texts.length < 2) { log("RESET SKIP: not enough text"); return; }

          // Take a sample — last 20 exchanges max
          const sample = texts.slice(-20).join("\n\n");

          await client.ingest([{
            content: sample.slice(0, 12000),
            role_type: "user",
            role: "conversation",
            name: `session-reset-${Date.now()}`,
            timestamp: new Date().toISOString(),
            source_description: "OpenClaw auto-capture: session reset",
          }]);

          log(`RESET INGESTED: ${texts.length} messages, ${sample.length} chars`);
        } catch (err) {
          log(`RESET ERROR: ${String(err)}`);
        }
      });
    }

    // ========================================================================
    // CLI
    // ========================================================================

    api.registerCli(
      ({ program }) => {
        const cmd = program.command("graphiti").description("Graphiti knowledge graph commands");

        cmd.command("status").description("Check Graphiti server health").action(async () => {
          const ok = await client.healthy();
          console.log(ok ? "✅ Graphiti is healthy" : "❌ Graphiti unreachable");
          if (ok) { console.log(`  URL: ${url}`); console.log(`  Group: ${groupId}`); }
        });

        cmd.command("search").description("Search the knowledge graph")
          .argument("<query>", "Search query")
          .option("-n, --limit <n>", "Max results", "10")
          .action(async (query: string, opts: { limit: string }) => {
            const facts = await client.search(query, parseInt(opts.limit));
            if (facts.length === 0) { console.log("No facts found."); return; }
            for (const f of facts) { console.log(`• ${f.name}: ${f.fact}`); }
          });

        cmd.command("episodes").description("List recent episodes")
          .option("-n, --limit <n>", "How many", "10")
          .action(async (opts: { limit: string }) => {
            const eps = await client.episodes(parseInt(opts.limit));
            console.log(JSON.stringify(eps, null, 2));
          });
      },
      { commands: ["graphiti"] },
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
