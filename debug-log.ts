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

export class DebugLog {
  private disabled = false;
  readonly filePath: string;

  constructor(filePath?: string, enabled = true) {
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
          const val = sv !== null && sv.includes(" ") ? `"${sv}"` : (sv ?? String(v));
          return `${k}=${val}`;
        });
      fs.appendFileSync(this.filePath, `${new Date().toISOString()} [graphiti] ${event.padEnd(12)} ${parts.join(" ")}\n`);
    } catch { /* never crash the plugin */ }
  }

  clear(): void {
    if (this.disabled) return;
    try { fs.writeFileSync(this.filePath, ""); }
    catch { /* ignore */ }
  }

  tail(n: number): string {
    try {
      const content = fs.readFileSync(this.filePath, "utf-8");
      const lines = content.trimEnd().split("\n");
      return lines.slice(-n).join("\n");
    } catch { return ""; }
  }
}

export const NOOP_LOG = new DebugLog(undefined, false);
