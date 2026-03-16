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
    file_type?: string;
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
  if (fields.file_type) prov.file_type = fields.file_type;
  if (fields.source) prov.source = fields.source;
  if (fields.agent) prov.agent = fields.agent;
  if (fields.channel) prov.channel = fields.channel;
  if (fields.session_start) prov.session_start = fields.session_start;
  return JSON.stringify(prov);
}

/**
 * Strip plugin-injected metadata and noise from text before graph ingestion.
 * Prevents feedback loops where recalled context is re-ingested as new knowledge.
 */
export function sanitizeForCapture(text: string): string {
  let t = text;

  // Strip <graphiti-context>...</graphiti-context> blocks (multiline)
  t = t.replace(/<graphiti-context>[\s\S]*?<\/graphiti-context>/g, "");

  // Strip <relevant-memories>...</relevant-memories> blocks (multiline)
  t = t.replace(/<relevant-memories>[\s\S]*?<\/relevant-memories>/g, "");

  // Strip conversation metadata JSON blocks: ```json\n{"schema":"openclaw.inbound_meta...}```
  t = t.replace(/```json\s*\n\s*\{["\s]*schema["\s]*:\s*"openclaw\.inbound_meta[\s\S]*?```/g, "");

  // Strip sender metadata JSON blocks containing both "label" and "sender" keys (order-independent)
  t = t.replace(/```json\s*\n\s*\{(?=[\s\S]*?"label")(?=[\s\S]*?"sender")[\s\S]*?```/g, "");

  // Strip [Subagent Context] prefixed lines
  t = t.replace(/^\[Subagent Context\].*$/gm, "");

  // Strip leading timestamps like [Mon 2026-03-15 14:00 UTC]
  t = t.replace(/^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+[A-Za-z]{2,5}\][ \t]*/gm, "");

  // Strip null bytes
  t = t.replace(/\u0000/g, "");

  // Collapse multiple blank lines to max 2
  t = t.replace(/\n{3,}/g, "\n\n");

  return t.trim();
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
