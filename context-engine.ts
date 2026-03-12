/**
 * ContextEngine implementation for the Graphiti knowledge graph.
 *
 * When the host OpenClaw version supports registerContextEngine (v2026.3.7+),
 * this replaces the sidecar hooks (before_agent_start, before_compaction,
 * before_reset) with first-class assemble/ingest/compact methods.
 */

import { createRequire } from "node:module";
import type { GraphitiClient, GraphitiMessage } from "./client.js";
import type { DebugLog } from "./debug-log.js";
import { buildEpisodeName, buildProvenance, extractTextContent, extractTextsFromMessages, formatFactsAsContext, DEFAULT_MAX_EPISODE_CHARS } from "./shared.js";

const _require = createRequire(import.meta.url);
const PLUGIN_VERSION: string = (_require("./package.json") as any).version;

// ---------------------------------------------------------------------------
// Types from openclaw/plugin-sdk — declared locally so the plugin compiles
// without a hard dependency on a specific OpenClaw version.
// ---------------------------------------------------------------------------

export interface ContextEngineInfo {
  id: string;
  name: string;
  version: string;
  ownsCompaction: boolean;
}

export interface IngestResult {
  ingested: boolean;
}

export interface IngestBatchResult {
  ingestedCount: number;
}

export interface AssembleResult {
  messages: unknown[];
  systemPromptAddition?: string;
  estimatedTokens?: number;
}

export interface CompactResult {
  ok: boolean;
  compacted: boolean;
  reason?: string;
}

export interface BootstrapResult {
  bootstrapped: boolean;
  reason?: string;
  episodeCount?: number;
}

export interface SubagentSpawnPreparation {
  systemPromptAddition?: string;
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

interface PluginConfig {
  recallMaxFacts?: number;
  autoRecall?: boolean;
  autoCapture?: boolean;
  debug?: boolean;
  maxEpisodeChars?: number;
  modelMaxContextChars?: number;
  /** Allow extension without breaking. */
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// GraphitiContextEngine
// ---------------------------------------------------------------------------

export class GraphitiContextEngine {
  readonly info: ContextEngineInfo = {
    id: "graphiti",
    name: "Graphiti Knowledge Graph",
    version: PLUGIN_VERSION,
    ownsCompaction: true,
  };

  /** Cached healthy() result with TTL to avoid redundant HTTP round-trips. */
  private _healthCache: { result: boolean; ts: number } | null = null;
  private static readonly HEALTH_CACHE_TTL_MS = 15_000;

  private maxEpisodeChars: number;
  private modelMaxContextChars: number | undefined;

  constructor(
    private client: GraphitiClient,
    private cfg: PluginConfig,
    private groupId: string,
    private debugLog: DebugLog,
    private logger?: { info?: (...args: any[]) => void; warn: (...args: any[]) => void },
  ) {
    this.maxEpisodeChars = cfg.maxEpisodeChars ?? DEFAULT_MAX_EPISODE_CHARS;
    this.modelMaxContextChars = cfg.modelMaxContextChars;
  }

  /** Health check with short TTL cache (15s). */
  private async cachedHealthy(): Promise<boolean> {
    const now = Date.now();
    if (this._healthCache && now - this._healthCache.ts < GraphitiContextEngine.HEALTH_CACHE_TTL_MS) {
      return this._healthCache.result;
    }
    const result = await this.client.healthy();
    this._healthCache = { result, ts: now };
    return result;
  }

  /**
   * Ingest with a single retry: on failure, if modelMaxContextChars is set,
   * truncate each episode's content and retry once.
   */
  private async ingestWithRetry(episodes: Parameters<GraphitiClient["ingest"]>[0]): Promise<void> {
    try {
      await this.client.ingest(episodes);
    } catch (err) {
      if (this.modelMaxContextChars == null) throw err;
      const truncated = episodes.map((ep) => ({
        ...ep,
        content: ep.content.slice(0, this.modelMaxContextChars!),
      }));
      this.debugLog.log("ce-ingestRetry", { originalChars: episodes[0]?.content.length, truncatedTo: this.modelMaxContextChars });
      await this.client.ingest(truncated);
    }
  }

  // ========================================================================
  // Required methods
  // ========================================================================

