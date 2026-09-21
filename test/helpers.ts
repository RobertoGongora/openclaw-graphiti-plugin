/**
 * Shared test infrastructure for Graphiti plugin tests.
 *
 * Provides:
 * - Mock HTTP server matching the real Graphiti FastAPI contract
 * - Mock OpenClaw plugin API factory
 * - Sample fixtures (facts, episodes)
 */

import http from "node:http";
import { vi } from "vitest";
import type { GraphitiFact } from "../client.js";

// ============================================================================
// Fixtures
// ============================================================================

export const SAMPLE_FACTS: GraphitiFact[] = [
  {
    uuid: "fact-001",
    name: "WORKS_AT",
    fact: "Alice works at Acme Corp",
    valid_at: "2024-01-15T10:30:00+00:00",
    invalid_at: null,
    created_at: "2024-01-15T10:30:00+00:00",
    expired_at: null,
  },
  {
    uuid: "fact-002",
    name: "PREFERS",
    fact: "User prefers dark mode",
    valid_at: "2024-02-01T00:00:00+00:00",
    invalid_at: null,
    created_at: "2024-02-01T00:00:00+00:00",
    expired_at: null,
  },
];

export const SAMPLE_EPISODES = [
  {
    uuid: "ep-001",
    name: "session-reset-1700000000000",
    group_id: "test-group",
    labels: [],
    created_at: "2024-01-15T10:30:00+00:00",
    source: "message",
    source_description: JSON.stringify({ plugin: "openclaw-graphiti", event: "before_compaction", ts: "2024-01-15T10:30:00.000Z", group_id: "test-group" }),
    content: "user(user): Hello",
    valid_at: "2024-01-15T10:30:00+00:00",
    entity_edges: ["fact-001"],
  },
];

export const SAMPLE_EPISODES_WITH_SESSION = [
  {
    uuid: "ep-sess-001",
    name: "turn-sess-1-1700000001",
    group_id: "test-group",
    created_at: "2024-01-15T11:00:00+00:00",
    source: "message",
    source_description: JSON.stringify({
      plugin: "openclaw-graphiti",
      event: "after_turn",
      session_key: "sess-1",
      group_id: "test-group",
    }),
    content: "user: What is the architecture of our system?\n\nassistant: The system uses microservices with Neo4j for the knowledge graph.",
  },
  {
    uuid: "ep-sess-002",
    name: "turn-other-1700000002",
    group_id: "test-group",
    created_at: "2024-01-15T10:00:00+00:00",
    source: "message",
    source_description: JSON.stringify({
      plugin: "openclaw-graphiti",
      event: "after_turn",
      session_key: "other-session",
      group_id: "test-group",
    }),
    content: "user: Unrelated conversation from another session.\n\nassistant: Different topic entirely.",
  },
  {
    uuid: "ep-sess-003",
    name: "turn-sess-1-thread-a-1700000003",
    group_id: "test-group",
    created_at: "2024-01-15T11:30:00+00:00",
    source: "message",
    source_description: JSON.stringify({
      plugin: "openclaw-graphiti",
      event: "after_turn",
      session_key: "sess-1",
      thread_id: "thread-a",
      group_id: "test-group",
    }),
    content: "user: Tell me about the deployment pipeline.\n\nassistant: We use GitHub Actions with Docker containers.",
  },
];

// ============================================================================
// Mock HTTP Server
// ============================================================================

export type MockOverrides = {
  healthy?: boolean;
  searchFacts?: GraphitiFact[];
  getMemoryFacts?: GraphitiFact[];
  ingestStatus?: number;
  ingestBody?: Record<string, unknown>;
  searchStatus?: number;
  searchErrorBody?: string;
  episodes?: any[];
  episodesStatus?: number;
  /** Raw JSON body for POST /search (e.g. "null"), bypassing `searchFacts`. */
  searchRawBody?: string;
  /** Raw JSON body for GET /episodes (e.g. "{}"), bypassing `episodes`. */
  episodesRawBody?: string;
  /** Status for DELETE /entity-edge and /episode (default 200). */
  deleteStatus?: number;
  /** Delay before answering POST /messages, in ms. */
  ingestDelayMs?: number;
  /** Answer POST /messages with 500 when the first message name contains this. */
  ingestFailOnName?: string;
  /** Pathname prefixes that never get a response (for abort-timeout tests). */
  hangPaths?: string[];
  /** Pathname prefixes that get 200 headers and half a body, then stall. */
  stallBodyPaths?: string[];
};

let server: http.Server;
let port: number;

export let mockOverrides: MockOverrides = {};
export const lastRequest: Record<string, unknown> = {};
/** Headers from the most recent request to each path (lowercase keys). */
export const lastHeaders: Record<string, Record<string, string>> = {};
/** Number of requests received per pathname since the last reset. */
export const requestCounts: Record<string, number> = {};
/** Every POST /messages body since the last reset, in arrival order. */
export const ingestRequests: any[] = [];

export function getMockPort(): number {
  return port;
}

