/**
 * Graphiti HTTP client — talks to the Graphiti FastAPI server.
 */

import { type DebugLog, NOOP_LOG } from "./debug-log.js";

export interface GraphitiFact {
  uuid: string;
  name: string;
  fact: string;
  valid_at: string | null;
  invalid_at: string | null;
  created_at: string;
  expired_at: string | null;
}

export interface GraphitiMessage {
  content: string;
  role_type: "user" | "assistant" | "system";
  /** Required — the server has no default and will return 422 if omitted. */
  role: string;
  /** Optional — defaults to empty string (`''`) server-side. */
  name?: string;
  /** ISO-8601 timestamp. Optional — defaults to `utc_now()` server-side. */
  timestamp?: string;
  source_description?: string;
}

export class GraphitiClient {
  constructor(
    private url: string,
    private groupId: string,
    private logger?: { info?: (...args: any[]) => void; warn: (...args: any[]) => void },
    private apiKey?: string,
    private debugLog: DebugLog = NOOP_LOG,
  ) {}

  /** Build headers, optionally including the Authorization bearer token. */
  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const h: Record<string, string> = { ...extra };
    if (this.apiKey) h["Authorization"] = `Bearer ${this.apiKey}`;
    return h;
  }

  private async fetch(path: string, body: unknown): Promise<any> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);
    const start = Date.now();

    try {
      const res = await fetch(`${this.url}${path}`, {
        method: "POST",
        headers: this.headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
        this.debugLog.log(path.slice(1), { status: res.status, group: this.groupId, error: "HTTP error", ms: Date.now() - start });
        throw new Error(`Graphiti ${path} returned ${res.status}: ${text}`);
      }

      return res.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  /**
   * Search for facts by query.
   */
  async search(query: string, maxFacts = 10): Promise<GraphitiFact[]> {
    const start = Date.now();
    const data = await this.fetch("/search", {
      query,
      group_ids: [this.groupId],
      max_facts: maxFacts,
    });
    const facts = data.facts ?? [];
    this.debugLog.log("search", { status: 200, group: this.groupId, count: facts.length, ms: Date.now() - start });
    return facts;
  }

  /**
   * Get contextual memory based on messages.
   */
  async getMemory(messages: GraphitiMessage[], maxFacts = 10): Promise<GraphitiFact[]> {
    const start = Date.now();
    const data = await this.fetch("/get-memory", {
      group_id: this.groupId,
      center_node_uuid: null,
      messages,
      max_facts: maxFacts,
    });
    const facts = data.facts ?? [];
    this.debugLog.log("get-memory", { status: 200, group: this.groupId, count: facts.length, ms: Date.now() - start });
    return facts;
  }

  /**
   * Ingest messages as episodes.
   *
   * Note: The server returns HTTP 202 (Accepted), not 200. This works
   * because `res.ok` covers all 2xx status codes.
   */
  async ingest(messages: GraphitiMessage[]): Promise<{ success: boolean; message: string }> {
    const start = Date.now();
    const result = await this.fetch("/messages", {
      group_id: this.groupId,
      messages,
    });
    this.debugLog.log("ingest", { status: 202, group: this.groupId, messages: messages.length, ms: Date.now() - start });
    return result;
  }

  /**
   * Health check.
   */
  async healthy(): Promise<boolean> {
    const start = Date.now();
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5_000);
      try {
        const res = await fetch(`${this.url}/healthcheck`, {
          headers: this.headers(),
          signal: controller.signal,
        });
        this.debugLog.log("healthcheck", { status: res.status, ms: Date.now() - start });
        return res.ok;
      } finally {
        clearTimeout(timeout);
      }
    } catch {
      this.debugLog.log("healthcheck", { error: "unreachable", ms: Date.now() - start });
      return false;
    }
  }

  /**
   * Get episode count and most recent episode timestamp.
   * Returns { count: 0, latestAt: null } on error or empty graph.
   */
  async episodeCount(): Promise<{ count: number; latestAt: string | null }> {
    const eps = await this.episodes(10000);
    return {
      count: eps.length,
      latestAt: eps.length > 0 ? (eps[0].created_at ?? null) : null,
    };
  }

  /**
   * Get recent episodes.
   *
   * Note: The server returns a bare JSON array, not a wrapped object.
   */
  async episodes(lastN = 10): Promise<any[]> {
    const start = Date.now();
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10_000);
      try {
        const res = await fetch(`${this.url}/episodes/${this.groupId}?last_n=${lastN}`, {
          headers: this.headers(),
          signal: controller.signal,
        });
        if (!res.ok) {
          this.debugLog.log("episodes", { status: res.status, group: this.groupId, error: "HTTP error", ms: Date.now() - start });
          return [];
        }
        const data = await res.json();
        this.debugLog.log("episodes", { status: 200, group: this.groupId, count: data.length, ms: Date.now() - start });
        return data;
      } finally {
        clearTimeout(timeout);
      }
    } catch {
      this.debugLog.log("episodes", { group: this.groupId, error: "unreachable", ms: Date.now() - start });
      return [];
    }
  }
}
