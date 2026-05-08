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
  ingestDelayMs?: number;
  searchStatus?: number;
  searchErrorBody?: string;
  episodes?: any[];
  episodesStatus?: number;
};

let server: http.Server;
let port: number;

export let mockOverrides: MockOverrides = {};
export const lastRequest: Record<string, unknown> = {};
export const requestBodies: Record<string, unknown[]> = {};
/** Headers from the most recent request to each path (lowercase keys). */
export const lastHeaders: Record<string, Record<string, string>> = {};

export function getMockPort(): number {
  return port;
}

export function resetMockState(): void {
  mockOverrides = {};
  for (const key of Object.keys(lastRequest)) delete lastRequest[key];
  for (const key of Object.keys(requestBodies)) delete requestBodies[key];
  for (const key of Object.keys(lastHeaders)) delete lastHeaders[key];
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
        (requestBodies["/messages"] ??= []).push(parsed);
        if (mockOverrides.ingestDelayMs) {
          await new Promise((resolve) => setTimeout(resolve, mockOverrides.ingestDelayMs));
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
        res.end(JSON.stringify(mockOverrides.episodes ?? SAMPLE_EPISODES));
        return;
      }

      // DELETE /entity-edge/:uuid
      if (req.method === "DELETE" && url.pathname.startsWith("/entity-edge/")) {
        const uuid = url.pathname.split("/")[2];
        lastRequest["/entity-edge"] = { uuid };
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: "ok" }));
        return;
      }

      // DELETE /episode/:uuid
      if (req.method === "DELETE" && url.pathname.startsWith("/episode/")) {
        const uuid = url.pathname.split("/")[2];
        lastRequest["/episode"] = { uuid };
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: "ok" }));
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
