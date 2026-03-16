/**
 * Tool execution tests (graphiti_search, graphiti_ingest).
 *
 * Registers the plugin with a mock API, then invokes tool.execute()
 * directly and verifies output formatting + server payloads.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from "vitest";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  createMockApi,
  mockOverrides,
  lastRequest,
  SAMPLE_FACTS,
} from "./helpers.js";

describe("tool execution", () => {
  beforeAll(startMockServer);
  afterAll(stopMockServer);
  beforeEach(resetMockState);

  // -- graphiti_search --

  test("graphiti_search returns formatted facts", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_search")!.tool;
    const result = await tool.execute("call-1", { query: "Alice", limit: 10 });

    expect(result.content[0].text).toContain("Found 2 facts");
    expect(result.content[0].text).toContain("WORKS_AT");
    expect(result.content[0].text).toContain("Alice works at Acme Corp");
    expect(result.details.count).toBe(2);
    expect(result.details.facts).toHaveLength(2);
  });

  test("graphiti_search returns no-facts message on empty", async () => {
    mockOverrides.searchFacts = [];
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_search")!.tool;
    const result = await tool.execute("call-2", { query: "nothing" });

    expect(result.content[0].text).toContain("No relevant facts");
    expect(result.details.count).toBe(0);
  });

  test("graphiti_search returns error message on failure", async () => {
    mockOverrides.searchStatus = 500;
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_search")!.tool;
    const result = await tool.execute("call-3", { query: "test" });

    expect(result.content[0].text).toContain("Graphiti search failed");
    expect(result.content[0].text).toContain("500");
  });

  // -- graphiti_ingest --

  test("graphiti_ingest sends to server and returns confirmation", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_ingest")!.tool;
    const result = await tool.execute("call-4", {
      content: "Important fact about the project architecture",
      name: "test-ep",
      source: "test",
    });

    expect(result.content[0].text).toContain("Ingested into knowledge graph");
    expect(result.content[0].text).toContain("Important fact");

    const req = lastRequest["/messages"] as any;
    expect(req.group_id).toBe("test-group");
    expect(req.messages).toHaveLength(1);
    expect(req.messages[0].content).toBe(
      "Important fact about the project architecture",
    );
    expect(req.messages[0].role_type).toBe("system");
    const prov = JSON.parse(req.messages[0].source_description);
    expect(prov.session_key).toBe("test-session-key");
    expect(prov.agent).toBe("test-agent");
    expect(prov.channel).toBe("test-channel");
  });

  test("graphiti_ingest provenance has event, source, ts, group_id", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_ingest")!.tool;
    await tool.execute("call-prov", {
      content: "Architecture uses event sourcing",
      source: "design-doc",
    });

    const req = lastRequest["/messages"] as any;
    const prov = JSON.parse(req.messages[0].source_description);
    expect(prov.event).toBe("manual");
    expect(prov.source).toBe("design-doc");
    expect(prov.ts).toBeDefined();
    expect(prov.group_id).toBe("test-group");
    expect(prov.plugin).toBe("openclaw-graphiti");
  });

  test("graphiti_ingest returns error message on failure", async () => {
    mockOverrides.ingestStatus = 500;
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_ingest")!.tool;
    const result = await tool.execute("call-5", { content: "test" });

    expect(result.content[0].text).toContain("Graphiti ingest failed");
  });

  // -- graphiti_forget --

  test("graphiti_forget deletes fact by UUID", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const validUuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890";
    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f1", { uuid: validUuid, type: "fact" });

    expect(result.content[0].text).toContain(`Deleted fact ${validUuid}`);
    expect(result.details.deleted).toBe(true);
    expect(lastRequest["/entity-edge"]).toEqual({ uuid: validUuid });
  });

  test("graphiti_forget deletes episode by UUID", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const validUuid = "b2c3d4e5-f6a7-8901-bcde-f12345678901";
    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f2", { uuid: validUuid, type: "episode" });

    expect(result.content[0].text).toContain(`Deleted episode ${validUuid}`);
    expect(result.details.deleted).toBe(true);
    expect(lastRequest["/episode"]).toEqual({ uuid: validUuid });
  });

  test("graphiti_forget auto-deletes single search match", async () => {
    mockOverrides.searchFacts = [SAMPLE_FACTS[0]];
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f3", { query: "Alice" });

    expect(result.details.deleted).toBe(true);
    expect(result.details.uuid).toBe("fact-001");
    expect(lastRequest["/entity-edge"]).toEqual({ uuid: "fact-001" });
  });

  test("graphiti_forget lists multiple matches without deleting", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f4", { query: "test" });

    expect(result.details.deleted).toBe(false);
    expect(result.details.reason).toBe("multiple_matches");
    expect(result.content[0].text).toContain("specify a UUID");
  });

  test("graphiti_forget rejects query mode with type=episode", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f-ep-query", { query: "some episode", type: "episode" });

    expect(result.details.deleted).toBe(false);
    expect(result.details.reason).toBe("episode_query_not_supported");
    expect(result.content[0].text).toContain("not supported");
    expect(result.content[0].text).toContain("UUID");
    // Should NOT have attempted a search
    expect(lastRequest["/search"]).toBeUndefined();
  });

  test("graphiti_forget error response includes details object", async () => {
    mockOverrides.searchStatus = 500;
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f-err", { query: "test" });

    expect(result.content[0].text).toContain("Graphiti forget failed");
    expect(result.details).toBeDefined();
    expect(result.details.deleted).toBe(false);
    expect(result.details.reason).toBe("error");
    expect(result.details.error).toBeDefined();
  });

  test("graphiti_forget returns error when neither query nor uuid provided", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f5", {});

    expect(result.content[0].text).toContain("provide either");
    expect(result.details).toBeDefined();
    expect(result.details.deleted).toBe(false);
    expect(result.details.reason).toBe("missing_params");
  });

  test("graphiti_forget rejects invalid UUID format", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f-bad-uuid", { uuid: "../admin", type: "fact" });

    expect(result.content[0].text).toContain("Invalid UUID format");
    expect(result.details.deleted).toBe(false);
    expect(result.details.reason).toBe("invalid_uuid");
    // Should NOT have sent a DELETE request
    expect(lastRequest["/entity-edge"]).toBeUndefined();
  });

  test("graphiti_forget accepts valid UUID format", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_forget")!.tool;
    const result = await tool.execute("call-f-good-uuid", { uuid: "a1b2c3d4-e5f6-7890-abcd-ef1234567890", type: "fact" });

    expect(result.details.deleted).toBe(true);
    expect(lastRequest["/entity-edge"]).toEqual({ uuid: "a1b2c3d4-e5f6-7890-abcd-ef1234567890" });
  });

  // -- graphiti_episodes --

  test("graphiti_episodes returns formatted list", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_episodes")!.tool;
    const result = await tool.execute("call-ep1", {});

    expect(result.content[0].text).toContain("1 episode(s)");
    expect(result.details.count).toBe(1);
  });

  test("graphiti_episodes error response includes details object", async () => {
    // Force the episodes endpoint to return 500 — the client returns []
    // for HTTP errors, so the tool returns "No episodes found" (the empty path).
    // This verifies the error path IS reachable by using a non-iterable value.
    // Set episodes to an object (not array) — client passes it through, then
    // eps.filter() throws because a plain object has no .filter method.
    mockOverrides.episodes = { broken: true } as any;

    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_episodes")!.tool;
    // Use sessionKey to trigger the filter path which calls .filter() on the object
    const result = await tool.execute("call-ep-err", { sessionKey: "trigger-error" });

    // The error should be caught and return a details object
    expect(result.content[0].text).toContain("Graphiti episodes failed");
    expect(result.details).toBeDefined();
    expect(result.details.count).toBe(0);
    expect(result.details.reason).toBe("error");
    expect(result.details.error).toBeDefined();
  });

  test("graphiti_episodes filters by sessionKey", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_episodes")!.tool;
    const result = await tool.execute("call-ep2", { sessionKey: "nonexistent" });

    expect(result.content[0].text).toContain("No episodes found");
    expect(result.details.count).toBe(0);
  });

  test("graphiti_episodes sessionKey compensation preserves matching episodes beyond initial limit", async () => {
    // Simulate: limit=2, but the matching episode is at position 4 (beyond limit).
    // The fetch-more heuristic (limit * 5 = 10) should fetch enough to find it.
    const targetSessionKey = "target-session-abc";
    mockOverrides.episodes = [
      {
        uuid: "ep-unrelated-1", name: "unrelated-1", created_at: "2024-01-15T10:30:00+00:00",
        source_description: JSON.stringify({ event: "after_turn", session_key: "other-session-1" }),
        content: "Unrelated episode 1",
      },
      {
        uuid: "ep-unrelated-2", name: "unrelated-2", created_at: "2024-01-15T10:31:00+00:00",
        source_description: JSON.stringify({ event: "after_turn", session_key: "other-session-2" }),
        content: "Unrelated episode 2",
      },
      {
        uuid: "ep-unrelated-3", name: "unrelated-3", created_at: "2024-01-15T10:32:00+00:00",
        source_description: JSON.stringify({ event: "after_turn", session_key: "other-session-3" }),
        content: "Unrelated episode 3",
      },
      {
        uuid: "ep-target-1", name: "target-episode", created_at: "2024-01-15T10:33:00+00:00",
        source_description: JSON.stringify({ event: "after_turn", session_key: targetSessionKey }),
        content: "This is the matching episode beyond initial limit",
      },
      {
        uuid: "ep-target-2", name: "target-episode-2", created_at: "2024-01-15T10:34:00+00:00",
        source_description: JSON.stringify({ event: "compact", session_key: targetSessionKey }),
        content: "Second matching episode",
      },
    ];

    const { default: plugin } = await import("../index.js");
    const { api, tools } = createMockApi();
    plugin.register(api as any);

    const tool = tools.find((t) => t.opts.name === "graphiti_episodes")!.tool;
    const result = await tool.execute("call-ep-compensation", { limit: 2, sessionKey: targetSessionKey });

    // Both matching episodes should be found (compensation fetched more than limit)
    expect(result.details.count).toBe(2);
    expect(result.content[0].text).toContain("2 episode(s)");
    expect(result.content[0].text).toContain("target-episode");

    // Verify the server was asked for more than limit (limit * 5 = 10)
    const episodesReq = lastRequest["/episodes"] as any;
    expect(Number(episodesReq.last_n)).toBe(10); // limit * 5
  });
});
