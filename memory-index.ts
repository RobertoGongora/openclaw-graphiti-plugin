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

export interface MemoryWrite {
  /** Workspace-relative POSIX path, e.g. "memory/2026-03-05.md". */
  filePath: string;
  /** Absolute path of the file that was actually written. */
  absolutePath: string;
}

/**
 * Resolve a write-like tool call to a file inside this workspace's memory
 * directory. The written path is kept absolute and must sit inside
 * `memoryDir` (checked with path.relative), so a write to another checkout's
 * `memory/` folder is ignored instead of indexing the local file of the same
 * name. Relative tool paths are resolved against the parent of `memoryDir`.
 */
export function resolveMemoryWrite(
  toolName: string,
  params: Record<string, unknown> | undefined,
  memoryDir: string,
): MemoryWrite | null {
  if (!WRITE_TOOLS.has(toolName)) return null;
  if (!params || typeof params !== "object") return null;

  const root = path.resolve(memoryDir);
  for (const key of PATH_KEYS) {
    const val = params[key];
    if (typeof val !== "string" || val.length === 0) continue;

    const absolutePath = path.resolve(path.dirname(root), val);
    const rel = path.relative(root, absolutePath);
    if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) continue;

    const filePath = path.posix.join(path.basename(root), rel.split(path.sep).join("/"));
    return { filePath, absolutePath };
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
    // A 2048-byte cut can land inside a multi-byte character; streaming decode
    // drops the incomplete trailing sequence instead of emitting U+FFFD.
    const raw = new TextDecoder("utf-8").decode(buf, { stream: stat.size > bufSize });
    let excerpt = raw.length > 500 ? raw.slice(0, 500) : raw;
    // Same hazard in UTF-16: never end the excerpt on a lone high surrogate.
    const last = excerpt.charCodeAt(excerpt.length - 1);
    if (last >= 0xd800 && last <= 0xdbff) excerpt = excerpt.slice(0, -1);

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
    const parsed: unknown = JSON.parse(raw);
    // Valid JSON that is not a plain object (null, array, string) is as
    // unusable as corrupt JSON — start from an empty state.
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return parsed as IndexState;
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
// Ingest index episode
// ============================================================================

export interface IndexEpisodeOptions {
  client: GraphitiClient;
  filePath: string; // relative, e.g. "memory/2026-03-05.md"
  absolutePath: string;
  groupId: string;
  debugLog: DebugLog;
  stateDir: string;
}

/**
 * Serialises index-state read-modify-write cycles. Concurrent after_tool_call
 * hooks would otherwise read the same state file, each add their own entry,
 * and the last writer would drop the others' entries.
 */
let indexQueue: Promise<unknown> = Promise.resolve();

/**
 * Ingest an index episode for a memory file, skipping files whose mtime has
 * not changed since they were last indexed.
 *
 * This appends — it does NOT replace the previous index episode for the same
 * file. Graphiti's `POST /messages` queues the episode and answers 202 with
 * `{ message, success }`: no episode uuid comes back, so there is nothing to
 * delete on the next write. Each modification therefore adds one more
 * `memory-index::<file>` episode; the newest one carries the latest
 * `last_modified`.
 *
 * Calls are serialised behind a module-level promise chain.
 */
export function ingestIndexEpisode(opts: IndexEpisodeOptions): Promise<boolean> {
  const run = indexQueue.then(() => ingestIndexEpisodeNow(opts));
  indexQueue = run.catch(() => {});
  return run;
}

async function ingestIndexEpisodeNow(opts: IndexEpisodeOptions): Promise<boolean> {
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

/** Directories nested deeper than this below the memory dir are not scanned. */
export const MAX_SCAN_DEPTH = 16;

export function scanMemoryFiles(memoryDir: string, prefix = "memory"): string[] {
  const results: string[] = [];
  // Real paths already walked — guards against directory cycles (bind mounts,
  // junctions) on top of the depth limit.
  const visited = new Set<string>();

  function walk(dir: string, rel: string, depth: number) {
    if (depth > MAX_SCAN_DEPTH) return;
    let entries: fs.Dirent[];
    try {
      const real = fs.realpathSync(dir);
      if (visited.has(real)) return;
      visited.add(real);
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      const relPath = path.join(rel, entry.name);
      if (entry.isDirectory()) {
        walk(fullPath, relPath, depth + 1);
      } else if (entry.isFile()) {
        results.push(relPath.replaceAll("\\", "/"));
      }
    }
  }

  walk(memoryDir, prefix, 0);
  return results;
}