export function resetMockState(): void {
  mockOverrides = {};
  for (const key of Object.keys(lastRequest)) delete lastRequest[key];
  for (const key of Object.keys(lastHeaders)) delete lastHeaders[key];
  for (const key of Object.keys(requestCounts)) delete requestCounts[key];
  ingestRequests.length = 0;
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (chunk: Buffer) => (data += chunk.toString()));
    req.on("end", () => resolve(data));
  });
}

function captureHeaders(pathname: string, req: http.IncomingMessage): void {
  const h: Record<string, string> = {};
  for (const [key, val] of Object.entries(req.headers)) {
    if (typeof val === "string") h[key] = val;
  }
  lastHeaders[pathname] = h;
}

export function startMockServer(): Promise<void> {
  return new Promise((resolve) => {
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url ?? "/", "http://localhost");

      // Capture headers for every request
      captureHeaders(url.pathname, req);
      requestCounts[url.pathname] = (requestCounts[url.pathname] ?? 0) + 1;

      // Simulate a stalled server: accept the request, never answer.
      if (mockOverrides.hangPaths?.some((p) => url.pathname.startsWith(p))) {
        req.resume();
        return;
      }
      if (mockOverrides.stallBodyPaths?.some((p) => url.pathname.startsWith(p))) {
        req.resume();
        res.writeHead(200, { "Content-Type": "application/json" });
        res.write('{"facts": [');
        return;
      }

      // GET /healthcheck
      if (req.method === "GET" && url.pathname === "/healthcheck") {
        if (mockOverrides.healthy === false) {
          res.writeHead(503);
          res.end();
          return;
        }
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: "healthy" }));
        return;
      }

      // POST /search
      if (req.method === "POST" && url.pathname === "/search") {
        const body = await readBody(req);
        lastRequest["/search"] = JSON.parse(body);
        const status = mockOverrides.searchStatus ?? 200;
        if (status !== 200) {
          res.writeHead(status);
          res.end(mockOverrides.searchErrorBody ?? JSON.stringify({ detail: "search error" }));
          return;
        }
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          mockOverrides.searchRawBody ??
          JSON.stringify({
            facts: mockOverrides.searchFacts ?? SAMPLE_FACTS,
          }),
        );
        return;
      }

      // POST /messages  (returns 202 like real Graphiti)
      if (req.method === "POST" && url.pathname === "/messages") {
        const body = await readBody(req);
        const parsed = JSON.parse(body);
        lastRequest["/messages"] = parsed;
        ingestRequests.push(parsed);
        if (mockOverrides.ingestDelayMs) {
          await new Promise((r) => setTimeout(r, mockOverrides.ingestDelayMs));
        }
        const failOn = mockOverrides.ingestFailOnName;
        if (failOn && String(parsed?.messages?.[0]?.name ?? "").includes(failOn)) {
          res.writeHead(500);
          res.end(JSON.stringify({ detail: "ingest error" }));
          return;
        }
        const status = mockOverrides.ingestStatus ?? 202;
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify(
            mockOverrides.ingestBody ?? {
              message: "Messages added to processing queue",
              success: true,
            },
          ),
        );
        return;
      }

      // POST /get-memory
      if (req.method === "POST" && url.pathname === "/get-memory") {
        const body = await readBody(req);
        lastRequest["/get-memory"] = JSON.parse(body);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            facts: mockOverrides.getMemoryFacts ?? mockOverrides.searchFacts ?? SAMPLE_FACTS,
          }),
        );
        return;
      }

      // GET /episodes/:group_id  (bare JSON array)
      if (req.method === "GET" && url.pathname.startsWith("/episodes/")) {
        lastRequest["/episodes"] = {
          group_id: url.pathname.split("/")[2],
          last_n: url.searchParams.get("last_n"),
        };
        const epStatus = mockOverrides.episodesStatus ?? 200;
        if (epStatus !== 200) {
          res.writeHead(epStatus);
          res.end(JSON.stringify({ detail: "episodes error" }));
          return;
        }
        res.writeHead(200, { "Content-Type": "application/json" });
        if (mockOverrides.episodesRawBody !== undefined) {
          res.end(mockOverrides.episodesRawBody);
          return;
        }
        // Honour last_n like the real server: never return more than asked for.
        const all = mockOverrides.episodes ?? SAMPLE_EPISODES;
        const lastN = Number(url.searchParams.get("last_n"));
        res.end(JSON.stringify(Number.isInteger(lastN) && lastN >= 0 ? all.slice(0, lastN) : all));
        return;
      }

      // DELETE /entity-edge/:uuid
      if (req.method === "DELETE" && url.pathname.startsWith("/entity-edge/")) {
        const uuid = url.pathname.split("/")[2];
        lastRequest["/entity-edge"] = { uuid };
        const delStatus = mockOverrides.deleteStatus ?? 200;
        res.writeHead(delStatus, { "Content-Type": "application/json" });
        res.end(JSON.stringify(delStatus === 200 ? { status: "ok" } : { detail: "delete error" }));
        return;
      }

      // DELETE /episode/:uuid
      if (req.method === "DELETE" && url.pathname.startsWith("/episode/")) {
        const uuid = url.pathname.split("/")[2];
        lastRequest["/episode"] = { uuid };
        const delStatus = mockOverrides.deleteStatus ?? 200;
        res.writeHead(delStatus, { "Content-Type": "application/json" });
        res.end(JSON.stringify(delStatus === 200 ? { status: "ok" } : { detail: "delete error" }));
        return;
      }

      res.writeHead(404);
      res.end(JSON.stringify({ detail: "Not Found" }));
    });

    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      port = typeof addr === "object" && addr !== null ? addr.port : 0;
      resolve();
    });
  });
}

