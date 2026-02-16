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
});
