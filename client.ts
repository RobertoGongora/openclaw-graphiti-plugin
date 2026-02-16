/**
 * Graphiti HTTP client — talks to the Graphiti FastAPI server.
 */

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
    private logger?: { info?: (...args: any[]) => void; warn: (...args: any[]) => void }
  ) {}

  private async fetch(path: string, body: unknown): Promise<any> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);

    try {
      const res = await fetch(`${this.url}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
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
    const data = await this.fetch("/search", {
      query,
      group_ids: [this.groupId],
      max_facts: maxFacts,
    });
    return data.facts ?? [];
  }

  /**
   * Get contextual memory based on messages.
   */
  async getMemory(messages: GraphitiMessage[], maxFacts = 10): Promise<GraphitiFact[]> {
    const data = await this.fetch("/get-memory", {
      group_id: this.groupId,
      center_node_uuid: null,
      messages,
      max_facts: maxFacts,
    });
    return data.facts ?? [];
  }

  /**
   * Ingest messages as episodes.
   *
   * Note: The server returns HTTP 202 (Accepted), not 200. This works
   * because `res.ok` covers all 2xx status codes.
   */
  async ingest(messages: GraphitiMessage[]): Promise<{ success: boolean; message: string }> {
    return this.fetch("/messages", {
      group_id: this.groupId,
      messages,
    });
  }

  /**
   * Health check.
   */
  async healthy(): Promise<boolean> {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5_000);
      try {
        const res = await fetch(`${this.url}/healthcheck`, { signal: controller.signal });
        return res.ok;
      } finally {
        clearTimeout(timeout);
      }
    } catch {
      return false;
    }
  }

  /**
   * Get recent episodes.
   *
   * Note: The server returns a bare JSON array, not a wrapped object.
   */
  async episodes(lastN = 10): Promise<any[]> {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10_000);
      try {
        const res = await fetch(`${this.url}/episodes/${this.groupId}?last_n=${lastN}`, {
          signal: controller.signal,
        });
        if (!res.ok) return [];
        return res.json();
      } finally {
        clearTimeout(timeout);
      }
    } catch {
      return [];
    }
  }
}
