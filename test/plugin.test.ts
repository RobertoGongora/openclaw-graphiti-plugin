/**
 * Plugin shape and registration tests.
 */

import { describe, test, expect, beforeAll, afterAll } from "vitest";
import {
  startMockServer,
  stopMockServer,
  createMockApi,
} from "./helpers.js";

// ============================================================================
// Plugin Shape
// ============================================================================

describe("plugin shape", () => {
  test("exports correct metadata", async () => {
    const { default: plugin } = await import("../index.js");

    expect(plugin.id).toBe("graphiti");
    expect(plugin.name).toBe("Graphiti Knowledge Graph");
    expect(plugin.kind).toBe("memory");
    expect(plugin.description).toContain("knowledge graph");
    expect(plugin.register).toBeInstanceOf(Function);
  });
});

// ============================================================================
// Registration
// ============================================================================

describe("registration", () => {
  beforeAll(startMockServer);
  afterAll(stopMockServer);

  test("registers all components with defaults", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, tools, hooks, clis, services, commands } = createMockApi();

    plugin.register(api as any);

    // 2 tools: graphiti_search, graphiti_ingest
    expect(tools).toHaveLength(2);
    expect(tools.map((t) => t.opts.name)).toEqual(
      expect.arrayContaining(["graphiti_search", "graphiti_ingest"]),
    );

    // 3 hooks: before_agent_start, before_compaction, before_reset
    expect(Object.keys(hooks)).toEqual(
      expect.arrayContaining([
        "before_agent_start",
        "before_compaction",
        "before_reset",
      ]),
    );

    // 2 CLI registrations: graphiti + memory (bridge)
    expect(clis).toHaveLength(2);
    expect(clis.map((c) => c.opts.commands)).toEqual(
      expect.arrayContaining([["graphiti"], ["memory"]]),
    );

    // 1 slash command
    expect(commands).toHaveLength(1);
    expect(commands[0].name).toBe("graphiti");

    // 1 service
    expect(services).toHaveLength(1);
    expect(services[0].id).toBe("graphiti");
  });

  test("skips hooks when autoRecall and autoCapture are false", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, hooks } = createMockApi({
      autoRecall: false,
      autoCapture: false,
    });

    plugin.register(api as any);

    expect(Object.keys(hooks)).toHaveLength(0);
  });

  test("registers only recall hook when autoCapture is false", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, hooks } = createMockApi({
      autoRecall: true,
      autoCapture: false,
    });

    plugin.register(api as any);

    expect(Object.keys(hooks)).toEqual(["before_agent_start"]);
  });

  test("registers memory CLI bridge via runtime", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, clis } = createMockApi();

    plugin.register(api as any);

    const memoryCli = clis.find((c) =>
      c.opts.commands.includes("memory"),
    );
    expect(memoryCli).toBeDefined();

    // Invoke the registrar to verify it calls runtime.tools.registerMemoryCli
    const mockProgram = {};
    memoryCli!.reg({ program: mockProgram });
    expect(api.runtime.tools.registerMemoryCli).toHaveBeenCalledWith(
      mockProgram,
    );
  });
});
