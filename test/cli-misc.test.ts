/**
 * CLI tests for status, episodes, logs, backfill, and --limit validation.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  createMockApi,
  captureCliActions,
  runCli,
  mockOverrides,
  lastRequest,
  ingestRequests,
} from "./helpers.js";
import { EPISODE_COUNT_CAP } from "../client.js";
import { readIndexState } from "../memory-index.js";

describe("CLI", () => {
  let tmpDir: string;
  let realHome: string | undefined;

  beforeAll(startMockServer);
  afterAll(stopMockServer);

  beforeEach(() => {
    resetMockState();
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "cli-misc-test-"));
    // Index state and the default log live under ~/.openclaw — sandbox the home dir.
    realHome = process.env.HOME;
    process.env.HOME = path.join(tmpDir, "home");
  });

  afterEach(() => {
    process.env.HOME = realHome;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  async function getActions(config: Record<string, unknown> = {}) {
    const { default: plugin } = await import("../index.js");
    const { api, clis } = createMockApi({ logFile: path.join(tmpDir, "plugin.log"), ...config });
    plugin.register(api as any);
    return captureCliActions(clis);
  }

  // -- --limit validation --

  describe("--limit validation", () => {
    for (const bad of ["abc", "0", "-3", "1.5", ""]) {
      test(`search rejects --limit "${bad}" with exit code 1 and no request`, async () => {
        const actions = await getActions();
        const { errors, exitCode } = await runCli(() => actions.search("query", { limit: bad }));

        expect(exitCode).toBe(1);
        expect(errors.join("\n")).toContain("Invalid --limit");
        expect(lastRequest["/search"]).toBeUndefined();
      });
    }

    test("episodes rejects a non-numeric --limit", async () => {
      const actions = await getActions();
      const { errors, exitCode } = await runCli(() => actions.episodes({ limit: "ten" }));

      expect(exitCode).toBe(1);
      expect(errors.join("\n")).toContain("Invalid --limit");
      expect(lastRequest["/episodes"]).toBeUndefined();
    });

    test("a valid --limit is sent as a number", async () => {
      const actions = await getActions();
      const { exitCode } = await runCli(() => actions.search("query", { limit: "7" }));

      expect(exitCode).toBeUndefined();
      expect((lastRequest["/search"] as any).max_facts).toBe(7);
    });
  });

  // -- status --

  describe("status", () => {
    test("shows the exact count below the cap", async () => {
      mockOverrides.episodes = Array.from({ length: 3 }, (_, i) => ({ uuid: `e${i}`, created_at: "2024-01-01T00:00:00Z" }));
      const actions = await getActions();
      const { logs } = await runCli(() => actions.status());

      expect(logs).toContain("  Episodes: 3");
      expect((lastRequest["/episodes"] as any).last_n).toBe(String(EPISODE_COUNT_CAP));
    });

    test(`shows "${EPISODE_COUNT_CAP}+" when the count hits the cap`, async () => {
      mockOverrides.episodes = Array.from({ length: EPISODE_COUNT_CAP + 5 }, (_, i) => ({ uuid: `e${i}` }));
      const actions = await getActions();
      const { logs } = await runCli(() => actions.status());

      expect(logs).toContain(`  Episodes: ${EPISODE_COUNT_CAP}+`);
    });
  });

  // -- logs --

  describe("logs", () => {
    test("prints the log path and recent entries", async () => {
      const actions = await getActions();
      await runCli(() => actions.search("query", { limit: "5" })); // writes a "search" entry
      const { logs } = await runCli(() => actions.logs({}));

      expect(logs[0]).toBe(`Log file: ${path.join(tmpDir, "plugin.log")}`);
      expect(logs.join("\n")).toMatch(/\[graphiti\] search\s+status=200/);
    });

    test("prints a placeholder when the log is empty", async () => {
      const actions = await getActions();
      await runCli(() => actions.logs({ clear: true })); // drop the "register" entry
      const { logs } = await runCli(() => actions.logs({}));
      expect(logs).toContain("(no log entries)");
    });

    test("--clear truncates the log", async () => {
      const actions = await getActions();
      await runCli(() => actions.search("query", { limit: "5" }));
      expect(fs.statSync(path.join(tmpDir, "plugin.log")).size).toBeGreaterThan(0);

      const { logs } = await runCli(() => actions.logs({ clear: true }));

      expect(logs).toContain("Log cleared.");
      expect(fs.statSync(path.join(tmpDir, "plugin.log")).size).toBe(0);
    });
  });

  // -- backfill --

  describe("backfill", () => {
    let memoryDir: string;
    const stateDir = () => path.join(tmpDir, "home", ".openclaw", "state", "graphiti");

    beforeEach(() => {
      memoryDir = path.join(tmpDir, "memory");
      fs.mkdirSync(memoryDir);
      for (const n of ["a.md", "b.md", "c.md"]) fs.writeFileSync(path.join(memoryDir, n), `content ${n}`);
      fs.writeFileSync(path.join(memoryDir, "skip.json"), "{}");
    });

    test("indexes every indexable file and records state", async () => {
      const actions = await getActions();
      const { logs, exitCode } = await runCli(() => actions.backfill({ dir: memoryDir }));

      expect(exitCode).toBeUndefined();
      expect(logs.at(-1)).toBe("Indexed 3 files (1 filtered)");
      expect(ingestRequests.map((r) => r.messages[0].name).sort()).toEqual([
        "memory-index::memory/a.md", "memory-index::memory/b.md", "memory-index::memory/c.md",
      ]);
      expect(Object.keys(readIndexState(stateDir())).sort()).toEqual(["memory/a.md", "memory/b.md", "memory/c.md"]);
    });

    test("--dry-run ingests nothing", async () => {
      const actions = await getActions();
      const { logs } = await runCli(() => actions.backfill({ dir: memoryDir, dryRun: true }));

      expect(ingestRequests).toHaveLength(0);
      expect(logs.at(-1)).toContain("3 new");
    });

    test("a mid-run failure keeps the progress and the re-run adds no duplicates", async () => {
      mockOverrides.ingestFailOnName = "b.md";
      const actions = await getActions();
      const first = await runCli(() => actions.backfill({ dir: memoryDir }));

      expect(first.exitCode).toBe(1);
      expect(first.errors.join("\n")).toContain("[failed] memory/b.md");
      expect(first.logs.at(-1)).toBe("Indexed 2 files (1 filtered, 1 failed)");
      // a.md and c.md survived the failure of b.md
      expect(Object.keys(readIndexState(stateDir())).sort()).toEqual(["memory/a.md", "memory/c.md"]);

      resetMockState();
      const second = await runCli(() => actions.backfill({ dir: memoryDir }));

      expect(second.exitCode).toBeUndefined();
      expect(ingestRequests.map((r) => r.messages[0].name)).toEqual(["memory-index::memory/b.md"]);
      expect(second.logs.at(-1)).toBe("Indexed 1 files (2 unchanged, 1 filtered)");
    });

    test("a corrupt state file is treated as empty", async () => {
      fs.mkdirSync(stateDir(), { recursive: true });
      fs.writeFileSync(path.join(stateDir(), "graphiti-memory-index.json"), "null");
      const actions = await getActions();
      const { logs } = await runCli(() => actions.backfill({ dir: memoryDir }));

      expect(logs.at(-1)).toBe("Indexed 3 files (1 filtered)");
    });

    test("aborts when the server is unreachable", async () => {
      mockOverrides.healthy = false;
      const actions = await getActions();
      const { logs } = await runCli(() => actions.backfill({ dir: memoryDir }));

      expect(logs.at(-1)).toContain("unreachable");
      expect(ingestRequests).toHaveLength(0);
    });
  });
});
