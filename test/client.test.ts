/**
 * GraphitiClient tests against a mock HTTP server.
 *
 * Validates request payloads and response parsing match the real
 * Graphiti FastAPI contract (status codes, body shapes, etc.).
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { GraphitiClient, EPISODE_COUNT_CAP, formatEpisodeCount } from "../client.js";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  getMockPort,
  mockOverrides,
  lastRequest,
  lastHeaders,
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

  test("episodes() returns empty array when server is unreachable", async () => {
    const eps = await client("http://127.0.0.1:1").episodes(10);
    expect(eps).toEqual([]);
  });

  test("episodes() swallows HTTP 500 and returns [] (documented contract)", async () => {
    mockOverrides.episodesStatus = 500;
    await expect(client().episodes(10)).resolves.toEqual([]);
  });

  test("episodes() returns [] for a non-array body", async () => {
    mockOverrides.episodesRawBody = JSON.stringify({ detail: "not a list" });
    expect(await client().episodes(10)).toEqual([]);
    mockOverrides.episodesRawBody = "null";
    expect(await client().episodes(10)).toEqual([]);
  });

  test("mock server honours last_n", async () => {
    mockOverrides.episodes = Array.from({ length: 8 }, (_, i) => ({ uuid: `ep-${i}` }));
    expect(await client().episodes(3)).toHaveLength(3);
    expect(await client().episodes(50)).toHaveLength(8);
  });

  // -- null / odd bodies --

  test("search() returns [] for a bare null body", async () => {
    mockOverrides.searchRawBody = "null";
    expect(await client().search("anything")).toEqual([]);
  });

  test("search() returns [] when facts is not an array", async () => {
    mockOverrides.searchRawBody = JSON.stringify({ facts: "nope" });
    expect(await client().search("anything")).toEqual([]);
  });

  // -- delete --

  test("deleteEdge() / deleteEpisode() throw on non-2xx", async () => {
    mockOverrides.deleteStatus = 404;
    await expect(client().deleteEdge("abc")).rejects.toThrow(/DELETE \/entity-edge\/abc returned 404/);
    await expect(client().deleteEpisode("abc")).rejects.toThrow(/DELETE \/episode\/abc returned 404/);
  });

  // -- abort timeouts --

  function fastClient() {
    return new GraphitiClient(
      `http://127.0.0.1:${getMockPort()}`, "test-group", undefined, undefined, undefined,
      { requestMs: 50, healthMs: 50, episodesMs: 50 },
    );
  }

  test("search() aborts when the server never answers", async () => {
    mockOverrides.hangPaths = ["/search"];
    await expect(fastClient().search("x")).rejects.toThrow(/abort/i);
  });

  test("search() aborts when the response BODY stalls after the headers", async () => {
    mockOverrides.stallBodyPaths = ["/search"];
    await expect(fastClient().search("x")).rejects.toThrow(/abort/i);
  });

  test("deleteEdge() aborts when the server never answers", async () => {
    mockOverrides.hangPaths = ["/entity-edge"];
    await expect(fastClient().deleteEdge("abc")).rejects.toThrow(/abort/i);
  });

  test("healthy() returns false on timeout", async () => {
    mockOverrides.hangPaths = ["/healthcheck"];
    expect(await fastClient().healthy()).toBe(false);
  });

  test("episodes() returns [] on timeout", async () => {
    mockOverrides.hangPaths = ["/episodes"];
    expect(await fastClient().episodes(5)).toEqual([]);
  });

  // -- episodeCount --

  test("episodeCount() returns count and latest timestamp", async () => {
    const stats = await client().episodeCount();
    expect(stats.count).toBe(1);
    expect(stats.latestAt).toBe("2024-01-15T10:30:00+00:00");
  });

  test("episodeCount() asks for at most EPISODE_COUNT_CAP episodes", async () => {
    await client().episodeCount();
    expect(lastRequest["/episodes"]).toEqual({ group_id: "test-group", last_n: String(EPISODE_COUNT_CAP) });
    expect(EPISODE_COUNT_CAP).toBeLessThanOrEqual(1000);
  });

  test("episodeCount() caps the count and picks the newest created_at in any order", async () => {
    mockOverrides.episodes = [
      { uuid: "a", created_at: "2024-01-01T00:00:00+00:00" },
      { uuid: "b", created_at: "2024-03-01T00:00:00+00:00" },
      { uuid: "c", created_at: "2024-02-01T00:00:00+00:00" },
      { uuid: "d" },
    ];
    expect(await client().episodeCount()).toEqual({ count: 4, latestAt: "2024-03-01T00:00:00+00:00" });
    expect((await client().episodeCount(2)).count).toBe(2);
  });

  test("formatEpisodeCount() marks a capped count as a lower bound", () => {
    expect(formatEpisodeCount(EPISODE_COUNT_CAP - 1)).toBe(String(EPISODE_COUNT_CAP - 1));
    expect(formatEpisodeCount(EPISODE_COUNT_CAP)).toBe(`${EPISODE_COUNT_CAP}+`);
  });

  test("episodeCount() returns zeros when server is unreachable", async () => {
    const stats = await client("http://127.0.0.1:1").episodeCount();
    expect(stats.count).toBe(0);
    expect(stats.latestAt).toBeNull();
  });

  // -- apiKey auth header --

  function clientWithKey(apiKey?: string) {
    return new GraphitiClient(
      `http://127.0.0.1:${getMockPort()}`,
      "test-group",
      undefined,
      apiKey,
    );
  }

  test("sends Authorization header on search when apiKey is set", async () => {
    await clientWithKey("sk-test-123").search("test");
    expect(lastHeaders["/search"]?.["authorization"]).toBe("Bearer sk-test-123");
  });

  test("sends Authorization header on healthcheck when apiKey is set", async () => {
    await clientWithKey("sk-test-123").healthy();
    expect(lastHeaders["/healthcheck"]?.["authorization"]).toBe("Bearer sk-test-123");
  });

  test("sends Authorization header on episodes when apiKey is set", async () => {
    const c = clientWithKey("sk-test-123");
    await c.episodes(5);
    // episodes path includes the group_id
    const epHeaders = lastHeaders[`/episodes/test-group`];
    expect(epHeaders?.["authorization"]).toBe("Bearer sk-test-123");
  });

  test("sends Authorization header on ingest when apiKey is set", async () => {
    await clientWithKey("sk-test-123").ingest([
      { content: "test", role_type: "user", role: "user" },
    ]);
    expect(lastHeaders["/messages"]?.["authorization"]).toBe("Bearer sk-test-123");
  });

  test("does NOT send Authorization header when apiKey is omitted", async () => {
    await clientWithKey().search("test");
    expect(lastHeaders["/search"]?.["authorization"]).toBeUndefined();
  });

  // -- multi-group search --

  test("search() sends custom group_ids when provided", async () => {
    await client().search("test", 5, ["group-a", "group-b"]);
    expect(lastRequest["/search"]).toEqual({
      query: "test",
      group_ids: ["group-a", "group-b"],
      max_facts: 5,
    });
  });

  test("search() falls back to default groupId when groupIds omitted", async () => {
    await client().search("test", 5);
    expect((lastRequest["/search"] as any).group_ids).toEqual(["test-group"]);
  });
});
