/**
 * GraphitiClient tests against a mock HTTP server.
 *
 * Validates request payloads and response parsing match the real
 * Graphiti FastAPI contract (status codes, body shapes, etc.).
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { GraphitiClient } from "../client.js";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  getMockPort,
  mockOverrides,
  lastRequest,
} from "./helpers.js";

describe("GraphitiClient", () => {
  beforeAll(startMockServer);
  afterAll(stopMockServer);
  beforeEach(resetMockState);

  function client(url?: string) {
    return new GraphitiClient(
      url ?? `http://127.0.0.1:${getMockPort()}`,
      "test-group",
    );
  }

  // -- healthy --

  test("healthy() returns true when server responds 200", async () => {
    expect(await client().healthy()).toBe(true);
  });

  test("healthy() returns false when server responds 503", async () => {
    mockOverrides.healthy = false;
    expect(await client().healthy()).toBe(false);
  });

  test("healthy() returns false when server is unreachable", async () => {
    expect(await client("http://127.0.0.1:1").healthy()).toBe(false);
  });

  // -- search --

  test("search() sends correct request and parses facts", async () => {
    const facts = await client().search("dark mode", 5);

    expect(lastRequest["/search"]).toEqual({
      query: "dark mode",
      group_ids: ["test-group"],
      max_facts: 5,
    });

    expect(facts).toHaveLength(2);
    expect(facts[0].uuid).toBe("fact-001");
    expect(facts[0].name).toBe("WORKS_AT");
    expect(facts[0].fact).toBe("Alice works at Acme Corp");
  });

  test("search() returns empty array when no facts", async () => {
    mockOverrides.searchFacts = [];
    const facts = await client().search("nonexistent");
    expect(facts).toEqual([]);
  });

  test("search() throws on server error", async () => {
    mockOverrides.searchStatus = 500;
    await expect(client().search("test")).rejects.toThrow(/returned 500/);
  });

  // -- ingest --

  test("ingest() sends correct request and handles 202", async () => {
    const result = await client().ingest([
      {
        content: "User prefers dark mode",
        role_type: "user",
        role: "conversation",
        name: "test-episode",
        timestamp: "2024-01-15T10:30:00+00:00",
        source_description: "test",
      },
    ]);

    expect(lastRequest["/messages"]).toEqual({
      group_id: "test-group",
      messages: [
        {
          content: "User prefers dark mode",
          role_type: "user",
          role: "conversation",
          name: "test-episode",
          timestamp: "2024-01-15T10:30:00+00:00",
          source_description: "test",
        },
      ],
    });

    expect(result.success).toBe(true);
    expect(result.message).toBe("Messages added to processing queue");
  });

  // -- getMemory --

  test("getMemory() sends correct request", async () => {
    const facts = await client().getMemory(
      [{ content: "Hello", role_type: "user", role: "user" }],
      5,
    );

    expect(lastRequest["/get-memory"]).toEqual({
      group_id: "test-group",
      center_node_uuid: null,
      messages: [{ content: "Hello", role_type: "user", role: "user" }],
      max_facts: 5,
    });
    expect(facts).toHaveLength(2);
  });

  // -- episodes --

  test("episodes() sends GET with query param and returns bare array", async () => {
    const eps = await client().episodes(3);

    expect(lastRequest["/episodes"]).toEqual({
      group_id: "test-group",
      last_n: "3",
    });
    expect(eps).toHaveLength(1);
    expect(eps[0].uuid).toBe("ep-001");
    expect(eps[0].content).toBe("user(user): Hello");
  });

  test("episodes() returns empty array on server error", async () => {
    const eps = await client("http://127.0.0.1:1").episodes(10);
    expect(eps).toEqual([]);
  });
});
