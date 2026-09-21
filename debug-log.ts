/**
 * Structured debug log for diagnostics.
 *
 * Records HTTP status codes, timing, and result counts — never
 * conversation content, search queries, or PII. Users can paste
 * log output in bug reports for instant diagnosis.
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";

/** Rotate the log once it grows past this size; one `.1` generation is kept. */
export const MAX_LOG_BYTES = 5 * 1024 * 1024;

/** `tail()` reads at most this many bytes from the end of the file. */
export const TAIL_READ_BYTES = 256 * 1024;

export class DebugLog {
  private disabled = false;
  readonly filePath: string;
  private readonly maxBytes: number;

  constructor(filePath?: string, enabled = true, maxBytes = MAX_LOG_BYTES) {
    this.maxBytes = maxBytes;
    this.filePath = filePath ?? path.join(os.homedir(), ".openclaw", "logs", "graphiti-plugin.log");
    if (!enabled) { this.disabled = true; return; }
    try { fs.mkdirSync(path.dirname(this.filePath), { recursive: true }); }
    catch { this.disabled = true; }
  }

  log(event: string, fields: Record<string, string | number | boolean | null | undefined>): void {
    if (this.disabled) return;
    try {
      const parts = Object.entries(fields)
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => {
          const sv = typeof v === "string" ? v.replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/"/g, '\\"') : null;
          // Quote anything a key=value parser could misread: whitespace,
          // quotes, `=`, a backslash, or an empty value.
          const val = sv !== null && (sv === "" || /[\s"=\\]/.test(sv)) ? `"${sv}"` : (sv ?? String(v));
          return `${k}=${val}`;
        });
      this.rotateIfNeeded();
      fs.appendFileSync(this.filePath, `${new Date().toISOString()} [graphiti] ${event.padEnd(12)} ${parts.join(" ")}\n`);
    } catch { /* never crash the plugin */ }
  }

  /** Size-capped rotation: `<file>` becomes `<file>.1`, replacing any older one. */
  private rotateIfNeeded(): void {
    try {
      if (fs.statSync(this.filePath).size < this.maxBytes) return;
      fs.renameSync(this.filePath, `${this.filePath}.1`);
    } catch { /* missing file or rename failure — keep appending */ }
  }

  clear(): void {
    if (this.disabled) return;
    try { fs.writeFileSync(this.filePath, ""); }
    catch { /* ignore */ }
  }

  /**
   * Return the last `n` lines of the log file (default 100).
   *
   * Reads only the final TAIL_READ_BYTES of the file, so the cost is bounded
   * regardless of log size. A partial first line in that window is dropped.
   */
  tail(n = 100): string {
    if (this.disabled) return "(disabled)";
    try {
      const fd = fs.openSync(this.filePath, "r");
      let content: string;
      let truncated: boolean;
      try {
        const size = fs.fstatSync(fd).size;
        const readSize = Math.min(size, TAIL_READ_BYTES);
        const buf = Buffer.alloc(readSize);
        fs.readSync(fd, buf, 0, readSize, size - readSize);
        content = buf.toString("utf-8");
        truncated = size > readSize;
      } finally {
        fs.closeSync(fd);
      }
      const lines = content.trimEnd().split("\n");
      if (truncated) lines.shift();
      return lines.slice(-n).join("\n");
    } catch { return ""; }
  }
}

export const NOOP_LOG = new DebugLog(undefined, false);
