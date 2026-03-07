/**
 * Unit tests for memory-index module.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import {
  extractMemoryPath,
  indexEpisodeName,
  buildIndexContent,
  readMemoryFileMeta,
  readIndexState,
  writeIndexState,
  upsertIndexEpisode,
  scanMemoryFiles,
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
// extractMemoryPath
// ============================================================================

describe("extractMemoryPath", () => {
  test("extracts from file_path with /memory/ segment", () => {
    expect(
      extractMemoryPath("Write", { file_path: "/Users/rob/.claude/memory/2026-03-05.md" }),
    ).toBe("memory/2026-03-05.md");
  });

  test("extracts from path param", () => {
    expect(
      extractMemoryPath("Edit", { path: "/home/user/project/memory/notes.md" }),
    ).toBe("memory/notes.md");
  });

  test("extracts from filePath param", () => {
    expect(
      extractMemoryPath("write", { filePath: "memory/MEMORY.md" }),
    ).toBe("memory/MEMORY.md");
  });

  test("extracts from target_file param", () => {
    expect(
      extractMemoryPath("edit", { target_file: "/foo/memory/sub/deep.md" }),
    ).toBe("memory/sub/deep.md");
  });

  test("returns null for non-write tools", () => {
    expect(
      extractMemoryPath("Read", { file_path: "memory/test.md" }),
    ).toBeNull();
  });

  test("returns null for non-memory paths", () => {
    expect(
      extractMemoryPath("Write", { file_path: "/Users/rob/src/index.ts" }),
    ).toBeNull();
  });

  test("returns null for missing params", () => {
    expect(extractMemoryPath("Write", undefined)).toBeNull();
    expect(extractMemoryPath("Write", {})).toBeNull();
  });

  test("returns null for non-string path values", () => {
    expect(extractMemoryPath("Write", { file_path: 42 })).toBeNull();
  });

  test("handles write_file and create_file tool names", () => {
    expect(
      extractMemoryPath("write_file", { file_path: "memory/test.md" }),
    ).toBe("memory/test.md");
    expect(
      extractMemoryPath("create_file", { path: "/x/memory/y.md" }),
    ).toBe("memory/y.md");
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
// upsertIndexEpisode — integration with mock server
// ============================================================================

describe("upsertIndexEpisode", () => {
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

    const result = await upsertIndexEpisode({
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
    await upsertIndexEpisode(opts);
    resetMockState();

    // Second call — should skip (mtime unchanged)
    const result = await upsertIndexEpisode(opts);
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

    await upsertIndexEpisode(opts);
    resetMockState();

    // Modify the file (force different mtime)
    const futureTime = new Date(Date.now() + 5000);
    fs.writeFileSync(filePath, "Updated content");
    fs.utimesSync(filePath, futureTime, futureTime);

    const result = await upsertIndexEpisode(opts);
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

    const result = await upsertIndexEpisode({
      client,
      filePath: "memory/nope.md",
      absolutePath: path.join(tmpDir, "nope.md"),
      groupId: "test-group",
      debugLog: NOOP_LOG,
      stateDir,
    });

    expect(result).toBe(false);
  });
});