  /**
   * Ingest a single message into the knowledge graph.
   */
  async ingest(params: {
    sessionId: string;
    message: { role: string; content: unknown };
    isHeartbeat?: boolean;
  }): Promise<IngestResult> {
    if (params.isHeartbeat) {
      this.debugLog.log("ce-ingest", { skipped: true, reason: "heartbeat" });
      return { ingested: false };
    }

    const { role, content } = params.message;
    if (role !== "user" && role !== "assistant") {
      this.debugLog.log("ce-ingest", { skipped: true, reason: "role", role });
      return { ingested: false };
    }

    const text = extractTextContent(content);
    if (!text) {
      this.debugLog.log("ce-ingest", { skipped: true, reason: "short_content" });
      return { ingested: false };
    }

    try {
      const start = Date.now();
      await this.ingestWithRetry([{
        content: `${role}: ${text}`,
        role_type: role as "user" | "assistant",
        role,
        name: `ingest-${params.sessionId}-${Date.now()}`,
        timestamp: new Date().toISOString(),
        source_description: buildProvenance(this.groupId, { event: "ingest" }),
      }]);

      this.debugLog.log("ce-ingest", { ingested: true, role, ms: Date.now() - start });
      return { ingested: true };
    } catch (err) {
      this.logger?.warn(`graphiti: ingest failed: ${String(err)}`);
      this.debugLog.log("ce-ingest", { error: String(err) });
      return { ingested: false };
    }
  }

  /**
   * Assemble context for the agent by recalling relevant facts.
   * Uses the richer /get-memory endpoint with the full message array.
   */
  async assemble(params: {
    sessionId: string;
    messages: unknown[];
    tokenBudget?: number;
  }): Promise<AssembleResult> {
    const passThrough: AssembleResult = { messages: params.messages };
    const start = Date.now();

    try {
      const healthy = await this.cachedHealthy();
      if (!healthy) {
        this.debugLog.log("ce-assemble", { skipped: true, reason: "unhealthy" });
        return passThrough;
      }

      // Convert the last N messages to GraphitiMessage format for /get-memory
      const recentMessages = params.messages.slice(-10);
      const graphitiMessages: GraphitiMessage[] = [];

      for (const msg of recentMessages) {
        if (!msg || typeof msg !== "object") continue;
        const m = msg as Record<string, unknown>;
        const role = m.role as string;
        if (role !== "user" && role !== "assistant") continue;

        const text = extractTextContent(m.content, 1);
        if (!text) continue;

        graphitiMessages.push({
          content: text,
          role_type: role as "user" | "assistant",
          role,
        });
      }

      if (graphitiMessages.length === 0) {
        this.debugLog.log("ce-assemble", { skipped: true, reason: "no_messages" });
        return passThrough;
      }

      const maxFacts = this.cfg.recallMaxFacts ?? 10;
      const facts = await this.client.getMemory(graphitiMessages, maxFacts);

      if (facts.length === 0) {
        this.debugLog.log("ce-assemble", { count: 0, ms: Date.now() - start });
        return passThrough;
      }

      const systemPromptAddition = formatFactsAsContext(facts);

      const estimatedTokens = Math.ceil(systemPromptAddition.length / 4);

      this.logger?.info?.(`graphiti: assembled ${facts.length} facts for context`);
      this.debugLog.log("ce-assemble", { count: facts.length, tokens: estimatedTokens, ms: Date.now() - start });

      return {
        messages: params.messages,
        systemPromptAddition,
        estimatedTokens,
      };
    } catch (err) {
      this.logger?.warn(`graphiti: assemble failed: ${String(err)}`);
      this.debugLog.log("ce-assemble", { error: String(err), ms: Date.now() - start });
      return passThrough;
    }
  }

