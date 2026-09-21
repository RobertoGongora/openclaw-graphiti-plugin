/**
 * Plugin shape and registration tests.
 */

import { describe, test, expect, vi, beforeAll, afterAll } from "vitest";
import {
  startMockServer,
  stopMockServer,
  createMockApi,
  createMockApiWithEngineSupport,
} from "./helpers.js";

// ============================================================================
// Plugin Shape
// ============================================================================

describe("plugin shape", () => {
  test("exports correct metadata", async () => {
    const { default: plugin } = await import("../index.js");

    expect(plugin.id).toBe("graphiti");
    expect(plugin.name).toBe("Graphiti Knowledge Graph");
    expect(plugin.description).toContain("knowledge graph");
    expect(plugin.register).toBeInstanceOf(Function);
  });

  test("has kind: context-engine", async () => {
    const { default: plugin } = await import("../index.js");
    expect(plugin.kind).toBe("context-engine");
  });
});

// ============================================================================
// Manifest & Packaging
// ============================================================================

describe("manifest and packaging contract", () => {
  test("manifest declares skills", async () => {
    const fs = await import("node:fs");
    const manifest = JSON.parse(
      fs.readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf-8"),
    );
    expect(manifest.skills).toEqual(["./skills"]);
  });

  test("SKILL.md exists at the declared path", async () => {
    const fs = await import("node:fs");
    const skillPath = new URL("../skills/graphiti/SKILL.md", import.meta.url);
    expect(fs.existsSync(skillPath)).toBe(true);
  });

  test("package.json ships the plugin readme, not the repository README", async () => {
    const fs = await import("node:fs");
    const pkg = JSON.parse(
      fs.readFileSync(new URL("../package.json", import.meta.url), "utf-8"),
    );
    expect(pkg.files).toContain("README.plugin.md");
    expect(pkg.files).toContain("skills");
    // The repository README.md documents the Python service.
    expect(pkg.files).not.toContain("README.md");
    // npm force-includes a root README.md, so prepack swaps the plugin readme in
    // and postpack restores the repository one.
    expect(pkg.scripts.prepack).toContain("README.plugin.md");
    expect(pkg.scripts.postpack).toContain("README.md");
  });

  test("the shipped readme documents the plugin and marks it legacy", async () => {
    const fs = await import("node:fs");
    const readme = fs.readFileSync(new URL("../README.plugin.md", import.meta.url), "utf-8");
    expect(readme).toContain("graphiti_search");
    expect(readme).toContain("openclaw plugins install");
    expect(readme).toMatch(/legacy/i);
    expect(readme).toContain("docs/legacy-openclaw.md");
  });

  test("every shipped source file exists", async () => {
    const fs = await import("node:fs");
    const pkg = JSON.parse(
      fs.readFileSync(new URL("../package.json", import.meta.url), "utf-8"),
    );
    for (const f of pkg.files) {
      expect(fs.existsSync(new URL(`../${f}`, import.meta.url)), f).toBe(true);
    }
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

    // 4 tools: graphiti_search, graphiti_ingest, graphiti_forget, graphiti_episodes
    expect(tools).toHaveLength(4);
    expect(tools.map((t) => t.opts.name)).toEqual(
      expect.arrayContaining(["graphiti_search", "graphiti_ingest", "graphiti_forget", "graphiti_episodes"]),
    );

    // 4 hooks by default (autoRecall=false, autoCapture=true, autoIndex=true):
    // before_compaction, before_reset, session_start (always), after_tool_call
    expect(Object.keys(hooks)).toEqual(
      expect.arrayContaining([
        "before_compaction",
        "before_reset",
        "session_start",
        "after_tool_call",
      ]),
    );
    expect(Object.keys(hooks)).not.toContain("before_agent_start");

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

  test("registers only session_start when autoRecall, autoCapture, and autoIndex are false", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, hooks } = createMockApi({
      autoRecall: false,
      autoCapture: false,
      autoIndex: false,
    });

    plugin.register(api as any);

    expect(Object.keys(hooks)).toEqual(["session_start"]);
  });

  test("registers recall, index, and session_start hooks when autoCapture is false", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, hooks } = createMockApi({
      autoRecall: true,
      autoCapture: false,
    });

    plugin.register(api as any);

    expect(Object.keys(hooks)).toEqual(
      expect.arrayContaining(["before_agent_start", "session_start", "after_tool_call"]),
    );
    expect(Object.keys(hooks)).toHaveLength(3);
  });

  test("graphiti CLI default action outputs help instead of erroring", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, clis } = createMockApi();

    plugin.register(api as any);

    const graphitiCli = clis.find((c) =>
      c.opts.commands.includes("graphiti"),
    );
    expect(graphitiCli).toBeDefined();

    // Build a minimal mock Commander chain to capture the default action
    let defaultAction: (() => void) | undefined;
    const mockCmd = {
      description: vi.fn().mockReturnThis(),
      action: vi.fn((fn: () => void) => {
        // First .action() call is the default handler on the parent command
        if (!defaultAction) defaultAction = fn;
        return mockCmd;
      }),
      command: vi.fn().mockReturnValue({
        description: vi.fn().mockReturnThis(),
        argument: vi.fn().mockReturnThis(),
        option: vi.fn().mockReturnThis(),
        action: vi.fn().mockReturnThis(),
      }),
      outputHelp: vi.fn(),
    };
    const mockProgram = {
      command: vi.fn().mockReturnValue(mockCmd),
    };

    graphitiCli!.reg({ program: mockProgram });

    // Default action should have been registered
    expect(defaultAction).toBeInstanceOf(Function);

    // Invoking it should call outputHelp (exit 0) rather than throwing or exiting 1
    defaultAction!();
    expect(mockCmd.outputHelp).toHaveBeenCalled();
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

  // ========================================================================
  // ContextEngine code path
  // ========================================================================

  test("registers context engine when api.registerContextEngine exists", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, contextEngines } = createMockApiWithEngineSupport();

    plugin.register(api as any);

    expect((api as any).registerContextEngine).toHaveBeenCalledWith(
      "graphiti",
      expect.any(Function),
    );
    expect(contextEngines).toHaveLength(1);
    expect(contextEngines[0].id).toBe("graphiti");

    // Factory should produce a GraphitiContextEngine
    const engine = contextEngines[0].factory();
    expect(engine.info.id).toBe("graphiti");
    expect(engine.info.ownsCompaction).toBe(false);
  });

  test("the engine receives the normalised config, not the raw plugin config", async () => {
    const { default: plugin } = await import("../index.js");
    // Raw values that only normalisation gets right: a truthy non-boolean
    // autoRecall must NOT enable recall (hooks mode requires `=== true`).
    const { api, contextEngines } = createMockApiWithEngineSupport({ autoRecall: "true" });
    plugin.register(api as any);

    const engine = contextEngines[0].factory();
    expect((engine as any).cfg).toMatchObject({
      autoRecall: false,
      autoCapture: true,
      recallMaxFacts: 10,
      autoIndexExtensions: [".md", ".txt"],
    });

    await engine.bootstrap({ sessionId: "s-norm" });
    const result = await engine.assemble({ sessionId: "s-norm", messages: [{ role: "user", content: "Hello" }] });
    expect(result.systemPromptAddition).toBeUndefined();
  });

  test("a non-array autoIndexExtensions does not crash registration", async () => {
    const { default: plugin, normalizeIndexExtensions } = await import("../index.js");
    for (const bad of [".md, .txt", 42, { a: 1 }, [".md", 7, null, " TXT "]]) {
      const { api } = createMockApi({ autoIndexExtensions: bad });
      expect(() => plugin.register(api as any)).not.toThrow();
      expect(api.logger.warn).toHaveBeenCalledWith(expect.stringContaining("autoIndexExtensions"));
    }
    expect(normalizeIndexExtensions(".md, txt")).toEqual({ extensions: [".md", ".txt"], coerced: true });
    expect(normalizeIndexExtensions([".md", 7, " TXT "])).toEqual({ extensions: [".md", ".txt"], coerced: true });
    expect(normalizeIndexExtensions(["MD", ".org"])).toEqual({ extensions: [".md", ".org"], coerced: false });
    expect(normalizeIndexExtensions(undefined)).toEqual({ extensions: [".md", ".txt"], coerced: false });
    expect(normalizeIndexExtensions(42)).toEqual({ extensions: [".md", ".txt"], coerced: true });
  });

  test("skips recall/capture hooks when context engine is registered", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, hooks } = createMockApiWithEngineSupport({ autoRecall: true, autoCapture: true });

    plugin.register(api as any);

    // Should NOT have recall/capture hooks
    expect(hooks["before_agent_start"]).toBeUndefined();
    expect(hooks["before_compaction"]).toBeUndefined();
    expect(hooks["before_reset"]).toBeUndefined();

    // Should still have session_start and after_tool_call
    expect(hooks["session_start"]).toBeDefined();
    expect(hooks["after_tool_call"]).toBeDefined();
  });

  test("falls back to hooks when api.registerContextEngine does not exist", async () => {
    const { default: plugin } = await import("../index.js");
    const { api, hooks } = createMockApi({ autoRecall: true, autoCapture: true });

    plugin.register(api as any);

    // Should have all hooks (no registerContextEngine on api)
    expect(hooks["before_agent_start"]).toBeDefined();
    expect(hooks["before_compaction"]).toBeDefined();
    expect(hooks["before_reset"]).toBeDefined();
    expect(hooks["session_start"]).toBeDefined();
    expect(hooks["after_tool_call"]).toBeDefined();
  });
});
