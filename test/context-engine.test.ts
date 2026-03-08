/**
 * Tests for GraphitiContextEngine.
 *
 * Instantiates the engine directly (not via plugin registration)
 * and verifies each method against the mock HTTP server.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from "vitest";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  getMockPort,
  mockOverrides,
  lastRequest,
} from "./helpers.js";
import { GraphitiClient } from "../client.js";
import { NOOP_LOG } from "../debug-log.js";
import { GraphitiContextEngine } from "../context-engine.js";
import { formatFactsAsContext } from "../shared.js";
import { createRequire } from "node:module";

const _require = createRequire(import.meta.url);
const PKG_VERSION: string = (_require("../package.json") as any).version;

function createEngine() {
  const port = getMockPort();
  const client = new GraphitiClient(`http://127.0.0.1:${port}`, "test-group", undefined, undefined, NOOP_LOG);
  return new GraphitiContextEngine(client, { recallMaxFacts: 10 }, "test-group", NOOP_LOG);
}

describe("GraphitiContextEngine", () => {
  beforeAll(startMockServer);
  afterAll(stopMockServer);
  beforeEach(resetMockState);

  // ========================================================================
  // info
  // ========================================================================

  describe("info", () => {
    test("has correct id and name", () => {
      const engine = createEngine();
      expect(engine.info.id).toBe("graphiti");
      expect(engine.info.name).toBe("Graphiti Knowledge Graph");
      expect(engine.info.version).toBe(PKG_VERSION);
      expect(engine.info.ownsCompaction).toBe(true);
    });
  });

  // ========================================================================
  // ingest
  // ========================================================================

  describe("ingest", () => {
    test("ingests user message", async () => {
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "user", content: "Tell me about the project architecture and design patterns" },
      });

      expect(result.ingested).toBe(true);
      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("user:");
      expect(req.messages[0].content).toContain("architecture");
    });

    test("ingests assistant message", async () => {
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "assistant", content: "The project uses a microservices architecture with Neo4j" },
      });

      expect(result.ingested).toBe(true);
      const req = lastRequest["/messages"] as any;
      expect(req.messages[0].content).toContain("assistant:");
    });

    test("skips heartbeat messages", async () => {
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "user", content: "This is a heartbeat check message" },
        isHeartbeat: true,
      });

      expect(result.ingested).toBe(false);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips short messages (< 20 chars)", async () => {
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "user", content: "hi" },
      });

      expect(result.ingested).toBe(false);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips system messages", async () => {
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "system", content: "You are a helpful assistant that answers questions thoroughly" },
      });

      expect(result.ingested).toBe(false);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips tool messages", async () => {
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "tool", content: "Tool result with some long content that passes length check" },
      });

      expect(result.ingested).toBe(false);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("handles content-block-array format", async () => {
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: {
          role: "user",
          content: [{ type: "text", text: "What about the project architecture and deployment?" }],
        },
      });

      expect(result.ingested).toBe(true);
      const req = lastRequest["/messages"] as any;
      expect(req.messages[0].content).toContain("architecture");
    });
  });

  // ========================================================================
  // ingestBatch
  // ========================================================================

  describe("ingestBatch", () => {
    test("batch ingests filtered messages", async () => {
      const engine = createEngine();
      const result = await engine.ingestBatch({
        sessionId: "sess-1",
        messages: [
          { role: "system", content: "You are a helpful assistant" },
          { role: "user", content: "What is the architecture of our system?" },
          { role: "assistant", content: "The system uses a microservices architecture with Neo4j." },
          { role: "tool", content: "tool output that should be skipped" },
        ],
      });

      expect(result.ingestedCount).toBe(2);
      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages).toHaveLength(1); // single episode
      expect(req.messages[0].content).toContain("architecture");
      // System and tool messages should be filtered out
      expect(req.messages[0].content).not.toContain("You are a helpful assistant");
      expect(req.messages[0].content).not.toContain("tool output");
    });

    test("skips heartbeat batches", async () => {
      const engine = createEngine();
      const result = await engine.ingestBatch({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "What is the architecture of our system?" },
        ],
        isHeartbeat: true,
      });

      expect(result.ingestedCount).toBe(0);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("returns correct ingestedCount", async () => {
      const engine = createEngine();
      const result = await engine.ingestBatch({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "First question about the project architecture" },
          { role: "assistant", content: "First answer about the system design patterns" },
          { role: "user", content: "Second question about deployment strategies" },
        ],
      });

      expect(result.ingestedCount).toBe(3);
    });
  });

  // ========================================================================
  // afterTurn
  // ========================================================================

  describe("afterTurn", () => {
    test("slices new messages using prePromptMessageCount", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "system", content: "System prompt" },
          { role: "user", content: "Old message before this turn was started" },
          { role: "user", content: "New question about the project architecture" },
          { role: "assistant", content: "New answer about the system design patterns" },
        ],
        prePromptMessageCount: 2,
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("architecture");
      expect(req.messages[0].content).toContain("design patterns");
      expect(req.messages[0].content).not.toContain("Old message");
    });

    test("ingests only new user/assistant messages", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "system", content: "System prompt" },
          { role: "user", content: "New question about architecture and design" },
          { role: "tool", content: "Tool result should be filtered out completely" },
          { role: "assistant", content: "Response about the architecture patterns used" },
        ],
        prePromptMessageCount: 1,
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("architecture");
      expect(req.messages[0].content).not.toContain("Tool result");
    });

    test("skips heartbeat turns", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "New question about architecture and design" },
        ],
        prePromptMessageCount: 0,
        isHeartbeat: true,
      });

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips when no new messages", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Only old messages before this turn" },
        ],
        prePromptMessageCount: 1,
      });

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips when new messages have no user/assistant content", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "system", content: "Old system prompt" },
          { role: "tool", content: "Only tool content in the new messages" },
        ],
        prePromptMessageCount: 1,
      });

      expect(lastRequest["/messages"]).toBeUndefined();
    });
  });

  // ========================================================================
  // assemble
  // ========================================================================

  describe("assemble", () => {
    test("calls /get-memory with converted messages", async () => {
      const engine = createEngine();
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about Alice at Acme Corp" },
          { role: "assistant", content: "Alice works at Acme as a senior engineer" },
        ],
      });

      const req = lastRequest["/get-memory"] as any;
      expect(req).toBeDefined();
      expect(req.group_id).toBe("test-group");
      expect(req.messages).toHaveLength(2);
      expect(req.max_facts).toBe(10);
    });

    test("returns systemPromptAddition with graphiti-context wrapper", async () => {
      const engine = createEngine();
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about Alice" },
        ],
      });

      expect(result.systemPromptAddition).toBeDefined();
      expect(result.systemPromptAddition).toContain("<graphiti-context>");
      expect(result.systemPromptAddition).toContain("</graphiti-context>");
      expect(result.systemPromptAddition).toContain("WORKS_AT");
      expect(result.systemPromptAddition).toContain("Alice works at Acme Corp");
    });

    test("returns pass-through when no facts", async () => {
      mockOverrides.searchFacts = [];
      const engine = createEngine();
      const messages = [{ role: "user", content: "Tell me about something unknown" }];
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages,
      });

      expect(result.messages).toBe(messages);
      expect(result.systemPromptAddition).toBeUndefined();
    });

    test("returns pass-through when server unhealthy", async () => {
      mockOverrides.healthy = false;
      const engine = createEngine();
      const messages = [{ role: "user", content: "Tell me about Alice" }];
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages,
      });

      expect(result.messages).toBe(messages);
      expect(result.systemPromptAddition).toBeUndefined();
    });

    test("includes estimatedTokens", async () => {
      const engine = createEngine();
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about Alice" },
        ],
      });

      expect(result.estimatedTokens).toBeDefined();
      expect(result.estimatedTokens).toBeGreaterThan(0);
    });

    test("messages are returned unchanged", async () => {
      const engine = createEngine();
      const messages = [
        { role: "user", content: "Tell me about Alice" },
        { role: "assistant", content: "Alice is a developer" },
      ];
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages,
      });

      expect(result.messages).toBe(messages);
    });
  });

  // ========================================================================
  // compact
  // ========================================================================

  describe("compact", () => {
    test("ingests messages and returns compacted: true", async () => {
      const engine = createEngine();
      const result = await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about the project architecture and design patterns" },
          { role: "assistant", content: "The project uses a modular architecture with clean separation of concerns" },
        ],
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(true);
      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("architecture");
    });

    test("falls back to bridge when server unhealthy", async () => {
      mockOverrides.healthy = false;
      const engine = createEngine();
      let bridgeCalled = false;
      const result = await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Some message content for the compaction test" },
        ],
        legacyParams: {
          bridge: { async compact() { bridgeCalled = true; } },
        },
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(true);
      expect(bridgeCalled).toBe(true);
    });

    test("returns not-compacted when unhealthy and no bridge", async () => {
      mockOverrides.healthy = false;
      const engine = createEngine();
      const result = await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Some message content for the compaction test" },
        ],
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(false);
      expect(result.reason).toBe("server-unhealthy");
    });

    test("returns compacted true when no extractable text", async () => {
      const engine = createEngine();
      const result = await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "system", content: "System prompt" },
          { role: "tool", content: "Tool output" },
        ],
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(true);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("returns not-compacted when no messages and no bridge", async () => {
      const engine = createEngine();
      const result = await engine.compact({
        sessionId: "sess-1",
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(false);
      expect(result.reason).toBe("no-messages");
    });

    test("delegates to legacy bridge when no messages but bridge available", async () => {
      const engine = createEngine();
      let bridgeCalled = false;
      const result = await engine.compact({
        sessionId: "sess-1",
        legacyParams: {
          bridge: { async compact() { bridgeCalled = true; } },
        },
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(true);
      expect(bridgeCalled).toBe(true);
    });
  });

  // ========================================================================
  // bootstrap
  // ========================================================================

  describe("bootstrap", () => {
    test("returns bootstrapped: true with episodeCount when server healthy", async () => {
      const engine = createEngine();
      const result = await engine.bootstrap({
        sessionId: "sess-1",
      });

      expect(result.bootstrapped).toBe(true);
      expect(result.episodeCount).toBeDefined();
      expect(typeof result.episodeCount).toBe("number");
    });

    test("returns bootstrapped: false when server unhealthy", async () => {
      mockOverrides.healthy = false;
      const engine = createEngine();
      const result = await engine.bootstrap({
        sessionId: "sess-1",
      });

      expect(result.bootstrapped).toBe(false);
      expect(result.reason).toBe("server-unhealthy");
    });
  });

  // ========================================================================
  // onSubagentEnded
  // ========================================================================

  describe("onSubagentEnded", () => {
    test("ingests summary when provided", async () => {
      const engine = createEngine();
      await engine.onSubagentEnded({
        childSessionKey: "child-1",
        reason: "completed",
        summary: "The subagent found that the database schema needs migration to support new features",
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("database schema");
      expect(req.messages[0].role).toBe("subagent-result");
    });

    test("ingests messages when no summary but messages provided", async () => {
      const engine = createEngine();
      await engine.onSubagentEnded({
        childSessionKey: "child-1",
        reason: "completed",
        messages: [
          { role: "user", content: "Investigate the performance bottleneck in the API" },
          { role: "assistant", content: "The bottleneck is in the database query layer due to missing indexes" },
        ],
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("bottleneck");
    });

    test("no-op when neither summary nor messages", async () => {
      const engine = createEngine();
      await engine.onSubagentEnded({
        childSessionKey: "child-1",
        reason: "completed",
      });

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips when server unhealthy", async () => {
      mockOverrides.healthy = false;
      const engine = createEngine();
      await engine.onSubagentEnded({
        childSessionKey: "child-1",
        reason: "completed",
        summary: "The subagent found important results about the system architecture",
      });

      expect(lastRequest["/messages"]).toBeUndefined();
    });
  });

  // ========================================================================
  // prepareSubagentSpawn
  // ========================================================================

  describe("prepareSubagentSpawn", () => {
    test("returns facts as systemPromptAddition when taskDescription provided", async () => {
      const engine = createEngine();
      const result = await engine.prepareSubagentSpawn({
        parentSessionKey: "parent",
        childSessionKey: "child",
        taskDescription: "Find information about Alice at Acme Corp",
      });

      expect(result).toBeDefined();
      expect(result!.systemPromptAddition).toContain("<graphiti-context>");
      expect(result!.systemPromptAddition).toContain("</graphiti-context>");
      expect(result!.systemPromptAddition).toContain("WORKS_AT");
    });

    test("returns undefined when no taskDescription", async () => {
      const engine = createEngine();
      const result = await engine.prepareSubagentSpawn({
        parentSessionKey: "parent",
        childSessionKey: "child",
      });

      expect(result).toBeUndefined();
    });

    test("returns undefined when no facts found", async () => {
      mockOverrides.searchFacts = [];
      const engine = createEngine();
      const result = await engine.prepareSubagentSpawn({
        parentSessionKey: "parent",
        childSessionKey: "child",
        taskDescription: "Find something that does not exist",
      });

      expect(result).toBeUndefined();
    });

    test("returns undefined when server unhealthy", async () => {
      mockOverrides.healthy = false;
      const engine = createEngine();
      const result = await engine.prepareSubagentSpawn({
        parentSessionKey: "parent",
        childSessionKey: "child",
        taskDescription: "Find information about Alice",
      });

      expect(result).toBeUndefined();
    });
  });

  // ========================================================================
  // error paths (server failures)
  // ========================================================================

  describe("error paths", () => {
    test("ingest() returns ingested: false on server error", async () => {
      mockOverrides.ingestStatus = 500;
      const engine = createEngine();
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "user", content: "Tell me about the project architecture and design patterns" },
      });

      expect(result.ingested).toBe(false);
    });

    test("ingestBatch() returns ingestedCount: 0 on server error", async () => {
      mockOverrides.ingestStatus = 500;
      const engine = createEngine();
      const result = await engine.ingestBatch({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "What is the architecture of our system?" },
          { role: "assistant", content: "The system uses a microservices architecture with Neo4j." },
        ],
      });

      expect(result.ingestedCount).toBe(0);
    });

    test("afterTurn() does not throw on server error", async () => {
      mockOverrides.ingestStatus = 500;
      const engine = createEngine();
      await expect(
        engine.afterTurn({
          sessionId: "sess-1",
          messages: [
            { role: "system", content: "System prompt" },
            { role: "user", content: "New question about the project architecture" },
            { role: "assistant", content: "New answer about the system design patterns" },
          ],
          prePromptMessageCount: 1,
        }),
      ).resolves.toBeUndefined();
    });

    test("onSubagentEnded() does not throw on server error", async () => {
      mockOverrides.ingestStatus = 500;
      const engine = createEngine();
      await expect(
        engine.onSubagentEnded({
          childSessionKey: "child-1",
          reason: "completed",
          summary: "The subagent found that the database schema needs migration to support new features",
        }),
      ).resolves.toBeUndefined();
    });

    test("prepareSubagentSpawn() returns undefined on search error", async () => {
      mockOverrides.searchStatus = 500;
      const engine = createEngine();
      const result = await engine.prepareSubagentSpawn({
        parentSessionKey: "parent",
        childSessionKey: "child",
        taskDescription: "Find information about Alice at Acme Corp",
      });

      expect(result).toBeUndefined();
    });
  });

  // ========================================================================
  // dispose
  // ========================================================================

  describe("dispose", () => {
    test("dispose is a no-op", () => {
      const engine = createEngine();
      expect(() => engine.dispose()).not.toThrow();
    });
  });

  // ========================================================================
  // formatFactsAsContext
  // ========================================================================

  describe("formatFactsAsContext", () => {
    test("formats facts array as graphiti-context block", () => {
      const facts = [
        { name: "WORKS_AT", fact: "Alice works at Acme Corp" },
        { name: "PREFERS", fact: "User prefers dark mode" },
      ];

      const result = formatFactsAsContext(facts);

      expect(result).toContain("<graphiti-context>");
      expect(result).toContain("</graphiti-context>");
      expect(result).toContain("- **WORKS_AT**: Alice works at Acme Corp");
      expect(result).toContain("- **PREFERS**: User prefers dark mode");
    });
  });
});
