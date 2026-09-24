---
name: memory
description: Use when the user asks about their projects, preferences, people, decisions, or past work; asks when something last happened; asks to remember or correct information; when a conversation establishes new information worth remembering; or when the user wants to see how their memories connect.
---

# Memory

Use the graph-memory MCP for persistent memory. Discover its tools before answering from past conversations or assuming that no memory is available. The connection chooses the memory graph; callers do not supply a namespace.

Consult memory before work that depends on the user's history, preferences, project conventions, or previous decisions, including ambiguous requests. Skip lookup for self-contained tasks that need none of that context. If repeated errors or unexpected behavior suggest a prior decision may matter, look up that context before repeating the same approach.

- Recall information with `memory_recall`, passing the subject as `entity` and the user's question as `question`. Start with the default compact response. Request more detail only when the returned facts leave a specific gap.
- Search remembered facts across entities with `memory_search`, passing the full question or a topic. Use this when the subject is unclear, the question spans several entities, or an entity lookup misses. This is keyword search; try fewer distinctive words or an alternative name if needed. An empty search does not establish that no relevant memory exists.
- Find names and identities with `memory_search_entities` when needed. Use a short name such as `Ketch` or `T3 Code`, not an entire question: all words must match the same entity's names or aliases. Search without a kind filter first; use returned entity keys for follow-up calls, and put the question in `memory_recall.question`.
- Use `memory_status` to check episode counts, processing, queued/failed work, and the latest saved or completed episode. Active leases do not prove worker liveness.
- Use `memory_latest` for when something last happened, `memory_evidence` for sources, and `memory_render` to visualize connections.
- Use `memory_ingest` to remember new information, `memory_retract` to withdraw an incorrect fact, and `memory_merge` for confirmed duplicate identities. Discover the input schema when using each tool.

Keep lookup focused on the task. Follow returned entity keys or evidence references to resolve specific gaps; avoid exhaustive graph traversal or broad filesystem searches. Stop after targeted searches find no useful match.

Learn from the conversation as well as recalling it. When work establishes a durable new fact, preference, decision, or correction, use the memory tools to retain it within the user's authorized scope; an explicit "remember this" is not required. Keep useful knowledge rather than routine progress chatter. Preserve who said it and when, distinguish proposals from completed work, and reuse source/message identities on retries. Background workers already ingest saved conversations, so do not upload whole sessions again to make them learnable. An accepted write can still be queued and unavailable to recall.

Preserve the source roles: user statements and assistant reports are conversational claims; tool outputs validate claims rather than originate them. Memory-file summaries are derived material. An assistant report is not confirmed merely because it was saved or repeated. Apply corrections through the tools rather than rewriting source evidence or editing native memory files.

The graph represents recorded evidence, not a live check. Verify potentially stale external state when verification is practical; otherwise state that the answer is memory-derived and may be outdated. Use returned source references for claims about prior work. Ingestion time is not event time. To test what memory could have supplied before a conversation, set `known_at` to its start time; current recall can contain information learned during that same conversation. Pending or failed episodes and unseen sessions limit coverage; zero pending episodes alone does not establish that ingestion is complete. A search miss means no match was found, not that the person or relationship does not exist.

## Showing graph images

When the user requests a graph image, call `memory_render` with their requested scope or Cypher; omit Cypher for the whole graph. Keep the returned image and render metadata. An image visible in a tool response may not be visible to the user.

For Codex chat, save the returned image to a persistent local PNG outside tracked source files, then embed it in the final reply with `![Memory graph](/absolute/path/graph.png)` and provide a separate full-size file link. Use the actual saved path. Reuse the original render result instead of rendering again merely to deliver it.

Write image bytes through a file/binary API or stdin when available. If shell commands are the only option, decode and append bounded base64 chunks, splitting on multiples of four characters; never place the entire encoded image in a shell argument or Markdown data URL. Large renders exceed command-length limits. Report any truncation indicated by the render metadata.
