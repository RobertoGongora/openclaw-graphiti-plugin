/**
 * ContextEngine implementation for the Graphiti knowledge graph.
 *
 * When the host OpenClaw version supports registerContextEngine (v2026.3.7+),
 * this replaces the sidecar hooks (before_agent_start, before_compaction,
 * before_reset) with first-class assemble/ingest/compact methods.
 */

import { createRequire } from "node:module";
import { BOOTSTRAP_EPISODE_COUNT_CAP } from "./client.js";
import type { GraphitiClient, GraphitiMessage } from "./client.js";
import type { DebugLog } from "./debug-log.js";
import { buildEpisodeName, buildProvenance, extractEpisodeContinuity, extractTextContent, extractTextsFromMessages, formatContinuityBlock, formatFactsAsContext, hasDeicticReferences, isContinuityGap, readSessionFileTail, sanitizeForCapture } from "./shared.js";
import type { LifecycleEvent } from "./shared.js";

const _require = createRequire(import.meta.url);
const PLUGIN_VERSION: string = (_require("./package.json") as any).version;

// ---------------------------------------------------------------------------
// Types from openclaw/plugin-sdk (canonical source: context-engine/types.ts).
// Declared locally so the plugin compiles without a hard dependency on a
// specific OpenClaw version. Keep structurally compatible with the SDK types.
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

/** The subset of the (normalised) plugin config the engine reads. */
export interface EngineConfig {
  recallMaxFacts?: number;
  autoRecall?: boolean;
  autoCapture?: boolean;
}

/** Per-session Smart autoRecall state. */
interface SessionState {
  /** One-shot lifecycle flag consumed by the next assemble() for this session. */
  lastEvent: LifecycleEvent | null;
  sessionFile: string | null;
  threadId: string | null;
}

/** Cap for the per-session maps/sets; the oldest entry is evicted past this. */
export const MAX_TRACKED_SESSIONS = 1000;

// ---------------------------------------------------------------------------
// GraphitiContextEngine
// ---------------------------------------------------------------------------

export class GraphitiContextEngine {
  readonly info: ContextEngineInfo = {
    id: "graphiti",
    name: "Graphiti Knowledge Graph",
    version: PLUGIN_VERSION,
    ownsCompaction: false,
  };

  /** Cached healthy() result with TTL to avoid redundant HTTP round-trips. */
  private _healthCache: { result: boolean; ts: number } | null = null;
  private static readonly HEALTH_CACHE_TTL_MS = 15_000;

  /**
   * Smart autoRecall state, keyed by session id. One engine instance serves
   * every session, so nothing session-specific may live in a scalar field.
   */
  private _sessions = new Map<string, SessionState>();
  private _recalledSessions = new Set<string>();

  constructor(
    private client: GraphitiClient,
    private cfg: EngineConfig,
    private groupId: string,
    private debugLog: DebugLog,
    private logger?: { info?: (...args: any[]) => void; warn: (...args: any[]) => void },
  ) {}

  /**
   * Get (or create) the state for a session and mark it most recently used.
   * Past MAX_TRACKED_SESSIONS the oldest session is evicted — never the whole map.
   */
  private session(sessionId: string): SessionState {
    let state = this._sessions.get(sessionId);
    if (state) {
      this._sessions.delete(sessionId);
    } else {
      state = { lastEvent: null, sessionFile: null, threadId: null };
      while (this._sessions.size >= MAX_TRACKED_SESSIONS) {
        const oldest = this._sessions.keys().next().value as string;
        this._sessions.delete(oldest);
        this.debugLog.log("ce-session", { action: "session_evicted" });
      }
    }
    this._sessions.set(sessionId, state);
    return state;
  }

  /** Thread id recorded for a session, without creating state for unknown ids. */
  private threadIdFor(sessionId: string): string | undefined {
    return this._sessions.get(sessionId)?.threadId ?? undefined;
  }

