/**
 * Memory file indexing — creates lightweight "index episodes" in Graphiti
 * whenever a file is written to the `memory/` directory.
 *
 * Also provides a backfill scanner for existing memory files.
 */

import fs from "node:fs";
import path from "node:path";
import type { GraphitiClient } from "./client.js";
import type { DebugLog } from "./debug-log.js";

// ============================================================================
// Path extraction
// ============================================================================

const WRITE_TOOLS = new Set(["Write", "Edit", "write", "edit", "write_file", "create_file"]);

const PATH_KEYS = ["file_path", "path", "filePath", "target_file"];

// ============================================================================
// Extension filtering
// ============================================================================

export const DEFAULT_INDEX_EXTENSIONS = [".md", ".txt"] as const;

/**
 * Return true if the file extension is in the allowed set.
 * Used to skip non-prose files (.json, .png, etc.) that create noise entities.
 * Files without an extension (e.g. `Makefile`) are also rejected since
 * `path.extname()` returns `""` which won't match any allowed entry.
 */
export function isIndexableFile(
  filePath: string,
  allowedExtensions: readonly string[] = DEFAULT_INDEX_EXTENSIONS,
): boolean {
  const ext = path.extname(filePath).toLowerCase();
  return allowedExtensions.some(
    (e) => (e.startsWith(".") ? e : `.${e}`).toLowerCase() === ext,
  );
}

/**
 * If `toolName` is a write-like tool and the params reference a memory/ path,
 * return the relative memory path. Otherwise return null.
 */
export function extractMemoryPath(
  toolName: string,
  params: Record<string, unknown> | undefined,
): string | null {
  if (!WRITE_TOOLS.has(toolName)) return null;
  if (!params || typeof params !== "object") return null;

  for (const key of PATH_KEYS) {
    const val = params[key];
    if (typeof val !== "string") continue;

    // Normalise: find the memory/ segment
    const idx = val.indexOf("/memory/");
    if (idx !== -1) return val.slice(idx + 1); // "memory/..."

    if (val.startsWith("memory/")) return val;
  }

  return null;
}

// ============================================================================
// Naming / content
// ============================================================================

export function indexEpisodeName(filePath: string): string {
  return `memory-index::${filePath}`;
}

export function buildIndexContent(
  filePath: string,
  lastModified: string,
  excerpt: string,
  fileSize: number,
): string {
  return [
    "---",
    "type: memory-index",
    `file: ${filePath}`,
    `last_modified: ${lastModified}`,
    `size: ${fileSize}`,
    "---",
    excerpt,
  ].join("\n");
}

// ============================================================================
// File metadata
// ============================================================================

export interface MemoryFileMeta {
  lastModified: string; // ISO-8601
  excerpt: string;
  fileSize: number;
}

/** Skip files larger than 1 MB — likely binary or log dumps. */
const MAX_FILE_SIZE = 1_048_576; // 1 MB

export function readMemoryFileMeta(absolutePath: string): MemoryFileMeta | null {
  try {
    const stat = fs.statSync(absolutePath);
    if (stat.size > MAX_FILE_SIZE) return null;

    const fd = fs.openSync(absolutePath, "r");
    const bufSize = Math.min(stat.size, 2048);
    const buf = Buffer.alloc(bufSize);
    try {
      fs.readSync(fd, buf, 0, bufSize, 0);
    } finally {
      fs.closeSync(fd);
    }
    const raw = buf.toString("utf-8");
    const excerpt = raw.length > 500 ? raw.slice(0, 500) : raw;

    return {
      lastModified: stat.mtime.toISOString(),
      excerpt,
      fileSize: stat.size,
    };
  } catch {
    return null;
  }
}

// ============================================================================
// State persistence (idempotency)
// ============================================================================

export interface IndexStateEntry {
  lastModified: string;
  lastIndexed: string;
}

export type IndexState = Record<string, IndexStateEntry>;

const STATE_FILENAME = "graphiti-memory-index.json";

function stateFilePath(stateDir: string): string {
  return path.join(stateDir, STATE_FILENAME);
}

export function readIndexState(stateDir: string): IndexState {
  try {
    const raw = fs.readFileSync(stateFilePath(stateDir), "utf-8");
    return JSON.parse(raw) as IndexState;
  } catch {
    return {};
  }
}

export function writeIndexState(stateDir: string, state: IndexState): void {
  fs.mkdirSync(stateDir, { recursive: true });
  const tmpPath = stateFilePath(stateDir) + ".tmp";
  fs.writeFileSync(tmpPath, JSON.stringify(state, null, 2));
  fs.renameSync(tmpPath, stateFilePath(stateDir));
}

// ============================================================================
// Upsert index episode
// ============================================================================

export interface UpsertOptions {
  client: GraphitiClient;
  filePath: string; // relative, e.g. "memory/2026-03-05.md"
  absolutePath: string;
  groupId: string;
  debugLog: DebugLog;
  stateDir: string;
}

export async function upsertIndexEpisode(opts: UpsertOptions): Promise<boolean> {
  const { client, filePath, absolutePath, groupId, debugLog, stateDir } = opts;

  const meta = readMemoryFileMeta(absolutePath);
  if (!meta) {
    debugLog.log("mem-index", { skipped: true, reason: "file_not_found", file: filePath });
    return false;
  }

  // Check state — skip if mtime unchanged
  const state = readIndexState(stateDir);
  const existing = state[filePath];
  if (existing && existing.lastModified === meta.lastModified) {
    debugLog.log("mem-index", { skipped: true, reason: "unchanged", file: filePath });
    return false;
  }

  const content = buildIndexContent(filePath, meta.lastModified, meta.excerpt, meta.fileSize);

  await client.ingest([{
    content,
    role_type: "system",
    role: "memory-index",
    name: indexEpisodeName(filePath),
    timestamp: meta.lastModified,
    source_description: JSON.stringify({
      plugin: "openclaw-graphiti",
      event: "memory_index",
      ts: new Date().toISOString(),
      group_id: groupId,
      file: filePath,
      file_type: path.extname(filePath).toLowerCase() || "unknown", // safety net — isIndexableFile rejects extensionless files
    }),
  }]);

  // Update state
  state[filePath] = {
    lastModified: meta.lastModified,
    lastIndexed: new Date().toISOString(),
  };
  writeIndexState(stateDir, state);

  debugLog.log("mem-index", { status: 202, group: groupId, file: filePath });
  return true;
}

// ============================================================================
// Scan memory directory
// ============================================================================

export function scanMemoryFiles(memoryDir: string, prefix = "memory"): string[] {
  const results: string[] = [];

  function walk(dir: string, rel: string) {
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      const relPath = path.join(rel, entry.name);
      if (entry.isDirectory()) {
        walk(fullPath, relPath);
      } else if (entry.isFile()) {
        results.push(relPath.replaceAll("\\", "/"));
      }
    }
  }

  walk(memoryDir, prefix);
  return results;
}
