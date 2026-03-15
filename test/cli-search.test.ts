/**
 * CLI search command tests.
 *
 * Covers happy path and error handling (server error, try/catch).
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from "vitest";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  createMockApi,
  mockOverrides,
} from "./helpers.js";

describe("CLI search command", () => {
  beforeAll(startMockServer);
  afterAll(stopMockServer);
  beforeEach(resetMockState);

  /**
   * Register the plugin and extract the search CLI action handler.
   */
  async function getSearchAction() {
    const { default: plugin } = await import("../index.js");
    const { api, clis } = createMockApi();
    plugin.register(api as any);

    const graphitiCli = clis.find((c) => c.opts.commands.includes("graphiti"));
    expect(graphitiCli).toBeDefined();

    let searchAction: ((query: string, opts: any) => Promise<void>) | undefined;

    const mockCmd = {
      description: () => mockCmd,
      action: (fn: any) => mockCmd,
      command: (name: string) => {
        const sub: any = {
          description: () => sub,
          argument: () => sub,
          option: () => sub,
          action: (fn: any) => {
            if (name === "search") searchAction = fn;
            return sub;
          },
        };
        return sub;
      },
      outputHelp: () => {},
    };
    const mockProgram = { command: () => mockCmd };

    graphitiCli!.reg({ program: mockProgram });
    expect(searchAction).toBeDefined();
    return searchAction!;
  }

  test("happy path prints formatted facts", async () => {
    const action = await getSearchAction();

    const logs: string[] = [];
    const origLog = console.log;
    console.log = (...args: any[]) => logs.push(args.join(" "));
    try {
      await action("Alice", { limit: "10" });
    } finally {
      console.log = origLog;
    }

    expect(logs.some((l) => l.includes("WORKS_AT"))).toBe(true);
    expect(logs.some((l) => l.includes("Alice works at Acme Corp"))).toBe(true);
    expect(logs.some((l) => l.includes("2024-01-15"))).toBe(true);
  });

  test("no results prints message", async () => {
    mockOverrides.searchFacts = [];
    const action = await getSearchAction();

    const logs: string[] = [];
    const origLog = console.log;
    console.log = (...args: any[]) => logs.push(args.join(" "));
    try {
      await action("nonexistent", { limit: "10" });
    } finally {
      console.log = origLog;
    }

    expect(logs.some((l) => l.includes("No facts found"))).toBe(true);
  });

  test("server error prints error and sets exitCode", async () => {
    mockOverrides.searchStatus = 500;
    const action = await getSearchAction();

    const errors: string[] = [];
    const origError = console.error;
    console.error = (...args: any[]) => errors.push(args.join(" "));
    const origExitCode = process.exitCode;
    try {
      await action("test query", { limit: "10" });
    } finally {
      console.error = origError;
    }

    expect(errors.some((e) => e.includes("Search failed"))).toBe(true);
    expect(process.exitCode).toBe(1);
    process.exitCode = origExitCode;
  });
});
