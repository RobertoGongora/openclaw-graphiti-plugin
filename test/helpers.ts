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
    source_description: "OpenClaw auto-capture",
    content: "user(user): Hello",
    valid_at: "2024-01-15T10:30:00+00:00",
    entity_edges: ["fact-001"],
  },
];

// ============================================================================
// Mock HTTP Server
// ============================================================================

export type MockOverrides = {
  healthy?: boolean;
  searchFacts?: GraphitiFact[];
  ingestStatus?: number;
  ingestBody?: Record<string, unknown>;
  searchStatus?: number;
};

let server: http.Server;
let port: number;

export let mockOverrides: MockOverrides = {};
export const lastRequest: Record<string, unknown> = {};

export function getMockPort(): number {
  return port;
}

export function resetMockState(): void {
  mockOverrides = {};
  for (const key of Object.keys(lastRequest)) delete lastRequest[key];
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (chunk: Buffer) => (data += chunk.toString()));
    req.on("end", () => resolve(data));
  });
}

export function startMockServer(): Promise<void> {
  return new Promise((resolve) => {
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url ?? "/", "http://localhost");

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
          res.end(JSON.stringify({ detail: "search error" }));
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
        lastRequest["/messages"] = JSON.parse(body);
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
            facts: mockOverrides.searchFacts ?? SAMPLE_FACTS,
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
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(SAMPLE_EPISODES));
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
// Mock OpenClaw Plugin API
// ============================================================================

export type RegisteredTool = { tool: any; opts: any };
export type RegisteredHooks = Record<string, ((...args: any[]) => any)[]>;

export function createMockApi(configOverrides: Record<string, unknown> = {}) {
  const tools: RegisteredTool[] = [];
  const hooks: RegisteredHooks = {};
  const clis: { reg: any; opts: any }[] = [];
  const services: any[] = [];
  const commands: any[] = [];

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
    registerTool: vi.fn((tool: any, opts: any) => tools.push({ tool, opts })),
    registerCli: vi.fn((reg: any, opts: any) => clis.push({ reg, opts })),
    registerService: vi.fn((svc: any) => services.push(svc)),
    registerCommand: vi.fn((cmd: any) => commands.push(cmd)),
    on: vi.fn((name: string, handler: any) => {
      (hooks[name] ??= []).push(handler);
    }),
    resolvePath: (p: string) => p,
  };

  return { api, tools, hooks, clis, services, commands };
}
