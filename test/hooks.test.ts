/**
 * Hook behavior tests (before_agent_start, before_compaction, before_reset).
 *
 * Registers the plugin with a mock API, then invokes hook handlers
 * directly and verifies context injection + server ingestion payloads.
 */

import { describe, test, expect, beforeAll, afterAll, beforeEach } from "vitest";
import {
  startMockServer,
  stopMockServer,
  resetMockState,
  createMockApi,
  createMockHookCtx,
  mockOverrides,
  lastRequest,
} from "./helpers.js";

describe("hooks", () => {
  beforeAll(startMockServer);
  afterAll(stopMockServer);
  beforeEach(resetMockState);

  // ========================================================================
  // before_agent_start (auto-recall)
  // ========================================================================

  describe("before_agent_start", () => {
    test("injects facts as prependContext", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi({ autoRecall: true });
      plugin.register(api as any);

      const handler = hooks["before_agent_start"][0];
      const result = await handler({
        prompt: "Tell me about Alice at Acme",
      }, createMockHookCtx());

      expect(result).toBeDefined();
      expect(result.prependContext).toContain("<graphiti-context>");
      expect(result.prependContext).toContain("WORKS_AT");
      expect(result.prependContext).toContain("Alice works at Acme Corp");
      expect(result.prependContext).toContain("dark mode");
    });

    test("returns undefined for short prompts", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi({ autoRecall: true, minPromptLength: 20 });
      plugin.register(api as any);

      const handler = hooks["before_agent_start"][0];
      const result = await handler({ prompt: "hi" }, createMockHookCtx());

      expect(result).toBeUndefined();
    });

    test("returns undefined for HEARTBEAT", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi({ autoRecall: true });
      plugin.register(api as any);

      const handler = hooks["before_agent_start"][0];
      const result = await handler({
        prompt: "HEARTBEAT system check for monitoring",
      }, createMockHookCtx());

      expect(result).toBeUndefined();
    });

    test("returns undefined for boot check", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi({ autoRecall: true });
      plugin.register(api as any);

      const handler = hooks["before_agent_start"][0];
      const result = await handler({
        prompt: "This is a boot check for the system",
      }, createMockHookCtx());

      expect(result).toBeUndefined();
    });

    test("returns undefined when server is unhealthy", async () => {
      mockOverrides.healthy = false;
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi({ autoRecall: true });
      plugin.register(api as any);

      const handler = hooks["before_agent_start"][0];
      const result = await handler({
        prompt: "Tell me about the project architecture",
      }, createMockHookCtx());

      expect(result).toBeUndefined();
    });

    test("returns undefined when no facts found", async () => {
      mockOverrides.searchFacts = [];
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi({ autoRecall: true });
      plugin.register(api as any);

      const handler = hooks["before_agent_start"][0];
      const result = await handler({
        prompt: "Something with no matching knowledge",
      }, createMockHookCtx());

      expect(result).toBeUndefined();
    });
  });

  // ========================================================================
  // before_compaction (auto-capture)
  // ========================================================================

  describe("before_compaction", () => {
    test("ingests conversation to server", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_compaction"][0];
      await handler({
        messages: [
          {
            role: "user",
            content: "What is the architecture of our system?",
          },
          {
            role: "assistant",
            content:
              "The system uses a microservices architecture with Neo4j.",
          },
          {
            role: "user",
            content: "Tell me more about the graph database.",
          },
          {
            role: "assistant",
            content:
              "Neo4j stores entities and relationships as a knowledge graph.",
          },
        ],
        messageCount: 4,
      }, createMockHookCtx());

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.group_id).toBe("test-group");
      expect(req.messages).toHaveLength(1);
      expect(req.messages[0].content).toContain("architecture");
      expect(req.messages[0].content).toContain("Neo4j");
      expect(req.messages[0].role_type).toBe("user");
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.event).toBe("before_compaction");
      expect(prov.plugin).toBe("openclaw-graphiti");
      expect(prov.group_id).toBe("test-group");
      expect(prov.ts).toBeDefined();
      expect(prov.session_key).toBe("test-session-key");
      expect(prov.agent).toBe("test-agent");
      expect(prov.channel).toBe("test-channel");
      expect(req.messages[0].name).toContain("compaction-test-session-key-");
    });

    test("skips when fewer than 4 messages", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_compaction"][0];
      await handler({
        messages: [
          { role: "user", content: "Hello" },
          { role: "assistant", content: "Hi!" },
        ],
        messageCount: 2,
      }, createMockHookCtx());

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips when server is unhealthy", async () => {
      mockOverrides.healthy = false;
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_compaction"][0];
      await handler({
        messages: [
          {
            role: "user",
            content: "What is the architecture of our system?",
          },
          {
            role: "assistant",
            content: "The system uses microservices.",
          },
          {
            role: "user",
            content: "Tell me more about the database.",
          },
          {
            role: "assistant",
            content: "We use Neo4j for the knowledge graph.",
          },
        ],
        messageCount: 4,
      }, createMockHookCtx());

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("handles content block arrays", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_compaction"][0];
      await handler({
        messages: [
          {
            role: "user",
            content: [
              { type: "text", text: "What about the project architecture?" },
            ],
          },
          {
            role: "assistant",
            content: [
              {
                type: "text",
                text: "The architecture uses event-driven patterns.",
              },
            ],
          },
          {
            role: "user",
            content: [{ type: "text", text: "Can you elaborate on that?" }],
          },
          {
            role: "assistant",
            content: [
              {
                type: "text",
                text: "We use message queues for async communication between services.",
              },
            ],
          },
        ],
        messageCount: 4,
      }, createMockHookCtx());

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("architecture");
      expect(req.messages[0].content).toContain("event-driven");
    });

    test("sessionKey from event propagates into provenance", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_compaction"][0];
      await handler({
        sessionKey: "sess-abc-123",
        messages: [
          { role: "user", content: "What is the architecture of our system?" },
          { role: "assistant", content: "The system uses a microservices architecture with Neo4j." },
          { role: "user", content: "Tell me more about the graph database." },
          { role: "assistant", content: "Neo4j stores entities and relationships as a knowledge graph." },
        ],
        messageCount: 4,
      });

      const req = lastRequest["/messages"] as any;
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.session_key).toBe("sess-abc-123");
    });
  });

  // ========================================================================
  // before_reset (auto-capture on /new)
  // ========================================================================

  describe("before_reset", () => {
    test("ingests conversation to server", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_reset"][0];
      await handler({
        messages: [
          {
            role: "user",
            content: "Let me tell you about our deployment setup.",
          },
          {
            role: "assistant",
            content:
              "I understand you want to discuss deployment infrastructure.",
          },
          {
            role: "user",
            content: "We use Kubernetes with ArgoCD for GitOps.",
          },
          {
            role: "assistant",
            content:
              "That is a solid GitOps workflow with Kubernetes and ArgoCD.",
          },
        ],
      }, createMockHookCtx());

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.group_id).toBe("test-group");
      expect(req.messages[0].content).toContain("deployment");
      expect(req.messages[0].content).toContain("Kubernetes");
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.event).toBe("before_reset");
      expect(prov.plugin).toBe("openclaw-graphiti");
      expect(prov.group_id).toBe("test-group");
      expect(prov.ts).toBeDefined();
      expect(prov.session_key).toBe("test-session-key");
      expect(prov.agent).toBe("test-agent");
      expect(prov.channel).toBe("test-channel");
      expect(req.messages[0].name).toContain("session-reset-test-session-key-");
    });

    test("skips when fewer than 4 messages", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_reset"][0];
      await handler({
        messages: [
          { role: "user", content: "Short session" },
          { role: "assistant", content: "Indeed." },
        ],
      }, createMockHookCtx());

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("skips when no messages", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_reset"][0];
      await handler({}, createMockHookCtx());

      expect(lastRequest["/messages"]).toBeUndefined();
    });

    test("handles content block arrays", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_reset"][0];
      await handler({
        messages: [
          {
            role: "user",
            content: [
              { type: "text", text: "What about our deployment pipeline?" },
            ],
          },
          {
            role: "assistant",
            content: [
              {
                type: "text",
                text: "The pipeline uses GitHub Actions with ArgoCD.",
              },
            ],
          },
          {
            role: "user",
            content: [{ type: "text", text: "How does the rollback work?" }],
          },
          {
            role: "assistant",
            content: [
              {
                type: "text",
                text: "ArgoCD supports automatic rollback on health check failure.",
              },
            ],
          },
        ],
      });

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      expect(req.messages[0].content).toContain("deployment pipeline");
      expect(req.messages[0].content).toContain("ArgoCD");
    });
  });

  // ========================================================================
  // Session metadata edge cases
  // ========================================================================

  describe("session metadata", () => {
    test("hooks work when ctx is undefined (backward compat)", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_compaction"][0];
      await handler({
        messages: [
          { role: "user", content: "What is the architecture of our system?" },
          { role: "assistant", content: "The system uses a microservices architecture with Neo4j." },
          { role: "user", content: "Tell me more about the graph database." },
          { role: "assistant", content: "Neo4j stores entities and relationships as a knowledge graph." },
        ],
        messageCount: 4,
      }, undefined);

      const req = lastRequest["/messages"] as any;
      expect(req).toBeDefined();
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.event).toBe("before_compaction");
      expect(prov.plugin).toBe("openclaw-graphiti");
      expect(prov.group_id).toBe("test-group");
      expect(prov.session_key).toBeUndefined();
    });

    test("handles partial ctx (missing fields)", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      const handler = hooks["before_compaction"][0];
      await handler({
        messages: [
          { role: "user", content: "What is the architecture of our system?" },
          { role: "assistant", content: "The system uses a microservices architecture with Neo4j." },
          { role: "user", content: "Tell me more about the graph database." },
          { role: "assistant", content: "Neo4j stores entities and relationships as a knowledge graph." },
        ],
        messageCount: 4,
      }, { sessionKey: "partial-key" });

      const req = lastRequest["/messages"] as any;
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.session_key).toBe("partial-key");
      expect(prov.agent).toBeUndefined();
    });

    test("session_start hook records start time", async () => {
      const { default: plugin } = await import("../index.js");
      const { api, hooks } = createMockApi();
      plugin.register(api as any);

      expect(hooks["session_start"]).toBeDefined();
      expect(hooks["session_start"]).toHaveLength(1);

      // Fire session_start before compaction
      await hooks["session_start"][0]({}, { sessionId: "sess-42", sessionKey: "key-42" });

      // Now fire compaction with same sessionId
      await hooks["before_compaction"][0]({
        messages: [
          { role: "user", content: "What is the architecture of our system?" },
          { role: "assistant", content: "The system uses a microservices architecture with Neo4j." },
          { role: "user", content: "Tell me more about the graph database." },
          { role: "assistant", content: "Neo4j stores entities and relationships as a knowledge graph." },
        ],
      }, { sessionKey: "key-42", sessionId: "sess-42", agentId: "a1", messageProvider: "slack" });

      const req = lastRequest["/messages"] as any;
      const prov = JSON.parse(req.messages[0].source_description);
      expect(prov.session_key).toBe("key-42");
      expect(prov.session_start).toBeDefined();
      expect(prov.agent).toBe("a1");
      expect(prov.channel).toBe("slack");
    });
  });
});
