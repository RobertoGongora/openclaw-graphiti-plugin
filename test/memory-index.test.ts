/**
 * Unit tests for memory-index module.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import {
  resolveMemoryWrite,
  indexEpisodeName,
  buildIndexContent,
  readMemoryFileMeta,
  readIndexState,
  writeIndexState,
  ingestIndexEpisode,
  scanMemoryFiles,
  MAX_SCAN_DEPTH,
  isIndexableFile,
} from "../memory-index.js";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  getMockPort,
  lastRequest,
  mockOverrides,
} from "./helpers.js";
import { GraphitiClient } from "../client.js";
import { NOOP_LOG } from "../debug-log.js";

// ============================================================================
// resolveMemoryWrite
// ============================================================================

describe("resolveMemoryWrite", () => {
  const workspace = path.join(os.tmpdir(), "ws-here");
  const memoryDir = path.join(workspace, "memory");

  test("resolves an absolute path inside the workspace memory dir", () => {
    const abs = path.join(memoryDir, "2026-03-05.md");
    expect(resolveMemoryWrite("Write", { file_path: abs }, memoryDir)).toEqual({
      filePath: "memory/2026-03-05.md",
      absolutePath: abs,
    });
  });

  test("resolves a workspace-relative path", () => {
    expect(resolveMemoryWrite("write", { filePath: "memory/MEMORY.md" }, memoryDir)).toEqual({
      filePath: "memory/MEMORY.md",
      absolutePath: path.join(memoryDir, "MEMORY.md"),
    });
  });

  test("keeps nested paths and reads every supported param key", () => {
    const abs = path.join(memoryDir, "sub", "deep.md");
    for (const key of ["file_path", "path", "filePath", "target_file"]) {
      expect(resolveMemoryWrite("edit", { [key]: abs }, memoryDir)?.filePath).toBe("memory/sub/deep.md");
    }
  });

  test("ignores a memory/ dir in ANOTHER checkout", () => {
    const other = path.join(os.tmpdir(), "ws-other", "memory", "2026-03-05.md");
    expect(resolveMemoryWrite("Write", { file_path: other }, memoryDir)).toBeNull();
  });

  test("ignores a nested memory/ dir that is not the workspace memory dir", () => {
    const nested = path.join(workspace, "packages", "x", "memory", "n.md");
    expect(resolveMemoryWrite("Write", { file_path: nested }, memoryDir)).toBeNull();
    expect(resolveMemoryWrite("Write", { file_path: "packages/x/memory/n.md" }, memoryDir)).toBeNull();
  });

  test("rejects traversal out of the memory dir and the dir itself", () => {
    expect(resolveMemoryWrite("Write", { file_path: "memory/../src/index.ts" }, memoryDir)).toBeNull();
    expect(resolveMemoryWrite("Write", { file_path: memoryDir }, memoryDir)).toBeNull();
  });

  test("returns null for non-write tools", () => {
    expect(resolveMemoryWrite("Read", { file_path: "memory/test.md" }, memoryDir)).toBeNull();
  });

  test("returns null for missing params and non-string values", () => {
    expect(resolveMemoryWrite("Write", undefined, memoryDir)).toBeNull();
    expect(resolveMemoryWrite("Write", {}, memoryDir)).toBeNull();
    expect(resolveMemoryWrite("Write", { file_path: 42 }, memoryDir)).toBeNull();
  });

  test("handles write_file and create_file tool names", () => {
    expect(resolveMemoryWrite("write_file", { file_path: "memory/test.md" }, memoryDir)?.filePath).toBe("memory/test.md");
    expect(resolveMemoryWrite("create_file", { path: "memory/y.md" }, memoryDir)?.filePath).toBe("memory/y.md");
  });
});

// ============================================================================
// isIndexableFile
// ============================================================================

describe("isIndexableFile", () => {
  test("allows .md files", () => {
    expect(isIndexableFile("memory/notes.md")).toBe(true);
  });

  test("allows .txt files", () => {
    expect(isIndexableFile("memory/log.txt")).toBe(true);
  });

  test("rejects .json files", () => {
    expect(isIndexableFile("memory/state.json")).toBe(false);
  });

  test("rejects .png files", () => {
    expect(isIndexableFile("memory/screenshot.png")).toBe(false);
  });

  test("case-insensitive extension matching", () => {
    expect(isIndexableFile("memory/NOTES.MD")).toBe(true);
    expect(isIndexableFile("memory/DATA.JSON")).toBe(false);
  });

  test("rejects files with no extension", () => {
    expect(isIndexableFile("memory/Makefile")).toBe(false);
  });

  test("respects custom allowed extensions", () => {
    expect(isIndexableFile("memory/data.json", [".json"])).toBe(true);
    expect(isIndexableFile("memory/notes.md", [".json"])).toBe(false);
  });

  test("normalizes extensions without leading dot", () => {
    expect(isIndexableFile("memory/notes.md", ["md", "txt"])).toBe(true);
    expect(isIndexableFile("memory/log.txt", ["md", "txt"])).toBe(true);
    expect(isIndexableFile("memory/data.json", ["md", "txt"])).toBe(false);
  });

  test("handles nested paths", () => {
    expect(isIndexableFile("memory/sub/deep/file.md")).toBe(true);
    expect(isIndexableFile("memory/sub/deep/data.json")).toBe(false);
  });
});

// ============================================================================
// indexEpisodeName
// ============================================================================

describe("indexEpisodeName", () => {
  test("returns prefixed name", () => {
    expect(indexEpisodeName("memory/2026-03-05.md")).toBe(
      "memory-index::memory/2026-03-05.md",
    );
  });

  test("handles nested paths", () => {
    expect(indexEpisodeName("memory/sub/file.md")).toBe(
      "memory-index::memory/sub/file.md",
    );
  });
});

// ============================================================================
// buildIndexContent
// ============================================================================

describe("buildIndexContent", () => {
  test("produces YAML frontmatter + excerpt", () => {
    const result = buildIndexContent(
      "memory/2026-03-05.md",
      "2026-03-05T14:32:00.000Z",
      "Some excerpt text here",
      2847,
    );

    expect(result).toContain("---");
    expect(result).toContain("type: memory-index");
    expect(result).toContain("file: memory/2026-03-05.md");
    expect(result).toContain("last_modified: 2026-03-05T14:32:00.000Z");
    expect(result).toContain("size: 2847");
    expect(result).toContain("Some excerpt text here");
  });
});

// ============================================================================
// readMemoryFileMeta
// ============================================================================

describe("readMemoryFileMeta", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mem-index-test-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test("reads file metadata", () => {
    const filePath = path.join(tmpDir, "test.md");
    fs.writeFileSync(filePath, "Hello world content");

    const meta = readMemoryFileMeta(filePath);
    expect(meta).not.toBeNull();
    expect(meta!.fileSize).toBe(19);
    expect(meta!.excerpt).toBe("Hello world content");
    expect(meta!.lastModified).toBeTruthy();
  });

  test("truncates excerpt to 500 chars", () => {
    const filePath = path.join(tmpDir, "big.md");
    fs.writeFileSync(filePath, "x".repeat(2000));

    const meta = readMemoryFileMeta(filePath);
    expect(meta).not.toBeNull();
    expect(meta!.excerpt.length).toBe(500);
  });

  test("returns null for non-existent file", () => {
    expect(readMemoryFileMeta(path.join(tmpDir, "nope.md"))).toBeNull();
  });

  test("never ends the excerpt on half a surrogate pair", () => {
    const filePath = path.join(tmpDir, "emoji.md");
    // "a" shifts every emoji so UTF-16 index 499 is a high surrogate.
    fs.writeFileSync(filePath, "a" + "\u{1F600}".repeat(600));

    const excerpt = readMemoryFileMeta(filePath)!.excerpt;
    expect(/[\uD800-\uDBFF]$/.test(excerpt)).toBe(false); // no dangling high surrogate
    expect(excerpt).not.toContain("\uFFFD");
  });

  test("does not emit U+FFFD when the 2048-byte read splits a multi-byte char", () => {
    const filePath = path.join(tmpDir, "euro.md");
    // 3-byte chars: byte 2048 lands inside one.
    fs.writeFileSync(filePath, "\u20AC".repeat(1000));

    const excerpt = readMemoryFileMeta(filePath)!.excerpt;
    expect(excerpt).toBe("\u20AC".repeat(500));
  });
});

// ============================================================================
// State file read/write/idempotency
// ============================================================================

describe("state persistence", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mem-state-test-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test("returns empty state for missing file", () => {
    expect(readIndexState(tmpDir)).toEqual({});
  });

  test("round-trips state", () => {
    const state = {
      "memory/test.md": {
        lastModified: "2026-03-05T14:32:00.000Z",
        lastIndexed: "2026-03-05T14:33:00.000Z",
      },
    };
    writeIndexState(tmpDir, state);
    expect(readIndexState(tmpDir)).toEqual(state);
  });

  test("creates directory if needed", () => {
    const nested = path.join(tmpDir, "a", "b", "c");
    writeIndexState(nested, { "memory/x.md": { lastModified: "x", lastIndexed: "y" } });
    expect(readIndexState(nested)).toHaveProperty("memory/x.md");
  });

  test("corrupt state JSON reads as empty state", () => {
    fs.writeFileSync(path.join(tmpDir, "graphiti-memory-index.json"), "{ not json");
    expect(readIndexState(tmpDir)).toEqual({});
  });

  test("valid JSON that is not an object reads as empty state", () => {
    for (const body of ["null", "[]", "\"str\"", "42"]) {
      fs.writeFileSync(path.join(tmpDir, "graphiti-memory-index.json"), body);
      expect(readIndexState(tmpDir)).toEqual({});
    }
  });

  test("atomic write uses tmp file", () => {
    // Write once, then overwrite — should not corrupt
    writeIndexState(tmpDir, { "a.md": { lastModified: "1", lastIndexed: "2" } });
    writeIndexState(tmpDir, { "b.md": { lastModified: "3", lastIndexed: "4" } });
    const state = readIndexState(tmpDir);
    expect(state).toHaveProperty("b.md");
    expect(state).not.toHaveProperty("a.md");
  });
});

// ============================================================================
// scanMemoryFiles
// ============================================================================

describe("scanMemoryFiles", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mem-scan-test-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test("finds files recursively", () => {
    fs.writeFileSync(path.join(tmpDir, "a.md"), "a");
    fs.mkdirSync(path.join(tmpDir, "sub"));
    fs.writeFileSync(path.join(tmpDir, "sub", "b.md"), "b");

    const files = scanMemoryFiles(tmpDir);
    expect(files).toHaveLength(2);
    expect(files).toContain("memory/a.md");
    expect(files).toContain(path.join("memory", "sub", "b.md"));
  });

  test("returns empty for non-existent dir", () => {
    expect(scanMemoryFiles(path.join(tmpDir, "nope"))).toEqual([]);
  });

  test("stops at MAX_SCAN_DEPTH", () => {
    let dir = tmpDir;
    for (let i = 0; i <= MAX_SCAN_DEPTH; i++) {
      dir = path.join(dir, "d");
      fs.mkdirSync(dir);
      fs.writeFileSync(path.join(dir, `f${i}.md`), "x");
    }
    // f0 sits at depth 1, f<MAX-1> at depth MAX; the last one is one level too deep.
    const files = scanMemoryFiles(tmpDir);
    expect(files).toHaveLength(MAX_SCAN_DEPTH);
    expect(files.some((f) => f.endsWith(`f${MAX_SCAN_DEPTH}.md`))).toBe(false);
  });

  test("does not follow directory symlinks (no cycles)", () => {
    fs.writeFileSync(path.join(tmpDir, "a.md"), "a");
    fs.symlinkSync(tmpDir, path.join(tmpDir, "loop"), "dir");
    expect(scanMemoryFiles(tmpDir)).toEqual(["memory/a.md"]);
  });

  test("uses custom prefix for path construction", () => {
    fs.writeFileSync(path.join(tmpDir, "a.md"), "a");
    fs.mkdirSync(path.join(tmpDir, "sub"));
    fs.writeFileSync(path.join(tmpDir, "sub", "b.md"), "b");

    const files = scanMemoryFiles(tmpDir, "my-notes");
    expect(files).toHaveLength(2);
    expect(files).toContain("my-notes/a.md");
    expect(files).toContain(path.join("my-notes", "sub", "b.md"));
  });
});

// ============================================================================
// ingestIndexEpisode — integration with mock server
// ============================================================================

describe("ingestIndexEpisode", () => {
  let tmpDir: string;
  let stateDir: string;

  beforeAll(startMockServer);
  afterAll(stopMockServer);

  beforeEach(() => {
    resetMockState();
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mem-upsert-test-"));
    stateDir = path.join(tmpDir, "state");
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test("ingests a new memory file", async () => {
    const filePath = path.join(tmpDir, "test.md");
    fs.writeFileSync(filePath, "Some memory content");

    const client = new GraphitiClient(
      `http://127.0.0.1:${getMockPort()}`,
      "test-group",
      undefined,
      undefined,
      NOOP_LOG,
    );

    const result = await ingestIndexEpisode({
      client,
      filePath: "memory/test.md",
      absolutePath: filePath,
      groupId: "test-group",
      debugLog: NOOP_LOG,
      stateDir,
    });

    expect(result).toBe(true);

    const req = lastRequest["/messages"] as any;
    expect(req).toBeDefined();
    expect(req.messages).toHaveLength(1);
    expect(req.messages[0].name).toBe("memory-index::memory/test.md");
    expect(req.messages[0].role).toBe("memory-index");
    expect(req.messages[0].role_type).toBe("system");
    const prov = JSON.parse(req.messages[0].source_description);
    expect(prov.plugin).toBe("openclaw-graphiti");
    expect(prov.event).toBe("memory_index");
    expect(prov.file).toBe("memory/test.md");
    expect(prov.file_type).toBe(".md");
    expect(prov.group_id).toBe("test-group");
    expect(prov.ts).toBeTruthy();
    expect(req.messages[0].content).toContain("type: memory-index");
    expect(req.messages[0].content).toContain("Some memory content");
  });

  test("skips unchanged file (idempotency)", async () => {
    const filePath = path.join(tmpDir, "test.md");
    fs.writeFileSync(filePath, "Some memory content");

    const client = new GraphitiClient(
      `http://127.0.0.1:${getMockPort()}`,
      "test-group",
      undefined,
      undefined,
      NOOP_LOG,
    );

    const opts = {
      client,
      filePath: "memory/test.md",
      absolutePath: filePath,
      groupId: "test-group",
      debugLog: NOOP_LOG,
      stateDir,
    };

    // First call — should index
    await ingestIndexEpisode(opts);
    resetMockState();

    // Second call — should skip (mtime unchanged)
    const result = await ingestIndexEpisode(opts);
    expect(result).toBe(false);
    expect(lastRequest["/messages"]).toBeUndefined();
  });

  test("re-indexes when file is modified", async () => {
    const filePath = path.join(tmpDir, "test.md");
    fs.writeFileSync(filePath, "Original content");

    const client = new GraphitiClient(
      `http://127.0.0.1:${getMockPort()}`,
      "test-group",
      undefined,
      undefined,
      NOOP_LOG,
    );

    const opts = {
      client,
      filePath: "memory/test.md",
      absolutePath: filePath,
      groupId: "test-group",
      debugLog: NOOP_LOG,
      stateDir,
    };

    await ingestIndexEpisode(opts);
    resetMockState();

    // Modify the file (force different mtime)
    const futureTime = new Date(Date.now() + 5000);
    fs.writeFileSync(filePath, "Updated content");
    fs.utimesSync(filePath, futureTime, futureTime);

    const result = await ingestIndexEpisode(opts);
    expect(result).toBe(true);
    const req = lastRequest["/messages"] as any;
    expect(req.messages[0].content).toContain("Updated content");
  });

  test("returns false for non-existent file", async () => {
    const client = new GraphitiClient(
      `http://127.0.0.1:${getMockPort()}`,
      "test-group",
      undefined,
      undefined,
      NOOP_LOG,
    );

    const result = await ingestIndexEpisode({
      client,
      filePath: "memory/nope.md",
      absolutePath: path.join(tmpDir, "nope.md"),
      groupId: "test-group",
      debugLog: NOOP_LOG,
      stateDir,
    });

    expect(result).toBe(false);
  });

  test("concurrent calls do not lose each other's state entries", async () => {
    const client = new GraphitiClient(`http://127.0.0.1:${getMockPort()}`, "test-group", undefined, undefined, NOOP_LOG);
    const names = ["a.md", "b.md", "c.md"];
    for (const n of names) fs.writeFileSync(path.join(tmpDir, n), `content of ${n}`);
    // Hold each ingest open so unserialised calls would all read the same empty state.
    mockOverrides.ingestDelayMs = 20;

    const results = await Promise.all(names.map((n) => ingestIndexEpisode({
      client,
      filePath: `memory/${n}`,
      absolutePath: path.join(tmpDir, n),
      groupId: "test-group",
      debugLog: NOOP_LOG,
      stateDir,
    })));

    expect(results).toEqual([true, true, true]);
    expect(Object.keys(readIndexState(stateDir)).sort()).toEqual(names.map((n) => `memory/${n}`));
  });

  test("a failed ingest rejects but does not block later calls", async () => {
    const client = new GraphitiClient(`http://127.0.0.1:${getMockPort()}`, "test-group", undefined, undefined, NOOP_LOG);
    fs.writeFileSync(path.join(tmpDir, "bad.md"), "bad");
    fs.writeFileSync(path.join(tmpDir, "good.md"), "good");
    mockOverrides.ingestFailOnName = "bad.md";
    const base = { client, groupId: "test-group", debugLog: NOOP_LOG, stateDir };

    const bad = ingestIndexEpisode({ ...base, filePath: "memory/bad.md", absolutePath: path.join(tmpDir, "bad.md") });
    const good = ingestIndexEpisode({ ...base, filePath: "memory/good.md", absolutePath: path.join(tmpDir, "good.md") });

    await expect(bad).rejects.toThrow(/returned 500/);
    expect(await good).toBe(true);
    expect(Object.keys(readIndexState(stateDir))).toEqual(["memory/good.md"]);
  });
});
