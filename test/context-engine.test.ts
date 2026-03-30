/**
 * Tests for GraphitiContextEngine.
 *
 * Instantiates the engine directly (not via plugin registration)
 * and verifies each method against the mock HTTP server.
 */

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, test, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  getMockPort,
  mockOverrides,
  lastRequest,
  SAMPLE_EPISODES_WITH_SESSION,
} from "./helpers.js";
import { GraphitiClient } from "../client.js";
import { NOOP_LOG } from "../debug-log.js";
import { GraphitiContextEngine } from "../context-engine.js";
import { formatFactsAsContext, isContinuityGap, hasDeicticReferences, readSessionFileTail, formatContinuityBlock, extractEpisodeContinuity } from "../shared.js";
import { createRequire } from "node:module";

const _require = createRequire(import.meta.url);
const PKG_VERSION: string = (_require("../package.json") as any).version;

function createEngine() {
  const port = getMockPort();
  const client = new GraphitiClient(`http://127.0.0.1:${port}`, "test-group", undefined, undefined, NOOP_LOG);
  return new GraphitiContextEngine(client, { recallMaxFacts: 10, autoRecall: true }, "test-group", NOOP_LOG);
}

function createEngineWithConfig(cfg: Record<string, unknown>) {
  const port = getMockPort();
  const client = new GraphitiClient(`http://127.0.0.1:${port}`, "test-group", undefined, undefined, NOOP_LOG);
  return new GraphitiContextEngine(client, { recallMaxFacts: 10, ...cfg }, "test-group", NOOP_LOG);
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
      expect(engine.info.ownsCompaction).toBe(false);
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

    test("sweeps all messages when compaction shrinks array (prePromptMessageCount > messages.length)", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Surviving message after compaction about architecture" },
          { role: "assistant", content: "Response about the system design patterns used" },
          { role: "user", content: "Follow-up question about deployment strategies" },
          { role: "assistant", content: "Details about the CI/CD pipeline configuration" },
          { role: "user", content: "Current turn question about testing approach" },
        ],
        prePromptMessageCount: 10, // pre-compaction count, now only 5 messages remain
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      // All 5 messages should be considered — 4 are user/assistant with enough text
      expect(req.messages[0].content).toContain("architecture");
      expect(req.messages[0].content).toContain("design patterns");
      expect(req.messages[0].content).toContain("deployment");
      expect(req.messages[0].content).toContain("CI/CD");
      expect(req.messages[0].content).toContain("testing approach");
    });

    test("sweep uses after_turn_sweep event in provenance", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Message surviving compaction about architecture" },
          { role: "assistant", content: "Response about the system design and patterns" },
        ],
        prePromptMessageCount: 8, // compaction occurred
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].source_description).toContain("after_turn_sweep");
    });

    test("normal turn uses after_turn event (not sweep)", async () => {
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Old message before this turn was started" },
          { role: "user", content: "New question about the project architecture" },
          { role: "assistant", content: "New answer about the system design patterns" },
        ],
        prePromptMessageCount: 1,
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].source_description).toContain('"event":"after_turn"');
      expect(req.messages[0].source_description).not.toContain("after_turn_sweep");
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

    test("sanitizes summary content before ingestion", async () => {
      const engine = createEngine();
      const summaryWithNoise = [
        "<graphiti-context>Recalled facts that should be stripped</graphiti-context>",
        "[Mon 2026-03-15 14:00 UTC] Timestamped line that should be stripped",
        "[Subagent Context] This metadata line should be stripped",
        "The actual subagent finding about architecture and design patterns",
      ].join("\n");

      await engine.onSubagentEnded({
        childSessionKey: "child-sanitize",
        reason: "completed",
        summary: summaryWithNoise,
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      // Noise should be stripped
      expect(req.messages[0].content).not.toContain("<graphiti-context>");
      expect(req.messages[0].content).not.toContain("[Subagent Context]");
      expect(req.messages[0].content).not.toContain("[Mon 2026-03-15");
      // Actual content should survive
      expect(req.messages[0].content).toContain("actual subagent finding");
    });

    test("sanitizes messages content before ingestion", async () => {
      const engine = createEngine();
      await engine.onSubagentEnded({
        childSessionKey: "child-sanitize-msgs",
        reason: "completed",
        messages: [
          { role: "user", content: "<graphiti-context>Old recalled facts</graphiti-context>\nInvestigate the performance issue in the API" },
          { role: "assistant", content: "The bottleneck is in the database query layer due to missing indexes" },
        ],
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).not.toContain("<graphiti-context>");
      expect(req.messages[0].content).toContain("performance issue");
      expect(req.messages[0].content).toContain("bottleneck");
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

    test("compact() returns ok: false on server error", async () => {
      mockOverrides.ingestStatus = 500;
      const engine = createEngine();
      const result = await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about the project architecture and design patterns" },
          { role: "assistant", content: "The project uses a modular architecture with clean separation" },
        ],
      });

      expect(result.ok).toBe(false);
      expect(result.compacted).toBe(false);
      expect(result.reason).toBe("error");
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
  // autoCapture: false
  // ========================================================================

  describe("autoCapture: false", () => {
    test("afterTurn skips ingestion", async () => {
      const engine = createEngineWithConfig({ autoCapture: false });
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "New question about the project architecture" },
          { role: "assistant", content: "New answer about the system design patterns" },
        ],
        prePromptMessageCount: 0,
      });

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("ingest skips ingestion", async () => {
      const engine = createEngineWithConfig({ autoCapture: false });
      const result = await engine.ingest({
        sessionId: "sess-1",
        message: { role: "user", content: "Tell me about the project architecture and design patterns" },
      });

      expect(result.ingested).toBe(false);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("ingestBatch skips ingestion", async () => {
      const engine = createEngineWithConfig({ autoCapture: false });
      const result = await engine.ingestBatch({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "What is the architecture of our system?" },
          { role: "assistant", content: "The system uses a microservices architecture with Neo4j." },
        ],
      });

      expect(result.ingestedCount).toBe(0);
      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("compact still works (not gated by autoCapture)", async () => {
      const engine = createEngineWithConfig({ autoCapture: false });
      const result = await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about the project architecture and design patterns" },
          { role: "assistant", content: "The project uses a modular architecture with clean separation" },
        ],
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(true);
      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("architecture");
    });
  });

  // ========================================================================
  // compact params
  // ========================================================================

  describe("compact params", () => {
    test("accepts customInstructions and runtimeContext without error", async () => {
      const engine = createEngine();
      const result = await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about the project architecture and design patterns" },
          { role: "assistant", content: "The project uses a modular architecture with clean separation" },
        ],
        customInstructions: "Summarize concisely",
        runtimeContext: { compactBuiltIn: true },
      });

      expect(result.ok).toBe(true);
      expect(result.compacted).toBe(true);
    });
  });

  // ========================================================================
  // sweep tail truncation
  // ========================================================================

  describe("sweep tail truncation", () => {
    test("preserves newest messages when sweep content exceeds 12k chars", async () => {
      // Build messages where the total joined text exceeds 12000 chars.
      // The last message should be at the tail and must survive truncation.
      const filler = "x".repeat(6000);
      const engine = createEngine();
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: `First old message with filler: ${filler}` },
          { role: "assistant", content: `Second old message with filler: ${filler}` },
          { role: "user", content: `Third old message with filler: ${filler}` },
          { role: "assistant", content: "UNIQUE_TAIL_MARKER: this is the newest turn response" },
        ],
        prePromptMessageCount: 10, // trigger sweep
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      // The tail marker must survive because sweep uses slice(-12000)
      expect(req.messages[0].content).toContain("UNIQUE_TAIL_MARKER");
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

  // ========================================================================
  // Smart autoRecall
  // ========================================================================

  describe("Smart autoRecall", () => {
    let tmpDir: string;

    beforeEach(async () => {
      tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "graphiti-smart-recall-"));
    });

    afterEach(async () => {
      await fs.rm(tmpDir, { recursive: true, force: true });
    });

    function createSessionFile(messages: Array<{ role: string; content: string }>): string {
      const filePath = path.join(tmpDir, "session.jsonl");
      const lines = [
        JSON.stringify({ type: "session", version: 2, id: "test-session", timestamp: new Date().toISOString() }),
        ...messages.map((m) => JSON.stringify({ type: "message", message: { role: m.role, content: m.content } })),
      ];
      // Write synchronously for simplicity in tests — file is small
      require("node:fs").writeFileSync(filePath, lines.join("\n"));
      return filePath;
    }

    test("fires after bootstrap with session file — produces both continuity and semantic blocks", async () => {
      const sessionFile = createSessionFile([
        { role: "user", content: "What is the architecture of the new feature?" },
        { role: "assistant", content: "The feature uses a modular plugin architecture with event-driven communication." },
        { role: "user", content: "How does the session file work?" },
        { role: "assistant", content: "The session file is a JSONL format that records all conversation history." },
      ]);

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Tell me about Alice" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("</graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("architecture");
      expect(result.systemPromptAddition).toContain("JSONL format");
      // Semantic block should also be present
      expect(result.systemPromptAddition).toContain("<graphiti-context>");
      expect(result.systemPromptAddition).toContain("</graphiti-context>");
    });

    test("does not fire when many messages and no deictic refs", async () => {
      const sessionFile = createSessionFile([
        { role: "user", content: "Old conversation about something" },
        { role: "assistant", content: "Old response about the topic" },
      ]);

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile });

      // Clear the bootstrap event by consuming it
      await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "First question after bootstrap" }],
      });

      // Now a normal turn with many messages — should NOT fire
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "First message in the conversation about testing" },
          { role: "assistant", content: "Response about testing methodology and best practices" },
          { role: "user", content: "Second question about the deployment pipeline" },
          { role: "assistant", content: "Response about the CI/CD pipeline configuration" },
          { role: "user", content: "What tools do we use for monitoring?" },
        ],
      });

      expect(result.systemPromptAddition).toBeUndefined();
    });

    test("fires on deictic references even with sufficient messages", async () => {
      const sessionFile = createSessionFile([
        { role: "user", content: "We were discussing the migration strategy for the database" },
        { role: "assistant", content: "The migration plan involves three phases with rollback support" },
      ]);

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile });

      // Consume bootstrap event
      await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "First question after bootstrap" }],
      });

      // Many messages but deictic reference — should fire
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "First message about the project setup" },
          { role: "assistant", content: "The project uses TypeScript with vitest for testing" },
          { role: "user", content: "Second question about dependencies" },
          { role: "assistant", content: "We use Neo4j for the graph database backend" },
          { role: "user", content: "Continue where we left off with the migration plan" },
        ],
      });

      expect(result.systemPromptAddition).toBeDefined();
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("migration");
    });

    test("no session file — skips continuity block, still runs semantic", async () => {
      const engine = createEngine();
      // Bootstrap WITHOUT session file
      await engine.bootstrap({ sessionId: "sess-1" });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Tell me about Alice" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      // No continuity block since no session file
      expect(result.systemPromptAddition).not.toContain("<graphiti-continuity>");
      // Semantic block should still be present
      expect(result.systemPromptAddition).toContain("<graphiti-context>");
    });

    test("one-shot: _lastEvent cleared after first recovery", async () => {
      const sessionFile = createSessionFile([
        { role: "user", content: "Previous conversation about the system design" },
        { role: "assistant", content: "The system uses event-driven architecture with plugins" },
      ]);

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile });

      // First assemble — bootstrap event triggers recovery
      const result1 = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Tell me about Alice" }],
      });
      expect(result1.systemPromptAddition).toBeDefined();
      expect(result1.systemPromptAddition).toContain("<graphiti-continuity>");

      // Second assemble with few messages but no event — gap by message count still works
      const result2 = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "What else is there?" }],
      });
      // Still fires because messageCount <= 3 (gap heuristic), but no event flag
      expect(result2.systemPromptAddition).toBeDefined();
    });

    test("fires after compact", async () => {
      const sessionFile = createSessionFile([
        { role: "user", content: "Discussion about the authentication system redesign" },
        { role: "assistant", content: "The new auth system will use JWT with refresh tokens" },
      ]);

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile });

      // Consume bootstrap event
      await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "First question after bootstrap" }],
      });

      // Compact
      await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Some messages being compacted from the conversation" },
          { role: "assistant", content: "Responses that were part of the compacted segment" },
        ],
      });

      // Next assemble after compact should trigger recovery
      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "What were we discussing about auth?" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("authentication");
    });

    test("afterTurn keeps session file reference fresh", async () => {
      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1" });

      // Consume bootstrap event
      await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "First question after bootstrap" }],
      });

      // afterTurn provides the session file
      const sessionFile = createSessionFile([
        { role: "user", content: "Important context about the API redesign project" },
        { role: "assistant", content: "The API will use GraphQL instead of REST endpoints" },
      ]);

      await engine.afterTurn({
        sessionId: "sess-1",
        sessionFile,
        messages: [
          { role: "user", content: "New question about the project architecture and design" },
          { role: "assistant", content: "New answer about the system patterns and conventions" },
        ],
        prePromptMessageCount: 0,
      });

      // Compact to trigger recovery on next assemble
      await engine.compact({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Messages being compacted from the session history" },
          { role: "assistant", content: "Responses included in the compaction cycle" },
        ],
      });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "What about the API?" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("GraphQL");
    });

    test("session file with only metadata and no messages returns semantic-only", async () => {
      const filePath = path.join(tmpDir, "empty-session.jsonl");
      const lines = [
        JSON.stringify({ type: "session", version: 2, id: "test" }),
        JSON.stringify({ type: "custom", customType: "model-snapshot", data: {} }),
      ];
      require("node:fs").writeFileSync(filePath, lines.join("\n"));

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile: filePath });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Tell me about Alice" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      expect(result.systemPromptAddition).not.toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("<graphiti-context>");
    });

    test("reset with empty session file — recovers continuity from same-session episodes", async () => {
      mockOverrides.episodes = SAMPLE_EPISODES_WITH_SESSION;
      const emptySessionFile = path.join(tmpDir, "new-session.jsonl");
      require("node:fs").writeFileSync(emptySessionFile, JSON.stringify({
        type: "session", version: 2, id: "new-session", timestamp: new Date().toISOString(),
      }));

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile: emptySessionFile });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "What were we just talking about?" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      // Episode-based recovery should produce continuity block
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("microservices");
      // Semantic block also present (search driven by episode content)
      expect(result.systemPromptAddition).toContain("<graphiti-context>");
      // Should have used /search (not /get-memory) since continuity was recovered
      expect(lastRequest["/search"]).toBeDefined();
    });

    test("episode recovery when no session file at all", async () => {
      mockOverrides.episodes = SAMPLE_EPISODES_WITH_SESSION;

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1" }); // no sessionFile

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Continue where we left off" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("microservices");
    });

    test("episode recovery filters by session_key — excludes other sessions", async () => {
      mockOverrides.episodes = SAMPLE_EPISODES_WITH_SESSION;

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1" });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "What were we discussing?" }],
      });

      expect(result.systemPromptAddition).toContain("microservices");
      expect(result.systemPromptAddition).not.toContain("Unrelated conversation from another session");
    });

    test("episode recovery returns null when no matching session — falls to getMemory", async () => {
      mockOverrides.episodes = SAMPLE_EPISODES_WITH_SESSION;
      // Use a different set of facts for /get-memory to prove it was called
      mockOverrides.getMemoryFacts = [
        { uuid: "f-gm-001", name: "FALLBACK", fact: "Fallback semantic fact from getMemory", valid_at: null, invalid_at: null, created_at: "2024-01-01T00:00:00Z", expired_at: null },
      ];

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "no-match-session" });

      const result = await engine.assemble({
        sessionId: "no-match-session",
        messages: [{ role: "user", content: "Tell me something" }],
      });

      expect(result.systemPromptAddition).toBeDefined();
      // No continuity block — no episodes match this session
      expect(result.systemPromptAddition).not.toContain("<graphiti-continuity>");
      // Falls to getMemory which returns the fallback facts
      expect(result.systemPromptAddition).toContain("<graphiti-context>");
      expect(result.systemPromptAddition).toContain("Fallback semantic fact from getMemory");
    });

    test("episode recovery prefers thread_id match over session_key-only", async () => {
      mockOverrides.episodes = SAMPLE_EPISODES_WITH_SESSION;

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", threadId: "thread-a" });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Continue" }],
      });

      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      // thread-a episode ("deployment pipeline") should be prioritized
      expect(result.systemPromptAddition).toContain("deployment pipeline");
    });

    test("skips episode fetch when session file has content", async () => {
      mockOverrides.episodes = SAMPLE_EPISODES_WITH_SESSION;
      const sessionFile = createSessionFile([
        { role: "user", content: "This is the existing session file content about React hooks" },
        { role: "assistant", content: "React hooks are used for state management and side effects." },
      ]);

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", sessionFile });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Tell me more" }],
      });

      // Continuity from session file, not episodes
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("React hooks");
      // Should use /search (driven by session file tail), not episode content
      expect(lastRequest["/search"]).toBeDefined();
      // Continuity should NOT contain episode content
      expect(result.systemPromptAddition).not.toContain("microservices");
    });

    test("timeout/resume — recovers from episodes after bootstrap with no session file", async () => {
      // Simulates a timeout-resume scenario: the runtime bootstraps a new session
      // with no session file. Episodes from the prior session should be recovered.
      mockOverrides.episodes = SAMPLE_EPISODES_WITH_SESSION;
      mockOverrides.getMemoryFacts = [
        { uuid: "f-unrelated", name: "UNRELATED", fact: "Completely unrelated fact", valid_at: null, invalid_at: null, created_at: "2024-01-01T00:00:00Z", expired_at: null },
      ];

      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1" }); // no sessionFile — simulates resume

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Where were we?" }],
      });

      // Should recover from episodes, NOT fall to getMemory
      expect(result.systemPromptAddition).toContain("<graphiti-continuity>");
      expect(result.systemPromptAddition).toContain("microservices");
      // /search should be used (driven by episode content), not /get-memory
      expect(lastRequest["/search"]).toBeDefined();
    });

    test("autoRecall: false disables recall in assemble but engine still works", async () => {
      const engine = createEngineWithConfig({ autoRecall: false });
      await engine.bootstrap({ sessionId: "sess-1" });

      const result = await engine.assemble({
        sessionId: "sess-1",
        messages: [{ role: "user", content: "Where were we?" }],
      });

      // assemble should return pass-through — no recall injection
      expect(result.systemPromptAddition).toBeUndefined();
      expect(lastRequest["/search"]).toBeUndefined();
      expect(lastRequest["/get-memory"]).toBeUndefined();
    });

    test("autoRecall: false still allows capture via afterTurn", async () => {
      const engine = createEngineWithConfig({ autoRecall: false, autoCapture: true });
      await engine.bootstrap({ sessionId: "sess-1" });

      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Tell me about the deployment pipeline and its stages" },
          { role: "assistant", content: "The pipeline uses Docker containers with GitHub Actions for CI/CD" },
        ],
      });

      // Capture should still work even with recall disabled
      expect(lastRequest["/messages"]).toBeDefined();
    });
  });

  // ========================================================================
  // isContinuityGap (unit tests)
  // ========================================================================

  describe("isContinuityGap", () => {
    test("returns true for messageCount below threshold", () => {
      expect(isContinuityGap(0)).toBe(true);
      expect(isContinuityGap(1)).toBe(true);
      expect(isContinuityGap(2)).toBe(true);
      expect(isContinuityGap(3)).toBe(true);
    });

    test("returns false for messageCount above threshold", () => {
      expect(isContinuityGap(4)).toBe(false);
      expect(isContinuityGap(10)).toBe(false);
    });

    test("returns true for bootstrap event regardless of count", () => {
      expect(isContinuityGap(100, { recentEvent: "bootstrap" })).toBe(true);
    });

    test("returns true for compact event regardless of count", () => {
      expect(isContinuityGap(100, { recentEvent: "compact" })).toBe(true);
    });

    test("respects custom threshold", () => {
      expect(isContinuityGap(5, { threshold: 5 })).toBe(true);
      expect(isContinuityGap(6, { threshold: 5 })).toBe(false);
    });

    test("null recentEvent does not trigger", () => {
      expect(isContinuityGap(10, { recentEvent: null })).toBe(false);
    });
  });

  // ========================================================================
  // hasDeicticReferences (unit tests)
  // ========================================================================

  describe("hasDeicticReferences", () => {
    test("detects 'as I mentioned'", () => {
      expect(hasDeicticReferences("As I mentioned earlier, the API needs refactoring")).toBe(true);
    });

    test("detects 'continue'", () => {
      expect(hasDeicticReferences("continue with the implementation")).toBe(true);
    });

    test("detects 'where we left off'", () => {
      expect(hasDeicticReferences("pick up where we left off")).toBe(true);
    });

    test("detects 'that approach'", () => {
      expect(hasDeicticReferences("let's use that approach")).toBe(true);
    });

    test("detects 'go on'", () => {
      expect(hasDeicticReferences("go on with the plan")).toBe(true);
    });

    test("detects 'back to the'", () => {
      expect(hasDeicticReferences("back to the original question")).toBe(true);
    });

    test("does not match normal prompts", () => {
      expect(hasDeicticReferences("What is the architecture of this system?")).toBe(false);
      expect(hasDeicticReferences("Write a function to parse JSON")).toBe(false);
      expect(hasDeicticReferences("How do I deploy to production?")).toBe(false);
    });
  });

  // ========================================================================
  // readSessionFileTail (unit tests)
  // ========================================================================

  describe("readSessionFileTail", () => {
    let tmpDir: string;

    beforeEach(async () => {
      tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "graphiti-session-tail-"));
    });

    afterEach(async () => {
      await fs.rm(tmpDir, { recursive: true, force: true });
    });

    test("reads user and assistant messages from JSONL", async () => {
      const filePath = path.join(tmpDir, "session.jsonl");
      const lines = [
        JSON.stringify({ type: "session", version: 2, id: "test" }),
        JSON.stringify({ type: "message", message: { role: "user", content: "Hello world" } }),
        JSON.stringify({ type: "message", message: { role: "assistant", content: "Hi there, how can I help?" } }),
      ];
      await fs.writeFile(filePath, lines.join("\n"));

      const result = await readSessionFileTail(filePath);
      expect(result).toBe("User: Hello world\nAssistant: Hi there, how can I help?");
    });

    test("skips non-message entries", async () => {
      const filePath = path.join(tmpDir, "session.jsonl");
      const lines = [
        JSON.stringify({ type: "session", version: 2, id: "test" }),
        JSON.stringify({ type: "custom", customType: "model-snapshot", data: {} }),
        JSON.stringify({ type: "message", message: { role: "user", content: "What about the plan?" } }),
        JSON.stringify({ type: "custom", customType: "tool-result", data: {} }),
        JSON.stringify({ type: "message", message: { role: "assistant", content: "Here is the plan overview." } }),
      ];
      await fs.writeFile(filePath, lines.join("\n"));

      const result = await readSessionFileTail(filePath);
      expect(result).toBe("User: What about the plan?\nAssistant: Here is the plan overview.");
    });

    test("respects maxChars budget", async () => {
      const filePath = path.join(tmpDir, "session.jsonl");
      const lines = [
        JSON.stringify({ type: "session", version: 2, id: "test" }),
        JSON.stringify({ type: "message", message: { role: "user", content: "First message ".repeat(100) } }),
        JSON.stringify({ type: "message", message: { role: "assistant", content: "Second message ".repeat(100) } }),
        JSON.stringify({ type: "message", message: { role: "user", content: "Last message" } }),
      ];
      await fs.writeFile(filePath, lines.join("\n"));

      const result = await readSessionFileTail(filePath, 100);
      expect(result).not.toBeNull();
      // Should only contain the last message(s) that fit within budget
      expect(result!).toContain("Last message");
      expect(result!.length).toBeLessThanOrEqual(200); // some slack for formatting
    });

    test("returns null for missing file", async () => {
      const result = await readSessionFileTail("/nonexistent/path/session.jsonl");
      expect(result).toBeNull();
    });

    test("returns null for file with no messages", async () => {
      const filePath = path.join(tmpDir, "empty.jsonl");
      const lines = [
        JSON.stringify({ type: "session", version: 2, id: "test" }),
        JSON.stringify({ type: "custom", customType: "model-snapshot", data: {} }),
      ];
      await fs.writeFile(filePath, lines.join("\n"));

      const result = await readSessionFileTail(filePath);
      expect(result).toBeNull();
    });

    test("skips system and tool role messages", async () => {
      const filePath = path.join(tmpDir, "session.jsonl");
      const lines = [
        JSON.stringify({ type: "message", message: { role: "system", content: "You are a helpful assistant" } }),
        JSON.stringify({ type: "message", message: { role: "user", content: "Hello world friend" } }),
        JSON.stringify({ type: "message", message: { role: "tool", content: "Tool output result" } }),
        JSON.stringify({ type: "message", message: { role: "assistant", content: "Hi there, how can I help you?" } }),
      ];
      await fs.writeFile(filePath, lines.join("\n"));

      const result = await readSessionFileTail(filePath);
      expect(result).toBe("User: Hello world friend\nAssistant: Hi there, how can I help you?");
    });
  });

  // ========================================================================
  // formatContinuityBlock (unit tests)
  // ========================================================================

  describe("formatContinuityBlock", () => {
    test("wraps text in graphiti-continuity tags", () => {
      const result = formatContinuityBlock("User: Hello\nAssistant: Hi");
      expect(result).toBe("<graphiti-continuity>\nRecent session context (recovered from transcript):\nUser: Hello\nAssistant: Hi\n</graphiti-continuity>");
    });
  });

  // ========================================================================
  // extractEpisodeContinuity (unit tests)
  // ========================================================================

  describe("extractEpisodeContinuity", () => {
    test("returns null when no episodes match session_key", () => {
      const episodes = [
        { source_description: JSON.stringify({ session_key: "other" }), content: "irrelevant" },
      ];
      expect(extractEpisodeContinuity(episodes, "my-session")).toBeNull();
    });

    test("returns content from matching session_key episodes", () => {
      const episodes = [
        { source_description: JSON.stringify({ session_key: "my-session" }), content: "user: Hello\nassistant: Hi", created_at: "2024-01-15T10:00:00Z" },
        { source_description: JSON.stringify({ session_key: "other" }), content: "unrelated" },
      ];
      const result = extractEpisodeContinuity(episodes, "my-session");
      expect(result).toBe("user: Hello\nassistant: Hi");
    });

    test("prefers thread_id match over session_key-only", () => {
      const episodes = [
        { source_description: JSON.stringify({ session_key: "s1" }), content: "session-only content", created_at: "2024-01-15T11:00:00Z" },
        { source_description: JSON.stringify({ session_key: "s1", thread_id: "t1" }), content: "thread-matched content", created_at: "2024-01-15T10:00:00Z" },
      ];
      const result = extractEpisodeContinuity(episodes, "s1", { threadId: "t1" });
      // thread-matched should come first despite being older
      expect(result).toContain("thread-matched content");
      expect(result!.indexOf("thread-matched")).toBeLessThan(result!.indexOf("session-only"));
    });

    test("respects maxChars budget", () => {
      const episodes = [
        { source_description: JSON.stringify({ session_key: "s1" }), content: "A".repeat(5000), created_at: "2024-01-15T11:00:00Z" },
        { source_description: JSON.stringify({ session_key: "s1" }), content: "B".repeat(5000), created_at: "2024-01-15T10:00:00Z" },
      ];
      const result = extractEpisodeContinuity(episodes, "s1", { maxChars: 6000 });
      expect(result).toBeDefined();
      expect(result!.length).toBeLessThanOrEqual(6000);
      // First episode fits; second is truncated
      expect(result).toContain("A".repeat(5000));
    });

    test("returns null for episodes with no content", () => {
      const episodes = [
        { source_description: JSON.stringify({ session_key: "s1" }), content: "" },
        { source_description: JSON.stringify({ session_key: "s1" }) },
      ];
      expect(extractEpisodeContinuity(episodes, "s1")).toBeNull();
    });

    test("handles unparseable source_description gracefully", () => {
      const episodes = [
        { source_description: "not-json", content: "some content" },
        { source_description: JSON.stringify({ session_key: "s1" }), content: "valid content" },
      ];
      const result = extractEpisodeContinuity(episodes, "s1");
      expect(result).toBe("valid content");
    });

    test("cross-session thread_id does not leak content", () => {
      const episodes = [
        // Different session but same thread_id — should NOT be included
        { source_description: JSON.stringify({ session_key: "other-session", thread_id: "t1" }), content: "secret from other session", created_at: "2024-01-15T12:00:00Z" },
        // Matching session, no thread_id — should be included
        { source_description: JSON.stringify({ session_key: "my-session" }), content: "my session content", created_at: "2024-01-15T11:00:00Z" },
      ];
      const result = extractEpisodeContinuity(episodes, "my-session", { threadId: "t1" });
      expect(result).toBe("my session content");
      expect(result).not.toContain("secret from other session");
    });
  });

  // ========================================================================
  // thread_id provenance
  // ========================================================================

  describe("thread_id provenance", () => {
    test("ingest includes thread_id in provenance when set via bootstrap", async () => {
      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", threadId: "thread-abc" });
      await engine.ingest({
        sessionId: "sess-1",
        message: { role: "user", content: "This is a test message for provenance checking" },
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.thread_id).toBe("thread-abc");
      expect(prov.session_key).toBe("sess-1");
    });

    test("afterTurn includes thread_id in provenance", async () => {
      const engine = createEngine();
      await engine.bootstrap({ sessionId: "sess-1", threadId: "thread-xyz" });
      await engine.afterTurn({
        sessionId: "sess-1",
        messages: [
          { role: "user", content: "Hello there, how are you doing today?" },
          { role: "assistant", content: "I am doing great, thank you for asking!" },
        ],
        prePromptMessageCount: 0,
      });

      const req = lastRequest["/messages"] as any;
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.thread_id).toBe("thread-xyz");
    });
  });
});
