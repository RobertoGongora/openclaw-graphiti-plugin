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

export interface GraphitiEpisode {
  uuid: string;
  name?: string;
  group_id?: string;
  labels?: string[];
  created_at?: string;
  source?: string;
  source_description?: string;
  content?: string;
  valid_at?: string;
  entity_edges?: string[];
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

/**
 * Upper bound on how many episodes `episodeCount()` fetches in order to count
 * them. The Graphiti REST API has no count endpoint, so counting means
 * downloading a page of full episodes; the cap keeps that page small. A count
 * equal to the cap means "at least this many" and is displayed as `<cap>+`.
 */
export const EPISODE_COUNT_CAP = 500;

/**
 * Smaller cap for `bootstrap()`, which runs on every session start and only
 * needs to know whether the graph is populated, not how large it is.
 */
export const BOOTSTRAP_EPISODE_COUNT_CAP = 50;

/** Render an episode count, marking it as a lower bound when it hit the cap. */
export function formatEpisodeCount(count: number, cap = EPISODE_COUNT_CAP): string {
  return count >= cap ? `${cap}+` : String(count);
}

/** Pull the `facts` array out of a response body, tolerating null/odd bodies. */
function factsFrom(data: unknown): GraphitiFact[] {
  const facts = (data as { facts?: unknown } | null | undefined)?.facts;
  return Array.isArray(facts) ? (facts as GraphitiFact[]) : [];
}

export class GraphitiClient {
  constructor(
    private url: string,
    private groupId: string,
    private logger?: { info?: (...args: any[]) => void; warn: (...args: any[]) => void },
    private apiKey?: string,
    private debugLog: DebugLog = NOOP_LOG,
    /** Abort timeouts in ms. Defaults: 15 s requests, 5 s health, 10 s episodes. */
    private timeouts: { requestMs?: number; healthMs?: number; episodesMs?: number } = {},
  ) {}

  /** Build headers, optionally including the Authorization bearer token. */
  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const h: Record<string, string> = { ...extra };
    if (this.apiKey) h["Authorization"] = `Bearer ${this.apiKey}`;
    return h;
  }

  private async fetch(path: string, body: unknown): Promise<any> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeouts.requestMs ?? 15_000);
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

      // Await the body inside the try so the abort timer still covers a
      // stalled response body, not just the headers.
      return await res.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  private async fetchDelete(path: string): Promise<void> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeouts.requestMs ?? 15_000);
    const start = Date.now();

    try {
      const res = await fetch(`${this.url}${path}`, {
        method: "DELETE",
        headers: this.headers(),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
        this.debugLog.log(path.slice(1), { status: res.status, group: this.groupId, error: "HTTP error", ms: Date.now() - start });
        throw new Error(`Graphiti DELETE ${path} returned ${res.status}: ${text}`);
      }

      // Consume the response body to allow connection reuse (HTTP keep-alive)
      await res.text().catch(() => {});
    } finally {
      clearTimeout(timeout);
    }
  }

  /**
   * Search for facts by query.
   */
  async search(query: string, maxFacts = 10, groupIds?: string[]): Promise<GraphitiFact[]> {
    const start = Date.now();
    const data = await this.fetch("/search", {
      query,
      group_ids: groupIds ?? [this.groupId],
      max_facts: maxFacts,
    });
    const facts = factsFrom(data);
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
    const facts = factsFrom(data);
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
      const timeout = setTimeout(() => controller.abort(), this.timeouts.healthMs ?? 5_000);
      try {
        const res = await fetch(`${this.url}/healthcheck`, {
          headers: this.headers(),
          signal: controller.signal,
        });
        // Consume the response body to allow connection reuse (HTTP keep-alive)
        await res.text().catch(() => {});
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
   *
   * The count is bounded: at most `cap` episodes (default EPISODE_COUNT_CAP)
   * are fetched, so `count === cap` means "cap or more" — display it with
   * `formatEpisodeCount()`. `latestAt` is the newest `created_at` in that page
   * (the server returns the most recent `last_n` episodes, in either order).
   */
  async episodeCount(cap = EPISODE_COUNT_CAP): Promise<{ count: number; latestAt: string | null }> {
    const eps = await this.episodes(cap);
    let latestAt: string | null = null;
    let latestMs = -Infinity;
    for (const ep of eps) {
      if (!ep.created_at) continue;
      const ms = Date.parse(ep.created_at);
      if (!Number.isNaN(ms) && ms > latestMs) { latestMs = ms; latestAt = ep.created_at; }
    }
    return { count: Math.min(eps.length, cap), latestAt };
  }

  /**
   * Get recent episodes.
   *
   * Note: The server returns a bare JSON array, not a wrapped object.
   * Contract: this method never throws — a non-2xx status, an unreachable
   * server, a timeout, or a non-array body all yield `[]` (logged to the
   * debug log) so status/recall paths degrade instead of failing.
   */
  async episodes(lastN = 10): Promise<GraphitiEpisode[]> {
    const start = Date.now();
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.timeouts.episodesMs ?? 10_000);
      try {
        const res = await fetch(`${this.url}/episodes/${this.groupId}?last_n=${lastN}`, {
          headers: this.headers(),
          signal: controller.signal,
        });
        if (!res.ok) {
          // Consume the response body to allow connection reuse (HTTP keep-alive)
          await res.text().catch(() => {});
          this.debugLog.log("episodes", { status: res.status, group: this.groupId, error: "HTTP error", ms: Date.now() - start });
          return [];
        }
        const data: unknown = await res.json();
        if (!Array.isArray(data)) {
          this.debugLog.log("episodes", { status: 200, group: this.groupId, error: "non-array body", ms: Date.now() - start });
          return [];
        }
        this.debugLog.log("episodes", { status: 200, group: this.groupId, count: data.length, ms: Date.now() - start });
        return data as GraphitiEpisode[];
      } finally {
        clearTimeout(timeout);
      }
    } catch {
      this.debugLog.log("episodes", { group: this.groupId, error: "unreachable", ms: Date.now() - start });
      return [];
    }
  }

  /**
   * Delete a fact/edge by UUID.
   */
  async deleteEdge(uuid: string): Promise<void> {
    const start = Date.now();
    await this.fetchDelete(`/entity-edge/${uuid}`);
    this.debugLog.log("deleteEdge", { status: 200, group: this.groupId, uuid, ms: Date.now() - start });
  }

  /**
   * Delete an episode by UUID.
   */
  async deleteEpisode(uuid: string): Promise<void> {
    const start = Date.now();
    await this.fetchDelete(`/episode/${uuid}`);
    this.debugLog.log("deleteEpisode", { status: 200, group: this.groupId, uuid, ms: Date.now() - start });
  }
}
