/**
 * Tests for the structured debug log.
 *
 * Covers: DebugLog class behavior, client integration, and PII safety.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { DebugLog, NOOP_LOG } from "../debug-log.js";
import { GraphitiClient } from "../client.js";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  getMockPort,
  mockOverrides,
} from "./helpers.js";

// ============================================================================
// Unit tests for DebugLog class
// ============================================================================

describe("DebugLog", () => {
  let tmpDir: string;
  let logPath: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "debug-log-test-"));
    logPath = path.join(tmpDir, "test.log");
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test("writes structured line with timestamp and fields", () => {
    const log = new DebugLog(logPath);
    log.log("search", { status: 200, count: 5 });

    const content = fs.readFileSync(logPath, "utf-8");
    expect(content).toMatch(/^\d{4}-\d{2}-\d{2}T/); // ISO timestamp
    expect(content).toContain("[graphiti]");
    expect(content).toContain("search");
    expect(content).toContain("status=200");
    expect(content).toContain("count=5");
  });

  test("pads event name for alignment", () => {
    const log = new DebugLog(logPath);
    log.log("search", { status: 200 });
    log.log("healthcheck", { status: 200 });

    const lines = fs.readFileSync(logPath, "utf-8").trimEnd().split("\n");
    // "search" (6 chars) should be padded to 12
    expect(lines[0]).toContain("search      ");
    // "healthcheck" (11 chars) should be padded to 12
    expect(lines[1]).toContain("healthcheck ");
  });

  test("quotes field values containing spaces", () => {
    const log = new DebugLog(logPath);
    log.log("test", { error: "not found" });

    const content = fs.readFileSync(logPath, "utf-8");
    expect(content).toContain('error="not found"');
  });

  test("filters out undefined values", () => {
    const log = new DebugLog(logPath);
    log.log("test", { status: 200, error: undefined });

    const content = fs.readFileSync(logPath, "utf-8");
    expect(content).toContain("status=200");
    expect(content).not.toContain("error");
  });

  test("tail() returns last N lines", () => {
    const log = new DebugLog(logPath);
    log.log("a", { n: 1 });
    log.log("b", { n: 2 });
    log.log("c", { n: 3 });

    const tail = log.tail(2);
    const lines = tail.split("\n");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain("b");
    expect(lines[1]).toContain("c");
  });

  test("tail() returns empty string for missing file", () => {
    const log = new DebugLog(path.join(tmpDir, "nonexistent.log"));
    expect(log.tail(10)).toBe("");
  });

  test("clear() truncates log file", () => {
    const log = new DebugLog(logPath);
    log.log("test", { n: 1 });
    expect(fs.readFileSync(logPath, "utf-8").length).toBeGreaterThan(0);

    log.clear();
    expect(fs.readFileSync(logPath, "utf-8")).toBe("");
  });

  test("disabled instance writes nothing", () => {
    const log = new DebugLog(logPath, false);
    log.log("test", { n: 1 });
    expect(fs.existsSync(logPath)).toBe(false);
  });

  test("NOOP_LOG writes nothing", () => {
    NOOP_LOG.log("test", { n: 1 });
    // Just verify it doesn't throw
  });

  test("creates directory if missing", () => {
    const nested = path.join(tmpDir, "a", "b", "c", "test.log");
    const log = new DebugLog(nested);
    log.log("test", { n: 1 });

    expect(fs.existsSync(nested)).toBe(true);
  });

  test("handles null field values", () => {
    const log = new DebugLog(logPath);
    log.log("test", { value: null });

    const content = fs.readFileSync(logPath, "utf-8");
    expect(content).toContain("value=null");
  });

  test("handles boolean field values", () => {
    const log = new DebugLog(logPath);
    log.log("test", { skipped: true });

    const content = fs.readFileSync(logPath, "utf-8");
    expect(content).toContain("skipped=true");
  });

  test("escapes newlines in field values", () => {
    const log = new DebugLog(logPath);
    log.log("test", { error: "line1\nline2" });

    const content = fs.readFileSync(logPath, "utf-8");
    // Should be a single line — newline escaped
    const lines = content.trimEnd().split("\n");
    expect(lines).toHaveLength(1);
    expect(content).toContain("error=line1\\nline2");
  });

  test("escapes quotes in field values", () => {
    const log = new DebugLog(logPath);
    log.log("test", { msg: 'has "quotes" inside' });

    const content = fs.readFileSync(logPath, "utf-8");
    // Quotes should be escaped, not breaking the format
    expect(content).not.toMatch(/msg="has "quotes"/);
    expect(content).toContain('\\"');
  });
});

// ============================================================================
// Integration tests with mock server
// ============================================================================

describe("DebugLog integration with GraphitiClient", () => {
  let tmpDir: string;
  let logPath: string;

  beforeAll(startMockServer);
  afterAll(stopMockServer);

  beforeEach(() => {
    resetMockState();
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "debug-log-int-"));
    logPath = path.join(tmpDir, "test.log");
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  function instrumentedClient(url?: string) {
    const debugLog = new DebugLog(logPath);
    return new GraphitiClient(
      url ?? `http://127.0.0.1:${getMockPort()}`,
      "test-group",
      undefined,
      undefined,
      debugLog,
    );
  }

  function logContent(): string {
    return fs.readFileSync(logPath, "utf-8");
  }

  test("search() logs status, group, count, and duration", async () => {
    await instrumentedClient().search("test query", 5);

    const content = logContent();
    expect(content).toContain("search");
    expect(content).toContain("status=200");
    expect(content).toContain("group=test-group");
    expect(content).toContain("count=2");
    expect(content).toMatch(/ms=\d+/);
  });

  test("episodes() logs on success", async () => {
    await instrumentedClient().episodes(10);

    const content = logContent();
    expect(content).toContain("episodes");
    expect(content).toContain("status=200");
    expect(content).toContain("group=test-group");
    expect(content).toContain("count=1");
    expect(content).toMatch(/ms=\d+/);
  });

  test("episodes() logs error when server is unreachable", async () => {
    await instrumentedClient("http://127.0.0.1:1").episodes(10);

    const content = logContent();
    expect(content).toContain("episodes");
    expect(content).toContain("error=unreachable");
    expect(content).toContain("group=test-group");
  });

  test("healthy() logs healthcheck result", async () => {
    await instrumentedClient().healthy();

    const content = logContent();
    expect(content).toContain("healthcheck");
    expect(content).toContain("status=200");
    expect(content).toMatch(/ms=\d+/);
  });

  test("healthy() logs unreachable error", async () => {
    await instrumentedClient("http://127.0.0.1:1").healthy();

    const content = logContent();
    expect(content).toContain("healthcheck");
    expect(content).toContain("error=unreachable");
  });

  test("ingest() logs message count", async () => {
    await instrumentedClient().ingest([
      { content: "test", role_type: "user", role: "user" },
      { content: "reply", role_type: "assistant", role: "assistant" },
    ]);

    const content = logContent();
    expect(content).toContain("ingest");
    expect(content).toContain("status=202");
    expect(content).toContain("messages=2");
    expect(content).toContain("group=test-group");
  });

  test("getMemory() logs status and count", async () => {
    await instrumentedClient().getMemory(
      [{ content: "Hello", role_type: "user", role: "user" }],
      5,
    );

    const content = logContent();
    expect(content).toContain("get-memory");
    expect(content).toContain("status=200");
    expect(content).toContain("count=2");
  });
});

// ============================================================================
// PII safety tests
// ============================================================================

describe("DebugLog PII safety", () => {
  let tmpDir: string;
  let logPath: string;

  beforeAll(startMockServer);
  afterAll(stopMockServer);

  beforeEach(() => {
    resetMockState();
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "debug-log-pii-"));
    logPath = path.join(tmpDir, "test.log");
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  function instrumentedClient() {
    const debugLog = new DebugLog(logPath);
    return new GraphitiClient(
      `http://127.0.0.1:${getMockPort()}`,
      "test-group",
      undefined,
      undefined,
      debugLog,
    );
  }

  function logContent(): string {
    try { return fs.readFileSync(logPath, "utf-8"); }
    catch { return ""; }
  }

  test("ingest() does NOT log message content", async () => {
    const pii = "My SSN is 123-45-6789 and I live at 742 Evergreen Terrace";
    await instrumentedClient().ingest([
      { content: pii, role_type: "user", role: "user" },
    ]);

    const content = logContent();
    expect(content).not.toContain("123-45-6789");
    expect(content).not.toContain("Evergreen");
    expect(content).not.toContain(pii);
    // Should only log the count
    expect(content).toContain("messages=1");
  });

  test("search() does NOT log query string", async () => {
    const query = "What is Alice's home address and phone number?";
    await instrumentedClient().search(query, 5);

    const content = logContent();
    expect(content).not.toContain("Alice");
    expect(content).not.toContain("home address");
    expect(content).not.toContain("phone number");
    expect(content).not.toContain(query);
  });

  test("getMemory() does NOT log message content", async () => {
    const pii = "My credit card is 4111-1111-1111-1111";
    await instrumentedClient().getMemory(
      [{ content: pii, role_type: "user", role: "user" }],
      5,
    );

    const content = logContent();
    expect(content).not.toContain("4111");
    expect(content).not.toContain("credit card");
    expect(content).not.toContain(pii);
  });

  test("error responses log sanitized error, not request body", async () => {
    mockOverrides.searchStatus = 500;
    try {
      await instrumentedClient().search("secret query about my medical records", 5);
    } catch { /* expected */ }

    const content = logContent();
    expect(content).not.toContain("secret query");
    expect(content).not.toContain("medical records");
    // Should log the error from the server response
    expect(content).toContain("status=500");
  });

  test("episodes() error logs error field instead of silently returning []", async () => {
    const debugLog = new DebugLog(logPath);
    const client = new GraphitiClient(
      "http://127.0.0.1:1",
      "test-group",
      undefined,
      undefined,
      debugLog,
    );

    const result = await client.episodes(10);
    expect(result).toEqual([]); // Still returns empty (backward compat)

    const content = logContent();
    expect(content).toContain("episodes");
    expect(content).toContain("error=unreachable");
    // The error is now observable, not silent
  });

  test("server error response body with PII is NOT logged to debug file", async () => {
    // Simulate upstream proxy echoing PII in error response
    const piiBody = "Error: invalid request from user john.doe@example.com SSN 987-65-4321";
    mockOverrides.searchStatus = 500;
    mockOverrides.searchErrorBody = piiBody;

    try {
      await instrumentedClient().search("test query", 5);
    } catch { /* expected */ }

    const content = logContent();
    // PII from error response body must NOT appear in the log
    expect(content).not.toContain("john.doe@example.com");
    expect(content).not.toContain("987-65-4321");
    expect(content).not.toContain(piiBody);
    // Should log a generic error label instead
    expect(content).toContain("HTTP error");
    expect(content).toContain("status=500");
  });
});