  /**
   * Graph-aware compaction: ingest messages being compacted, then signal
   * the runtime that it's safe to truncate. The graph holds long-term memory.
   */
  async compact(params: {
    sessionId: string;
    sessionFile?: string;
    tokenBudget?: number;
    force?: boolean;
    messages?: unknown[];
    legacyParams?: { bridge?: { compact: () => Promise<any> } };
  }): Promise<CompactResult> {
    try {
      if (params.messages) {
        const healthy = await this.cachedHealthy();
        if (!healthy) {
          if (params.legacyParams?.bridge) {
            this.debugLog.log("ce-compact", { action: "legacy_bridge_fallback" });
            await params.legacyParams.bridge.compact();
            return { ok: true, compacted: true };
          }
          this.debugLog.log("ce-compact", { action: "unhealthy", compacted: false });
          return { ok: true, compacted: false, reason: "server-unhealthy" };
        }

        const texts = extractTextsFromMessages(params.messages);
        if (texts.length === 0) {
          // No user/assistant text to preserve — safe to signal compaction complete.
          // The graph already holds any knowledge from prior ingestion cycles.
          this.debugLog.log("ce-compact", { action: "no_text", compacted: true });
          return { ok: true, compacted: true };
        }

        const episode = texts.join("\n\n").slice(0, this.maxEpisodeChars);
        await this.ingestWithRetry([{
          content: episode,
          role_type: "user",
          role: "conversation",
          name: buildEpisodeName("compact", { sessionKey: params.sessionId }),
          timestamp: new Date().toISOString(),
          source_description: buildProvenance(this.groupId, { event: "compact", session_key: params.sessionId }),
        }]);

        this.debugLog.log("ce-compact", { action: "ingested", texts: texts.length, compacted: true });
        return { ok: true, compacted: true };
      }

      if (params.legacyParams?.bridge) {
        this.debugLog.log("ce-compact", { action: "legacy_bridge" });
        await params.legacyParams.bridge.compact();
        return { ok: true, compacted: true };
      }

      this.debugLog.log("ce-compact", { action: "no_messages", compacted: false });
      return { ok: true, compacted: false, reason: "no-messages" };
    } catch (err) {
      this.logger?.warn(`graphiti: compact failed: ${String(err)}`);
      this.debugLog.log("ce-compact", { error: String(err) });
      return { ok: false, compacted: false, reason: "error" };
    }
  }

  // ========================================================================
  // Optional methods
  // ========================================================================

  /**
   * Batch-ingest multiple messages at once.
   */
  async ingestBatch(params: {
    sessionId: string;
    messages: Array<{ role: string; content: unknown }>;
    isHeartbeat?: boolean;
  }): Promise<IngestBatchResult> {
    if (params.isHeartbeat) {
      this.debugLog.log("ce-ingestBatch", { skipped: true, reason: "heartbeat" });
      return { ingestedCount: 0 };
    }

    const texts = extractTextsFromMessages(params.messages);
    if (texts.length === 0) {
      this.debugLog.log("ce-ingestBatch", { skipped: true, reason: "no_content" });
      return { ingestedCount: 0 };
    }

    try {
      const start = Date.now();
      const episode = texts.join("\n\n").slice(0, this.maxEpisodeChars);

      await this.ingestWithRetry([{
        content: episode,
        role_type: "user",
        role: "conversation",
        name: buildEpisodeName("batch", { sessionKey: params.sessionId }),
        timestamp: new Date().toISOString(),
        source_description: buildProvenance(this.groupId, {
          event: "ingest_batch",
          session_key: params.sessionId,
        }),
      }]);

      this.debugLog.log("ce-ingestBatch", { count: texts.length, ms: Date.now() - start });
      return { ingestedCount: texts.length };
    } catch (err) {
      this.logger?.warn(`graphiti: ingestBatch failed: ${String(err)}`);
      this.debugLog.log("ce-ingestBatch", { error: String(err) });
      return { ingestedCount: 0 };
    }
  }

  /**
   * After-turn hook: ingest new messages from this turn.
   * When implemented, runtime does NOT call ingestBatch/ingest fallback.
   */
  async afterTurn(params: {
    sessionId: string;
    sessionFile?: string;
    messages: Array<{ role: string; content: unknown }>;
    prePromptMessageCount: number;
    isHeartbeat?: boolean;
    tokenBudget?: number;
  }): Promise<void> {
    if (params.isHeartbeat) {
      this.debugLog.log("ce-afterTurn", { skipped: true, reason: "heartbeat" });
      return;
    }

    const newMessages = params.messages.slice(params.prePromptMessageCount);
    if (newMessages.length === 0) {
      this.debugLog.log("ce-afterTurn", { skipped: true, reason: "no_new_messages" });
      return;
    }

    const texts = extractTextsFromMessages(newMessages);
    if (texts.length === 0) {
      this.debugLog.log("ce-afterTurn", { skipped: true, reason: "no_content" });
      return;
    }

    try {
      const start = Date.now();
      const episode = texts.join("\n\n").slice(0, this.maxEpisodeChars);

      await this.ingestWithRetry([{
        content: episode,
        role_type: "user",
        role: "conversation",
        name: buildEpisodeName("turn", { sessionKey: params.sessionId }),
        timestamp: new Date().toISOString(),
        source_description: buildProvenance(this.groupId, {
          event: "after_turn",
          session_key: params.sessionId,
        }),
      }]);

      this.logger?.info?.(`graphiti: after-turn ingested ${texts.length} messages`);
      this.debugLog.log("ce-afterTurn", { count: texts.length, ms: Date.now() - start });
    } catch (err) {
      this.logger?.warn(`graphiti: afterTurn failed: ${String(err)}`);
      this.debugLog.log("ce-afterTurn", { error: String(err) });
    }
  }

