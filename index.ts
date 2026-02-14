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
 * - Auto-capture: Ingests conversation turns after each exchange (via agent_end)
 * - CLI: `openclaw graphiti status|search|episodes`
 * - Slash command: /graphiti-status
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";
import { GraphitiClient } from "./client.js";
import { appendFileSync } from "node:fs";

interface PluginConfig {
  url?: string;
  groupId?: string;
  autoRecall?: boolean;
  autoCapture?: boolean;
  recallMaxFacts?: number;
  captureRoles?: string[];
  minPromptLength?: number;
}

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
    const captureRoles = cfg.captureRoles ?? ["user", "assistant"];
    const minPromptLength = cfg.minPromptLength ?? 10;

    const client = new GraphitiClient(url, groupId, api.logger);

    api.logger.info(`graphiti: plugin registered (url: ${url}, group: ${groupId})`);

    // Debug: log registration to file
    try {
      appendFileSync("/tmp/graphiti-hook-debug.log",
        `[${new Date().toISOString()}] PLUGIN REGISTER called | url: ${url} | group: ${groupId}\n`
      );
    } catch {}

    // Debug: check what api exposes
    try {
      const apiKeys = Object.keys(api).join(", ");
      appendFileSync("/tmp/graphiti-hook-debug.log",
        `[${new Date().toISOString()}] API KEYS: ${apiKeys}\n`
      );
      // Try to find if there's an internal way to verify registration
      appendFileSync("/tmp/graphiti-hook-debug.log",
        `[${new Date().toISOString()}] api.on type: ${typeof (api as any).on}\n`
      );
    } catch {}

    // Debug: try registerHook (old API) with name option
    try {
      (api as any).registerHook(["before_agent_start"], async (event: any) => {
        appendFileSync("/tmp/graphiti-hook-debug.log",
          `[${new Date().toISOString()}] OLD API before_agent_start FIRED\n`
        );
      }, { name: "graphiti-recall-test" });
      appendFileSync("/tmp/graphiti-hook-debug.log",
        `[${new Date().toISOString()}] registerHook (old API) call succeeded\n`
      );
    } catch (e) {
      appendFileSync("/tmp/graphiti-hook-debug.log",
        `[${new Date().toISOString()}] registerHook (old API) ERROR: ${String(e)}\n`
      );
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
              content: [
                {
                  type: "text",
                  text: `Found ${facts.length} facts:\n\n${text}`,
                },
              ],
              details: {
                count: facts.length,
                facts: facts.map((f) => ({
                  uuid: f.uuid,
                  name: f.name,
                  fact: f.fact,
                  valid_at: f.valid_at,
                })),
              },
            };
          } catch (err) {
            return {
              content: [
                {
                  type: "text",
                  text: `Graphiti search failed: ${err instanceof Error ? err.message : String(err)}`,
                },
              ],
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
          source: Type.Optional(
            Type.String({ description: "Source description (default: manual)" })
          ),
        }),
        async execute(_toolCallId, params) {
          const { content, name, source = "manual" } = params as {
            content: string;
            name?: string;
            source?: string;
          };

          try {
            const result = await client.ingest([
              {
                content,
                role_type: "system",
                role: "shiba",
                name: name ?? `manual-${Date.now()}`,
                timestamp: new Date().toISOString(),
                source_description: `OpenClaw agent: ${source}`,
              },
            ]);

            return {
              content: [
                {
                  type: "text",
                  text: `Ingested into knowledge graph: "${content.slice(0, 100)}${content.length > 100 ? "..." : ""}"`,
                },
              ],
              details: result,
            };
          } catch (err) {
            return {
              content: [
                {
                  type: "text",
                  text: `Graphiti ingest failed: ${err instanceof Error ? err.message : String(err)}`,
                },
              ],
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
      api.logger.info("graphiti: registering before_agent_start hook");
      api.on("before_agent_start", async (event: any) => {
        // Debug: write to file so we can verify the hook fires
        
        appendFileSync("/tmp/graphiti-hook-debug.log",
          `[${new Date().toISOString()}] before_agent_start fired | prompt: ${event.prompt?.slice(0, 100) ?? "NONE"} | keys: ${Object.keys(event).join(",")}\n`
        );
        api.logger.info?.(`graphiti: before_agent_start fired, prompt length: ${event.prompt?.length ?? 0}`);
        if (!event.prompt || event.prompt.length < minPromptLength) {
          return;
        }

        // Skip heartbeat polls and system messages
        if (
          event.prompt.includes("HEARTBEAT") ||
          event.prompt.includes("Pre-compaction memory flush") ||
          event.prompt.includes("GatewayRestart")
        ) {
          return;
        }

        try {
          const healthy = await client.healthy();
          if (!healthy) {
            api.logger.warn("graphiti: server unhealthy, skipping auto-recall");
            return;
          }

          const facts = await client.search(event.prompt, recallMaxFacts);

          if (facts.length === 0) {
            return;
          }

          const context = facts
            .map((f) => `- **${f.name}**: ${f.fact}`)
            .join("\n");

          api.logger.info?.(`graphiti: injecting ${facts.length} facts into context`);

          return {
            prependContext:
              `<graphiti-context>\n` +
              `Relevant knowledge graph facts (auto-recalled):\n` +
              `${context}\n` +
              `</graphiti-context>`,
          };
        } catch (err) {
          api.logger.warn(`graphiti: recall failed: ${String(err)}`);
        }
      });
    }

    // Auto-capture: ingest conversation after agent ends
    if (autoCapture) {
      api.logger.info("graphiti: registering agent_end hook");
      api.on("agent_end", async (event: any) => {
        
        appendFileSync("/tmp/graphiti-hook-debug.log",
          `[${new Date().toISOString()}] agent_end fired | success: ${event.success} | messages: ${event.messages?.length ?? 0}\n`
        );
        // Debug: log filtering decisions
        const _dbgLog = (msg: string) => { try { appendFileSync("/tmp/graphiti-hook-debug.log", `[${new Date().toISOString()}] agent_end: ${msg}\n`); } catch {} };
        api.logger.info?.(`graphiti: agent_end fired, success: ${event.success}, messages: ${event.messages?.length ?? 0}`);
        if (!event.success || !event.messages || event.messages.length === 0) {
          return;
        }

        try {
          const healthy = await client.healthy();
          if (!healthy) {
            api.logger.warn("graphiti: server unhealthy, skipping auto-capture");
            return;
          }

          // Extract text from messages
          const texts: Array<{ role: string; content: string }> = [];
          for (const msg of event.messages) {
            if (!msg || typeof msg !== "object") continue;
            const msgObj = msg as Record<string, unknown>;
            const role = msgObj.role as string;

            if (!captureRoles.includes(role)) continue;

            const content = msgObj.content;
            if (typeof content === "string") {
              texts.push({ role, content });
            } else if (Array.isArray(content)) {
              for (const block of content) {
                if (
                  block &&
                  typeof block === "object" &&
                  "type" in block &&
                  (block as any).type === "text" &&
                  typeof (block as any).text === "string"
                ) {
                  texts.push({ role, content: (block as any).text });
                }
              }
            }
          }

          if (texts.length === 0) { _dbgLog(`SKIP: no texts extracted (roles: ${captureRoles})`); return; }

          // Skip very short exchanges (just acks, heartbeats)
          const totalContent = texts.map((t) => t.content).join(" ");
          if (totalContent.length < 50) { _dbgLog(`SKIP: too short (${totalContent.length} chars)`); return; }

          // Skip system-heavy content
          if (
            totalContent.includes("HEARTBEAT_OK") ||
            totalContent.includes("Pre-compaction memory flush")
          ) {
            _dbgLog("SKIP: system content (heartbeat/flush)");
            return;
          }
          // Skip if the ONLY substantive content is NO_REPLY
          const nonNoReply = totalContent.replace(/NO_REPLY/g, "").trim();
          if (nonNoReply.length < 50) {
            _dbgLog(`SKIP: only NO_REPLY content (remaining: ${nonNoReply.length} chars)`);
            return;
          }

          // Build conversation episode — take last 10 messages max
          const recent = texts.slice(-10);
          const episodeContent = recent
            .map((t) => `${t.role}: ${t.content.slice(0, 2000)}`)
            .join("\n\n");

          // Ingest as conversation episode
          await client.ingest([
            {
              content: episodeContent.slice(0, 12000),
              role_type: "user",
              role: "conversation",
              name: `conversation-${Date.now()}`,
              timestamp: new Date().toISOString(),
              source_description: "OpenClaw auto-capture: agent conversation",
            },
          ]);

          _dbgLog(`INGESTED: ${recent.length} messages, ${episodeContent.length} chars`);
          api.logger.info?.(
            `graphiti: auto-captured ${recent.length} messages (${episodeContent.length} chars)`
          );
        } catch (err) {
          _dbgLog(`ERROR: ${String(err)}`);
          api.logger.warn(`graphiti: capture failed: ${String(err)}`);
        }
      });
    }

    // ========================================================================
    // CLI
    // ========================================================================

    api.registerCli(
      ({ program }) => {
        const cmd = program
          .command("graphiti")
          .description("Graphiti knowledge graph commands");

        cmd
          .command("status")
          .description("Check Graphiti server health")
          .action(async () => {
            const ok = await client.healthy();
            console.log(ok ? "✅ Graphiti is healthy" : "❌ Graphiti unreachable");
            if (ok) {
              console.log(`  URL: ${url}`);
              console.log(`  Group: ${groupId}`);
            }
          });

        cmd
          .command("search")
          .description("Search the knowledge graph")
          .argument("<query>", "Search query")
          .option("-n, --limit <n>", "Max results", "10")
          .action(async (query: string, opts: { limit: string }) => {
            const facts = await client.search(query, parseInt(opts.limit));
            if (facts.length === 0) {
              console.log("No facts found.");
              return;
            }
            for (const f of facts) {
              console.log(`• ${f.name}: ${f.fact}`);
            }
          });

        cmd
          .command("episodes")
          .description("List recent episodes")
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
        api.logger.info(
          `graphiti: service started (healthy: ${ok}, url: ${url}, group: ${groupId})`
        );
      },
      stop() {
        api.logger.info("graphiti: service stopped");
      },
    });
  },
};

export default graphitiPlugin;
