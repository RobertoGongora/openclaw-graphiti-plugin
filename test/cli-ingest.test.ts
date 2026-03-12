/**
 * CLI ingest command tests.
 *
 * Covers happy path (--content), file ingestion (--source-file),
 * and error paths (missing file, server error).
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
  mockOverrides,
  lastRequest,
} from "./helpers.js";

describe("CLI ingest command", () => {
  let tmpDir: string;

  beforeAll(startMockServer);
  afterAll(stopMockServer);

  beforeEach(() => {
    resetMockState();
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "cli-ingest-test-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  /**
   * Register the plugin and extract the ingest CLI action handler.
   * Returns a function that invokes the handler with the given opts.
   */
  async function getIngestAction(configOverrides: Record<string, unknown> = {}) {
    const { default: plugin } = await import("../index.js");
    const { api, clis } = createMockApi(configOverrides);
    plugin.register(api as any);

    const graphitiCli = clis.find((c) => c.opts.commands.includes("graphiti"));
    expect(graphitiCli).toBeDefined();

    // Build a mock Commander chain to capture the ingest action
    let ingestAction: ((opts: any) => Promise<void>) | undefined;

    const mockCmd = {
      description: () => mockCmd,
      action: (fn: any) => { /* default action, ignore */ return mockCmd; },
      command: (name: string) => {
        const sub: any = {
          description: () => sub,
          argument: () => sub,
          option: () => sub,
          action: (fn: any) => {
            if (name === "ingest") ingestAction = fn;
            return sub;
          },
        };
        return sub;
      },
      outputHelp: () => {},
    };
    const mockProgram = { command: () => mockCmd };

    graphitiCli!.reg({ program: mockProgram });
    expect(ingestAction).toBeDefined();
    return ingestAction!;
  }

  test("--content happy path ingests text and logs confirmation", async () => {
    const action = await getIngestAction();

    const logs: string[] = [];
    const origLog = console.log;
    console.log = (...args: any[]) => logs.push(args.join(" "));
    try {
      await action({ content: "Important architecture decision" });
    } finally {
      console.log = origLog;
    }

    // Should have sent to mock server
    const req = lastRequest["/messages"] as any;
    expect(req).toBeDefined();
    expect(req.messages[0].content).toBe("Important architecture decision");

    // Should log confirmation
    expect(logs.some((l) => l.includes("Ingested"))).toBe(true);
  });

  test("--source-file happy path reads file and ingests", async () => {
    const action = await getIngestAction();

    const testFile = path.join(tmpDir, "notes.md");
    fs.writeFileSync(testFile, "# Meeting Notes\nDecided to use PostgreSQL.");

    const logs: string[] = [];
    const origLog = console.log;
    console.log = (...args: any[]) => logs.push(args.join(" "));
    try {
      await action({ sourceFile: testFile });
    } finally {
      console.log = origLog;
    }

    const req = lastRequest["/messages"] as any;
    expect(req).toBeDefined();
    expect(req.messages[0].content).toContain("PostgreSQL");
    expect(logs.some((l) => l.includes("notes.md"))).toBe(true);
  });

  test("--source-file uses path.basename() for provenance file field", async () => {
    const action = await getIngestAction();

    const testFile = path.join(tmpDir, "secret-dir", "data.txt");
    fs.mkdirSync(path.dirname(testFile), { recursive: true });
    fs.writeFileSync(testFile, "Some content to ingest for testing.");

    const origLog = console.log;
    console.log = () => {};
    try {
      await action({ sourceFile: testFile });
    } finally {
      console.log = origLog;
    }

    const req = lastRequest["/messages"] as any;
    const prov = JSON.parse(req.messages[0].source_description);
    // Should be basename only, not the full absolute path
    expect(prov.file).toBe("data.txt");
    expect(prov.file).not.toContain(tmpDir);
  });

  test("missing file prints error and sets exitCode", async () => {
    const action = await getIngestAction();

    const errors: string[] = [];
    const origError = console.error;
    console.error = (...args: any[]) => errors.push(args.join(" "));
    const origExitCode = process.exitCode;
    try {
      await action({ sourceFile: "/nonexistent/path/file.txt" });
    } finally {
      console.error = origError;
    }

    expect(errors.some((e) => e.includes("Ingest failed"))).toBe(true);
    expect(process.exitCode).toBe(1);
    process.exitCode = origExitCode;
  });

  test("server error prints error and sets exitCode", async () => {
    mockOverrides.ingestStatus = 500;
    const action = await getIngestAction();

    const errors: string[] = [];
    const origError = console.error;
    console.error = (...args: any[]) => errors.push(args.join(" "));
    const origExitCode = process.exitCode;
    try {
      await action({ content: "test content" });
    } finally {
      console.error = origError;
    }

    expect(errors.some((e) => e.includes("Ingest failed"))).toBe(true);
    expect(process.exitCode).toBe(1);
    process.exitCode = origExitCode;
  });

  test("no --source-file or --content prints usage error", async () => {
    const action = await getIngestAction();

    const errors: string[] = [];
    const origError = console.error;
    console.error = (...args: any[]) => errors.push(args.join(" "));
    const origExitCode = process.exitCode;
    try {
      await action({});
    } finally {
      console.error = origError;
    }

    expect(errors.some((e) => e.includes("--source-file or --content"))).toBe(true);
    expect(process.exitCode).toBe(1);
    process.exitCode = origExitCode;
  });

  test("--source-file does not truncate by default (unlimited)", async () => {
    const action = await getIngestAction();

    const largeContent = "x".repeat(15_000);
    const testFile = path.join(tmpDir, "large-file-unlimited.txt");
    fs.writeFileSync(testFile, largeContent);

    const warns: string[] = [];
    const origLog = console.log;
    const origWarn = console.warn;
    console.log = () => {};
    console.warn = (...args: any[]) => warns.push(args.join(" "));
    try {
      await action({ sourceFile: testFile });
    } finally {
      console.log = origLog;
      console.warn = origWarn;
    }

    // Should NOT warn about truncation
    expect(warns.length).toBe(0);

    // Full content should be sent
    const req = lastRequest["/messages"] as any;
    expect(req).toBeDefined();
    expect(req.messages[0].content.length).toBe(15_000);
  });

  test("--source-file truncates content exceeding maxEpisodeChars", async () => {
    const action = await getIngestAction({ maxEpisodeChars: 12_000 });

    const largeContent = "x".repeat(15_000);
    const testFile = path.join(tmpDir, "large-file.txt");
    fs.writeFileSync(testFile, largeContent);

    const logs: string[] = [];
    const warns: string[] = [];
    const origLog = console.log;
    const origWarn = console.warn;
    console.log = (...args: any[]) => logs.push(args.join(" "));
    console.warn = (...args: any[]) => warns.push(args.join(" "));
    try {
      await action({ sourceFile: testFile });
    } finally {
      console.log = origLog;
      console.warn = origWarn;
    }

    // Should warn about truncation
    expect(warns.some((w) => w.includes("truncated to 12000 characters"))).toBe(true);

    // Content sent to server should be capped at 12,000
    const req = lastRequest["/messages"] as any;
    expect(req).toBeDefined();
    expect(req.messages[0].content.length).toBe(12_000);

    // Log should show truncated size
    expect(logs.some((l) => l.includes("12000 chars"))).toBe(true);
  });
});