  /**
   * Bootstrap: health-check the server and report graph population.
   */
  async bootstrap(_params: {
    sessionId: string;
    sessionFile?: string;
  }): Promise<BootstrapResult> {
    try {
      const healthy = await this.cachedHealthy();
      if (!healthy) {
        this.debugLog.log("ce-bootstrap", { healthy: false });
        return { bootstrapped: false, reason: "server-unhealthy" };
      }

      const stats = await this.client.episodeCount();
      this.debugLog.log("ce-bootstrap", { healthy: true, episodes: stats.count });
      return { bootstrapped: true, episodeCount: stats.count };
    } catch (err) {
      this.logger?.warn(`graphiti: bootstrap failed: ${String(err)}`);
      this.debugLog.log("ce-bootstrap", { error: String(err) });
      return { bootstrapped: false, reason: "error" };
    }
  }

  /**
   * Called when a subagent finishes. Ingests its findings into the parent's graph scope.
   */
  async onSubagentEnded(params: {
    childSessionKey: string;
    reason: string;
    summary?: string;
    messages?: unknown[];
  }): Promise<void> {
    const hasSummary = typeof params.summary === "string" && params.summary.length >= 20;

    if (!hasSummary && (!params.messages || params.messages.length === 0)) {
      this.debugLog.log("ce-subagentEnded", {
        child: params.childSessionKey,
        reason: params.reason,
        action: "no-op",
      });
      return;
    }

    try {
      const healthy = await this.cachedHealthy();
      if (!healthy) {
        this.debugLog.log("ce-subagentEnded", { child: params.childSessionKey, skipped: true, reason: "unhealthy" });
        return;
      }

      let episode: string;
      if (hasSummary) {
        episode = params.summary!.slice(0, this.maxEpisodeChars);
      } else {
        const texts = extractTextsFromMessages(params.messages!);
        if (texts.length === 0) {
          this.debugLog.log("ce-subagentEnded", { child: params.childSessionKey, action: "no_content" });
          return;
        }
        episode = texts.join("\n\n").slice(0, this.maxEpisodeChars);
      }

      await this.ingestWithRetry([{
        content: episode,
        // "system" is a valid GraphitiMessage.role_type — subagent results are
        // neither user nor assistant from the parent conversation's perspective.
        role_type: "system",
        role: "subagent-result",
        name: buildEpisodeName("subagent", { sessionKey: params.childSessionKey }),
        timestamp: new Date().toISOString(),
        source_description: buildProvenance(this.groupId, {
          event: "subagent_ended",
          session_key: params.childSessionKey,
        }),
      }]);

      this.debugLog.log("ce-subagentEnded", {
        child: params.childSessionKey,
        action: hasSummary ? "ingested_summary" : "ingested_messages",
      });
    } catch (err) {
      this.logger?.warn(`graphiti: onSubagentEnded failed: ${String(err)}`);
      this.debugLog.log("ce-subagentEnded", { child: params.childSessionKey, error: String(err) });
    }
  }

  /**
   * Prepare context for a spawning subagent by injecting relevant facts.
   */
  async prepareSubagentSpawn(params: {
    parentSessionKey: string;
    childSessionKey: string;
    ttlMs?: number;
    taskDescription?: string;
  }): Promise<SubagentSpawnPreparation | undefined> {
    if (!params.taskDescription) return undefined;

    try {
      const healthy = await this.cachedHealthy();
      if (!healthy) return undefined;

      const maxFacts = this.cfg.recallMaxFacts ?? 10;
      const facts = await this.client.search(params.taskDescription, maxFacts);
      if (facts.length === 0) return undefined;

      return { systemPromptAddition: formatFactsAsContext(facts) };
    } catch (err) {
      this.logger?.warn(`graphiti: prepareSubagentSpawn failed: ${String(err)}`);
      this.debugLog.log("ce-prepareSubagentSpawn", { error: String(err) });
      return undefined;
    }
  }

  /**
   * Dispose: no-op (no persistent connections).
   */
  dispose(): void {
    // No persistent connections to clean up
  }
}
