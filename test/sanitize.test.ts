/**
 * Unit tests for sanitizeForCapture().
 */

import { describe, test, expect } from "vitest";
import { sanitizeForCapture } from "../shared.js";

describe("sanitizeForCapture", () => {
  test("strips <graphiti-context> blocks", () => {
    const input = "before\n<graphiti-context>\nsome recalled facts\n</graphiti-context>\nafter";
    expect(sanitizeForCapture(input)).toBe("before\n\nafter");
  });

  test("strips multiline <graphiti-context> blocks", () => {
    const input = "<graphiti-context>\nRelevant knowledge graph facts (auto-recalled):\n- **WORKS_AT**: Alice works at Acme\n</graphiti-context>";
    expect(sanitizeForCapture(input)).toBe("");
  });

  test("strips <relevant-memories> blocks", () => {
    const input = "hello\n<relevant-memories>\nmemory data\n</relevant-memories>\nworld";
    expect(sanitizeForCapture(input)).toBe("hello\n\nworld");
  });

  test("strips conversation metadata JSON blocks", () => {
    const input = 'before\n```json\n{"schema":"openclaw.inbound_meta","version":1}\n```\nafter';
    expect(sanitizeForCapture(input)).toBe("before\n\nafter");
  });

  test("strips sender metadata JSON blocks", () => {
    const input = 'before\n```json\n{"label":"User","id":"u1","sender":"alice"}\n```\nafter';
    expect(sanitizeForCapture(input)).toBe("before\n\nafter");
  });

  test("strips [Subagent Context] prefixed lines", () => {
    const input = "line1\n[Subagent Context] some context here\nline2";
    expect(sanitizeForCapture(input)).toBe("line1\n\nline2");
  });

  test("strips leading timestamps", () => {
    const input = "[Mon 2026-03-15 14:00 UTC] Hello there";
    expect(sanitizeForCapture(input)).toBe("Hello there");
  });

  test("strips timestamps on multiple lines", () => {
    const input = "[Tue 2026-03-15 09:30 PST] First\n[Wed 2026-03-16 10:00 EST] Second";
    expect(sanitizeForCapture(input)).toBe("First\nSecond");
  });

  test("strips null bytes", () => {
    const input = "hello\u0000world\u0000test";
    expect(sanitizeForCapture(input)).toBe("helloworldtest");
  });

  test("collapses multiple blank lines to max 2", () => {
    const input = "a\n\n\n\n\nb";
    expect(sanitizeForCapture(input)).toBe("a\n\nb");
  });

  test("passes through clean text unchanged", () => {
    const input = "Alice works at Acme Corp and prefers dark mode.";
    expect(sanitizeForCapture(input)).toBe(input);
  });

  test("returns empty string for empty input", () => {
    expect(sanitizeForCapture("")).toBe("");
  });

  test("returns empty string for text that is entirely metadata", () => {
    const input = "<graphiti-context>\nfacts\n</graphiti-context>";
    expect(sanitizeForCapture(input)).toBe("");
  });

  test("handles combined patterns", () => {
    const input = [
      "[Mon 2026-03-15 14:00 UTC] User said something",
      "<graphiti-context>\nold facts\n</graphiti-context>",
      "[Subagent Context] task result",
      '```json\n{"schema":"openclaw.inbound_meta","v":1}\n```',
      "The actual content we want to keep.",
    ].join("\n");
    const result = sanitizeForCapture(input);
    expect(result).toContain("User said something");
    expect(result).toContain("The actual content we want to keep.");
    expect(result).not.toContain("graphiti-context");
    expect(result).not.toContain("Subagent Context");
    expect(result).not.toContain("openclaw.inbound_meta");
  });

  test("trims leading and trailing whitespace", () => {
    const input = "  \n\nhello\n\n  ";
    expect(sanitizeForCapture(input)).toBe("hello");
  });
});
