/**
 * Shared helpers used by both index.ts (hooks) and context-engine.ts.
 *
 * Extracted to avoid circular dependencies and reduce duplication.
 */

/**
 * Session context embedded in episode provenance for traceability.
 * @exported - public API of this plugin
 */
export interface SessionMeta {
  sessionKey?: string;
  sessionStart?: string;
  agent?: string;
  channel?: string;
}

/**
 * Build an episode name that includes the session key when available.
 * Produces `<prefix>-<sessionKey>-<ts>` or `<prefix>-<ts>` as fallback.
 * @exported - public API of this plugin
 */
export function buildEpisodeName(prefix: string, meta: SessionMeta): string {
  if (meta.sessionKey) return `${prefix}-${meta.sessionKey}-${Date.now()}`;
  return `${prefix}-${Date.now()}`;
}

/**
 * Build a JSON-encoded provenance object for episode source_description.
 *
 * Always includes: plugin, event, ts, group_id.
 * Optional: session_key, file, source, agent, channel, session_start.
 */
export function buildProvenance(
  groupId: string,
  fields: {
    event: string;
    session_key?: string;
    file?: string;
    source?: string;
    agent?: string;
    channel?: string;
    session_start?: string;
  },
): string {
  const prov: Record<string, string> = {
    plugin: "openclaw-graphiti",
    event: fields.event,
    ts: new Date().toISOString(),
    group_id: groupId,
  };
  if (fields.session_key) prov.session_key = fields.session_key;
  if (fields.file) prov.file = fields.file;
  if (fields.source) prov.source = fields.source;
  if (fields.agent) prov.agent = fields.agent;
  if (fields.channel) prov.channel = fields.channel;
  if (fields.session_start) prov.session_start = fields.session_start;
  return JSON.stringify(prov);
}

/**
 * Extract text content from a message's content field.
 * Handles both string content and content-block-array format.
 * Returns null if no usable text found or text is below minLength.
 */
export function extractTextContent(
  content: unknown,
  minLength = 20,
): string | null {
  if (typeof content === "string") {
    return content.length > minLength ? content : null;
  }
  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const block of content) {
      if (
        block &&
        typeof block === "object" &&
        (block as any).type === "text" &&
        typeof (block as any).text === "string"
      ) {
        const text = (block as any).text;
        if (text.length > minLength) {
          parts.push(text);
        }
      }
    }
    return parts.length > 0 ? parts.join("\n") : null;
  }
  return null;
}

/**
 * Extract user/assistant text from a message array.
 * Returns formatted strings like "user: <text>" or "assistant: <text>".
 */
export function extractTextsFromMessages(
  messages: unknown[],
  opts: { maxPerMessage?: number; minLength?: number } = {},
): string[] {
  const { maxPerMessage = 2000, minLength = 20 } = opts;
  const texts: string[] = [];

  for (const msg of messages) {
    if (!msg || typeof msg !== "object") continue;
    const msgObj = msg as Record<string, unknown>;
    const role = msgObj.role as string;
    if (role !== "user" && role !== "assistant") continue;

    const content = msgObj.content;
    if (typeof content === "string" && content.length > minLength) {
      texts.push(`${role}: ${content.slice(0, maxPerMessage)}`);
    } else if (Array.isArray(content)) {
      for (const block of content) {
        if (
          block &&
          typeof block === "object" &&
          (block as any).type === "text" &&
          typeof (block as any).text === "string"
        ) {
          const text = (block as any).text;
          if (text.length > minLength) {
            texts.push(`${role}: ${text.slice(0, maxPerMessage)}`);
          }
        }
      }
    }
  }

  return texts;
}

/**
 * Format facts as a `<graphiti-context>` block for system prompt injection.
 * Shared between assemble() and prepareSubagentSpawn().
 */
export function formatFactsAsContext(facts: { name: string; fact: string }[]): string {
  const context = facts.map((f) => `- **${f.name}**: ${f.fact}`).join("\n");
  return `<graphiti-context>\nRelevant knowledge graph facts (auto-recalled):\n${context}\n</graphiti-context>`;
}
