/**
 * Shared helpers used by both index.ts (hooks) and context-engine.ts.
 *
 * Extracted to avoid circular dependencies and reduce duplication.
 */

import fs from "node:fs/promises";

/**
 * Session context embedded in episode provenance for traceability.
 * @exported - public API of this plugin
 */
export interface SessionMeta {
  sessionKey?: string;
  sessionStart?: string;
  agent?: string;
  channel?: string;
  threadId?: string;
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
    thread_id?: string;
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
  if (fields.thread_id) prov.thread_id = fields.thread_id;
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

  // Strip <graphiti-continuity>...</graphiti-continuity> blocks (multiline)
  t = t.replace(/<graphiti-continuity>[\s\S]*?<\/graphiti-continuity>/g, "");

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

// ---------------------------------------------------------------------------
// Smart autoRecall helpers
// ---------------------------------------------------------------------------

/**
 * Detect whether the current turn is likely a continuity gap —
 * the runtime has lost recent conversational context.
 */
export function isContinuityGap(
  messageCount: number,
  opts?: { recentEvent?: string | null; threshold?: number },
): boolean {
  const threshold = opts?.threshold ?? 3;
  const event = opts?.recentEvent;
  if (event === "bootstrap" || event === "compact") return true;
  return messageCount <= threshold;
}

/**
 * Deictic patterns that signal the user expects continuity with prior context.
 * Kept intentionally conservative to avoid false positives on normal prompts.
 */
const DEICTIC_PATTERNS = [
  /\bas (?:I|we) (?:mentioned|said|discussed|talked about)\b/i,
  /\bwhat we (?:just|were) (?:discussing|talking about)\b/i,
  /\b(?:continue|go on|keep going|pick up where)\b/i,
  /\bthat (?:thing|idea|approach|plan|issue|bug|feature|task)\b/i,
  /\blike (?:I|we) said\b/i,
  /\bremember when\b/i,
  /\bback to (?:the|that|what)\b/i,
  /\bwhere (?:we|I) left off\b/i,
];

/**
 * Returns true if text contains references to prior conversational context
 * that the model may not have in its current message window.
 */
export function hasDeicticReferences(text: string): boolean {
  return DEICTIC_PATTERNS.some((p) => p.test(text));
}

/**
 * Read the tail of a JSONL session file, returning the most recent
 * user/assistant messages up to `maxChars` of formatted text.
 *
 * JSONL format (from OpenClaw):
 *   { "type": "message", "message": { "role": "user"|"assistant", "content": ... } }
 *
 * Returns null if the file is missing, empty, or has no usable messages.
 */
export async function readSessionFileTail(
  sessionFile: string,
  maxChars = 8000,
): Promise<string | null> {
  // Read only a bounded tail chunk to avoid loading arbitrarily large session files.
  // 128KB is generous headroom for the default 8000-char output budget.
  const TAIL_BYTES = 128 * 1024;

  let raw: string;
  try {
    const fh = await fs.open(sessionFile, "r");
    try {
      const stat = await fh.stat();
      const readSize = Math.min(stat.size, TAIL_BYTES);
      const buf = Buffer.alloc(readSize);
      await fh.read(buf, 0, readSize, stat.size - readSize);
      raw = buf.toString("utf-8");
    } finally {
      await fh.close();
    }
  } catch {
    return null;
  }

  const lines = raw.split("\n");

  const collected: string[] = [];
  let totalChars = 0;

  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;

    let record: any;
    try {
      record = JSON.parse(line);
    } catch {
      continue;
    }

    if (record?.type !== "message") continue;
    const msg = record.message;
    if (!msg || (msg.role !== "user" && msg.role !== "assistant")) continue;

    const text = typeof msg.content === "string"
      ? msg.content
      : extractTextContent(msg.content, 1);
    if (!text) continue;

    const label = msg.role === "user" ? "User" : "Assistant";
    const formatted = `${label}: ${text}`;

    if (totalChars + formatted.length > maxChars && collected.length > 0) break;
    collected.push(formatted);
    totalChars += formatted.length;
  }

  if (collected.length === 0) return null;

  collected.reverse();
  return collected.join("\n");
}

/**
 * Wrap recovered session transcript in a `<graphiti-continuity>` block.
 */
export function formatContinuityBlock(text: string): string {
  return `<graphiti-continuity>\nRecent session context (recovered from transcript):\n${text}\n</graphiti-continuity>`;
}