  /** Signal that the next assemble() should fire recovery for this session. */
  private signalRecovery(sessionId: string, event: LifecycleEvent): void {
    this.session(sessionId).lastEvent = event;
    this._recalledSessions.delete(sessionId);
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
    if (this.cfg.autoCapture === false) {
      this.debugLog.log("ce-ingest", { skipped: true, reason: "autoCapture_disabled" });
      return { ingested: false };
    }

    if (params.isHeartbeat) {
      this.debugLog.log("ce-ingest", { skipped: true, reason: "heartbeat" });
      return { ingested: false };
    }

    const { role, content } = params.message;
    if (role !== "user" && role !== "assistant") {
      this.debugLog.log("ce-ingest", { skipped: true, reason: "role", role });
      return { ingested: false };
    }

    const rawText = extractTextContent(content);
    const text = rawText ? sanitizeForCapture(rawText) : null;
    if (!text || text.length < 20) {
      this.debugLog.log("ce-ingest", { skipped: true, reason: text ? "sanitized_short" : "short_content" });
      return { ingested: false };
    }

    try {
      const start = Date.now();
      await this.client.ingest([{
        content: `${role}: ${text.slice(0, 2000)}`,
        role_type: role as "user" | "assistant",
        role,
        name: `ingest-${params.sessionId}-${Date.now()}`,
        timestamp: new Date().toISOString(),
        source_description: buildProvenance(this.groupId, { event: "ingest", session_key: params.sessionId, thread_id: this.threadIdFor(params.sessionId) }),
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
   * Assemble context for the agent via Smart autoRecall.
   *
   * Only fires when a continuity gap is detected (bootstrap, compaction,
   * few messages) or the user prompt contains deictic references to prior
   * context. Normal turns with sufficient history get no injection.
   *
   * Two-stage pipeline:
   *   A. Continuity recovery — session transcript tail, then episode-based
   *      fallback (filtered by session_key/thread_id provenance)
   *   B. Targeted semantic recall — /search against recovered continuity,
   *      or /get-memory on the current window when no continuity is available
   */
  async assemble(params: {
    sessionId: string;
    messages: unknown[];
    tokenBudget?: number;
    threadId?: string;
  }): Promise<AssembleResult> {
    const passThrough: AssembleResult = { messages: params.messages };
    const start = Date.now();

    try {
      const healthy = await this.cachedHealthy();
      if (!healthy) {
        this.debugLog.log("ce-assemble", { skipped: true, reason: "unhealthy" });
        return passThrough;
      }

      // Respect opt-in config — engine still registers for capture/compaction
      // but recall injection requires explicit autoRecall: true
      if (!this.cfg.autoRecall) {
        this.debugLog.log("ce-assemble", { skipped: true, reason: "autoRecall_disabled" });
        return passThrough;
      }

      const state = this.session(params.sessionId);
      if (params.threadId) state.threadId = params.threadId;

      const lastUserText = this.extractLastUserText(params.messages);
      const gapDetected = isContinuityGap(params.messages.length, { recentEvent: state.lastEvent });
      const deicticDetected = !!lastUserText && hasDeicticReferences(lastUserText);

      // Session-scoped short-circuit: skip recall if this session already received
      // Graphiti context.  Lifecycle events (bootstrap/compact via lastEvent) and
      // deictic references override the gate so recovery still fires when needed.
      // TODO(#171): if the plugin-sdk exposes effective system-prompt contents or
      // placement control for ContextEngine additions, replace this session-scoped
      // short-circuit with direct Graphiti-tag detection or append-based placement.
      if (this._recalledSessions.has(params.sessionId) && !state.lastEvent && !deicticDetected) {
        this.debugLog.log("ce-assemble", { skipped: true, reason: "already_injected" });
        return passThrough;
      }

      if (!gapDetected && !deicticDetected) {
        this.debugLog.log("ce-assemble", { skipped: true, reason: "no_recovery_needed" });
        return passThrough;
      }

      // Consume the one-shot event flag.  Safe: JS is single-threaded so no
      // concurrent assemble() can read between capture and clear.
      const triggerEvent = state.lastEvent;
      state.lastEvent = null;
      const maxFacts = this.cfg.recallMaxFacts ?? 10;

      // Stage A: recover continuity from session transcript
      let continuityBlock: string | null = null;
      let continuityTail: string | null = null;
      if (state.sessionFile) {
        continuityTail = await readSessionFileTail(state.sessionFile);
        if (continuityTail) {
          continuityBlock = formatContinuityBlock(continuityTail);
        }
      }

      // Stage A fallback: episode-based recovery when session file is empty/missing
      if (!continuityTail) {
        const episodes = await this.client.episodes(20);
        const episodeText = extractEpisodeContinuity(episodes, params.sessionId, {
          threadId: state.threadId ?? undefined,
        });
        if (episodeText) {
          continuityTail = episodeText;
          continuityBlock = formatContinuityBlock(episodeText);
        }
      }

      // Stage B: semantic recall — use recovered continuity as the query
      // so facts are relevant to what was actually discussed, not to the
      // possibly-empty current message window
      let semanticBlock: string | null = null;
      if (continuityTail) {
        const query = continuityTail.slice(-2000);
        const facts = await this.client.search(query, maxFacts);
        if (facts.length > 0) {
          semanticBlock = formatFactsAsContext(facts);
        }
      } else {
        const graphitiMessages = this.buildGraphitiMessages(params.messages);
        if (graphitiMessages.length > 0) {
          const facts = await this.client.getMemory(graphitiMessages, maxFacts);
          if (facts.length > 0) {
            semanticBlock = formatFactsAsContext(facts);
          }
        }
      }

      // --- Combine ---
      const parts = [continuityBlock, semanticBlock].filter(Boolean) as string[];
      if (parts.length === 0) {
        this.debugLog.log("ce-assemble", { recovery: true, trigger: triggerEvent ?? "deictic", empty: true, ms: Date.now() - start });
        return passThrough;
      }

      const systemPromptAddition = parts.join("\n\n");
      const estimatedTokens = Math.ceil(systemPromptAddition.length / 4);
      const trigger = triggerEvent ?? (deicticDetected ? "deictic" : "gap");

      this.logger?.info?.(`graphiti: smart autoRecall fired (trigger=${trigger}, continuity=${!!continuityBlock}, facts=${semanticBlock ? "yes" : "none"})`);
      this.debugLog.log("ce-assemble", {
        recovery: true,
        trigger,
        hasContinuity: !!continuityBlock,
        factCount: semanticBlock ? (semanticBlock.match(/^- \*\*/gm)?.length ?? 0) : 0,
        tokens: estimatedTokens,
        ms: Date.now() - start,
      });

      // Re-insert so the set stays ordered by last recall, then evict the oldest
      // entries past the cap (never clear: that would re-inject every live session).
      this._recalledSessions.delete(params.sessionId);
      while (this._recalledSessions.size >= MAX_TRACKED_SESSIONS) {
        const oldest = this._recalledSessions.values().next().value as string;
        this._recalledSessions.delete(oldest);
        this.debugLog.log("ce-assemble", { action: "recalled_session_evicted" });
      }
      this._recalledSessions.add(params.sessionId);
      return { messages: params.messages, systemPromptAddition, estimatedTokens };
    } catch (err) {
      this.logger?.warn(`graphiti: assemble failed: ${String(err)}`);
      this.debugLog.log("ce-assemble", { error: String(err), ms: Date.now() - start });
      return passThrough;
    }
  }

  /** Convert raw messages to GraphitiMessage format for /get-memory. */
  private buildGraphitiMessages(messages: unknown[]): GraphitiMessage[] {
    const recentMessages = messages.slice(-10);
    const graphitiMessages: GraphitiMessage[] = [];

    for (const msg of recentMessages) {
      if (!msg || typeof msg !== "object") continue;
      const m = msg as Record<string, unknown>;
      const role = m.role as string;
      if (role !== "user" && role !== "assistant") continue;

      const text = extractTextContent(m.content, 1);
      if (!text) continue;

      graphitiMessages.push({
        content: text.slice(0, 2000),
        role_type: role as "user" | "assistant",
        role,
      });
    }

    return graphitiMessages;
  }

  /** Extract the text of the last user message for deictic reference detection. */
  private extractLastUserText(messages: unknown[]): string | null {
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (!msg || typeof msg !== "object") continue;
      const m = msg as Record<string, unknown>;
      if (m.role !== "user") continue;
      return extractTextContent(m.content, 1);
    }
    return null;
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
    customInstructions?: string; // reserved for future runtimeContext.compactBuiltIn callback
    runtimeContext?: Record<string, unknown>; // reserved for future runtimeContext.compactBuiltIn callback
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

        const episode = sanitizeForCapture(texts.join("\n\n")).slice(0, 12000);
        await this.client.ingest([{
          content: episode,
          role_type: "user",
          role: "conversation",
          name: buildEpisodeName("compact", { sessionKey: params.sessionId }),
          timestamp: new Date().toISOString(),
          source_description: buildProvenance(this.groupId, { event: "compact", session_key: params.sessionId, thread_id: this.threadIdFor(params.sessionId) }),
        }]);

        this.signalRecovery(params.sessionId, "compact");
        this.debugLog.log("ce-compact", { action: "ingested", texts: texts.length, compacted: true });
        return { ok: true, compacted: true };
      }

      if (params.legacyParams?.bridge) {
        this.signalRecovery(params.sessionId, "compact");
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
   *
   * The messages are joined into a single episode; `ingestedCount` is the
   * number of episodes committed (0 or 1), not the number of input messages.
   */
  async ingestBatch(params: {
    sessionId: string;
    messages: Array<{ role: string; content: unknown }>;
    isHeartbeat?: boolean;
  }): Promise<IngestBatchResult> {
    if (this.cfg.autoCapture === false) {
      this.debugLog.log("ce-ingestBatch", { skipped: true, reason: "autoCapture_disabled" });
      return { ingestedCount: 0 };
    }

    if (params.isHeartbeat) {
      this.debugLog.log("ce-ingestBatch", { skipped: true, reason: "heartbeat" });
      return { ingestedCount: 0 };
    }

    const rawTexts = extractTextsFromMessages(params.messages);
    const texts = rawTexts.map(sanitizeForCapture).filter(t => t.length > 0);
    if (texts.length === 0) {
      this.debugLog.log("ce-ingestBatch", { skipped: true, reason: "no_content" });
      return { ingestedCount: 0 };
    }

    try {
      const start = Date.now();
      const episode = texts.join("\n\n").slice(0, 12000);

      await this.client.ingest([{
        content: episode,
        role_type: "user",
        role: "conversation",
        name: buildEpisodeName("batch", { sessionKey: params.sessionId }),
        timestamp: new Date().toISOString(),
        source_description: buildProvenance(this.groupId, {
          event: "ingest_batch",
          session_key: params.sessionId,
          thread_id: this.threadIdFor(params.sessionId),
        }),
      }]);

      // All messages are joined into ONE episode, so one episode was committed.
      this.debugLog.log("ce-ingestBatch", { episodes: 1, messages: texts.length, ms: Date.now() - start });
      return { ingestedCount: 1 };
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
    // Keep session state fresh for Smart autoRecall
    if (params.sessionId && params.sessionFile) {
      this.session(params.sessionId).sessionFile = params.sessionFile;
    }

    if (this.cfg.autoCapture === false) {
      this.debugLog.log("ce-afterTurn", { skipped: true, reason: "autoCapture_disabled" });
      return;
    }

    if (params.isHeartbeat) {
      this.debugLog.log("ce-afterTurn", { skipped: true, reason: "heartbeat" });
      return;
    }

    // When Pi auto-compaction runs during a prompt, prePromptMessageCount
    // reflects the pre-compaction count while messages is post-compaction.
    // Detect this mismatch and sweep all messages to avoid losing the
    // current turn's content from the graph.
    // Note: when compaction removes exactly `turnMessageCount` messages,
    // prePromptMessageCount === messages.length and this check is false.
    // Using >= would cause false-positive sweeps on empty turns. The rare
    // missed content is captured on the next afterTurn cycle.
    const compactionOccurred = params.prePromptMessageCount > params.messages.length;
    // Sweep may re-ingest messages already captured in a prior afterTurn call.
    // Intentional: preventing data loss is more important than deduplication here.
    // Use the "after_turn_sweep" provenance event to identify and filter these.
    const newMessages = compactionOccurred
      ? params.messages
      : params.messages.slice(params.prePromptMessageCount);

    if (newMessages.length === 0) {
      this.debugLog.log("ce-afterTurn", { skipped: true, reason: "no_new_messages" });
      return;
    }

    const texts = extractTextsFromMessages(newMessages);
    if (texts.length === 0) {
      this.debugLog.log("ce-afterTurn", { skipped: true, reason: "no_content" });
      return;
    }

    const event = compactionOccurred ? "after_turn_sweep" : "after_turn";

    try {
      const start = Date.now();
      const joined = sanitizeForCapture(texts.join("\n\n"));
      if (!joined) {
        this.debugLog.log("ce-afterTurn", { skipped: true, reason: "sanitized_empty" });
        return;
      }
      const episode = compactionOccurred
        ? joined.slice(-12000)   // sweep: keep tail (newest messages)
        : joined.slice(0, 12000);

      await this.client.ingest([{
        content: episode,
        role_type: "user",
        role: "conversation",
        name: buildEpisodeName("turn", { sessionKey: params.sessionId }),
        timestamp: new Date().toISOString(),
        source_description: buildProvenance(this.groupId, {
          event,
          session_key: params.sessionId,
          thread_id: this.threadIdFor(params.sessionId),
        }),
      }]);

      this.logger?.info?.(`graphiti: after-turn ingested ${texts.length} messages`);
      this.debugLog.log("ce-afterTurn", { count: texts.length, sweep: compactionOccurred, ms: Date.now() - start });

      // Auto-compaction truncated the message window — clear the session
      // recall flag so the next assemble() fires recovery, mirroring the
      // explicit compact() method.
      if (compactionOccurred) {
        this.signalRecovery(params.sessionId, "compact");
      }
    } catch (err) {
      this.logger?.warn(`graphiti: afterTurn failed: ${String(err)}`);
      this.debugLog.log("ce-afterTurn", { error: String(err) });
    }
  }

  /**
   * Bootstrap: health-check the server, report graph population,
   * and prime Smart autoRecall state for continuity recovery.
   */
  async bootstrap(params: {
    sessionId: string;
    sessionFile?: string;
    threadId?: string;
  }): Promise<BootstrapResult> {
    try {
      const state = this.session(params.sessionId);
      state.sessionFile = params.sessionFile ?? null;
      if (params.threadId) state.threadId = params.threadId;

      const healthy = await this.cachedHealthy();
      if (!healthy) {
        this.debugLog.log("ce-bootstrap", { healthy: false });
        return { bootstrapped: false, reason: "server-unhealthy" };
      }

      this.signalRecovery(params.sessionId, "bootstrap");

      // Bounded on purpose: episodeCount is a lower bound capped at BOOTSTRAP_EPISODE_COUNT_CAP.
      const stats = await this.client.episodeCount(BOOTSTRAP_EPISODE_COUNT_CAP);
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
        episode = sanitizeForCapture(params.summary!).slice(0, 12000);
      } else {
        const texts = extractTextsFromMessages(params.messages!);
        if (texts.length === 0) {
          this.debugLog.log("ce-subagentEnded", { child: params.childSessionKey, action: "no_content" });
          return;
        }
        episode = sanitizeForCapture(texts.join("\n\n")).slice(0, 12000);
      }

      await this.client.ingest([{
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
          thread_id: this.threadIdFor(params.childSessionKey),
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
   * Dispose: release in-memory session state (no persistent connections).
   */
  dispose(): void {
    // No persistent connections to clean up; drop per-session state
    this._sessions.clear();
    this._recalledSessions.clear();
    this._healthCache = null;
  }
}