export function stopMockServer(): Promise<void> {
  return new Promise((resolve) => {
    server.close(() => resolve());
    // Drop sockets parked by `hangPaths` so close() can finish.
    server.closeAllConnections();
  });
}

// ============================================================================
// Mock Context Factories
// ============================================================================

export function createMockHookCtx(overrides: Record<string, unknown> = {}) {
  return {
    sessionKey: "test-session-key",
    sessionId: "test-session-id",
    agentId: "test-agent",
    messageProvider: "test-channel",
    ...overrides,
  };
}

// ============================================================================
// Mock OpenClaw Plugin API
// ============================================================================

export type RegisteredTool = { tool: any; opts: any };
export type RegisteredHooks = Record<string, ((...args: any[]) => any)[]>;

const DEFAULT_TOOL_CTX = {
  sessionKey: "test-session-key",
  messageChannel: "test-channel",
  agentId: "test-agent",
};

export function createMockApi(configOverrides: Record<string, unknown> = {}) {
  const tools: RegisteredTool[] = [];
  const hooks: RegisteredHooks = {};
  const clis: { reg: any; opts: any }[] = [];
  const services: any[] = [];
  const commands: any[] = [];
  const contextEngines: { id: string; factory: () => any }[] = [];

  const api = {
    id: "graphiti",
    name: "Graphiti Knowledge Graph",
    source: "test",
    config: {},
    pluginConfig: {
      url: `http://127.0.0.1:${port}`,
      groupId: "test-group",
      ...configOverrides,
    },
    runtime: { tools: { registerMemoryCli: vi.fn() } },
    logger: {
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
      debug: vi.fn(),
    },
    registerTool: vi.fn((toolOrFactory: any, opts: any) => {
      if (typeof toolOrFactory === "function") {
        tools.push({ tool: toolOrFactory(DEFAULT_TOOL_CTX), opts });
      } else {
        tools.push({ tool: toolOrFactory, opts });
      }
    }),
    registerCli: vi.fn((reg: any, opts: any) => clis.push({ reg, opts })),
    registerService: vi.fn((svc: any) => services.push(svc)),
    registerCommand: vi.fn((cmd: any) => commands.push(cmd)),
    on: vi.fn((name: string, handler: any) => {
      (hooks[name] ??= []).push(handler);
    }),
    resolvePath: (p: string) => p,
  };

  return { api, tools, hooks, clis, services, commands, contextEngines };
}

/**
 * Create a mock API that supports registerContextEngine.
 * Use this to test the ContextEngine code path.
 */
export function createMockApiWithEngineSupport(configOverrides: Record<string, unknown> = {}) {
  const result = createMockApi(configOverrides);
  const { contextEngines } = result;

  (result.api as any).registerContextEngine = vi.fn((id: string, factory: () => any) => {
    contextEngines.push({ id, factory });
  });

  return result;
}

// ============================================================================
// CLI helpers
// ============================================================================

/**
 * Run the plugin's `graphiti` CLI registration against a mock Commander chain
 * and return every subcommand's action handler, keyed by subcommand name.
 */
export function captureCliActions(
  clis: { reg: any; opts: any }[],
): Record<string, (...args: any[]) => Promise<void>> {
  const graphitiCli = clis.find((c) => c.opts.commands.includes("graphiti"));
  if (!graphitiCli) throw new Error("graphiti CLI not registered");

  const actions: Record<string, (...args: any[]) => Promise<void>> = {};
  const mockCmd: any = {
    description: () => mockCmd,
    action: () => mockCmd,
    command: (name: string) => {
      const sub: any = {
        description: () => sub,
        argument: () => sub,
        option: () => sub,
        action: (fn: any) => { actions[name] = fn; return sub; },
      };
      return sub;
    },
    outputHelp: () => {},
  };
  graphitiCli.reg({ program: { command: () => mockCmd } });
  return actions;
}

/**
 * Run a CLI action with console.log/console.error captured. `process.exitCode`
 * is reported and always restored, even when the action throws.
 */
export async function runCli(
  fn: () => Promise<void>,
): Promise<{ logs: string[]; errors: string[]; exitCode: typeof process.exitCode }> {
  const logs: string[] = [];
  const errors: string[] = [];
  const origLog = console.log;
  const origError = console.error;
  const origExitCode = process.exitCode;
  console.log = (...args: any[]) => logs.push(args.join(" "));
  console.error = (...args: any[]) => errors.push(args.join(" "));
  try {
    process.exitCode = undefined;
    await fn();
    return { logs, errors, exitCode: process.exitCode };
  } finally {
    console.log = origLog;
    console.error = origError;
    process.exitCode = origExitCode;
  }
}
