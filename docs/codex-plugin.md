# Codex Graph Memory plugin

`plugins/graph-memory/` packages the existing engine as a Codex client integration:

- `.codex-plugin/plugin.json`: plugin identity and presentation.
- `.mcp.json`: stdio connection to the running transcript MCP container.
- `skills/memory/SKILL.md`: intent-based discovery, lookup, learning, and corrections.
- `skills/memory/agents/openai.yaml`: implicit invocation and the MCP dependency.

The plugin does not run a second database or ingestion worker. Docker and the
transcript stack must already be running. The MCP command uses `docker` on PATH,
the current Docker context (Colima on Rob's host), container
`graph-memory-transcripts-mcp-1`, and namespace `transcripts`. A different deployment
must adjust that connection before packaging. Credentials stay in the container;
the plugin does not include credentials, transcripts, or graph contents.

## Local installation

The personal marketplace is `~/.agents/plugins/marketplace.json`. Its source
`~/plugins/graph-memory` links to this checkout's `plugins/graph-memory` directory.
The installed package is cached by Codex, so normal use does not read this checkout.
Keep the source link valid for future plugin reinstalls.

```sh
codex plugin add graph-memory@personal
```

The plugin replaces the previous `[mcp_servers.graph-memory]` registration.
Do not leave an `enabled = false` standalone entry for that name: in the tested
CLI it also suppresses the plugin's server. The old configuration was backed up
privately under `~/.local/share/graph-memory/deployment/plugin-install/` before
removing just the standalone entry. Native memories remain disabled.

The user's plugin policy permits `memory_ingest` so an authorized learning call
can run in a headless session without an approval prompt:

```toml
[plugins."graph-memory@personal".mcp_servers.graph-memory.tools.memory_ingest]
approval_mode = "approve"
```

This is host policy, not a permission grant embedded in the shared plugin.

Start a new Codex thread after installing or updating. The skill description is
available during skill selection; full skill instructions and MCP tools load when
needed. This supports discovery without putting all tool schemas into every turn.

For local development, use the plugin-creator cachebuster helper, validate the
package, and reinstall `graph-memory@personal`. Updating source files alone does
not update the installed cache. For distribution, share the plugin package and
document its Docker service prerequisite.

## Memory behavior

The skill retains useful behavior from the native memory workflow: consult prior
context when it matters, keep lookup focused, revisit context after repeated
errors, verify mutable facts when practical, identify stale or uncertain evidence,
and cite sources. It deliberately uses graph tools instead of the native Markdown
registry and ad hoc update files.

At Rob's request, learning is proactive: a durable new fact, preference, decision,
or correction can trigger memory ingestion without the words "remember this".
Original conversational roles and timestamps remain evidence. Tool outputs only
validate claims. Accepted ingestion can remain queued; it does not mean the new
fact is already retrievable. Workers continue to ingest saved sessions independently.

## Validation scope

The plugin and skill validators check package structure. At validation a fresh
Codex app-server discovered all eight tools of that release through the plugin
alone and successfully called entity search. The catalog now has nine tools;
`memory_status` was added afterwards. Behavioral probes use Terra/medium at standard speed in
ephemeral sessions, so the probes do not become source transcripts.

Compare the ordinary recall prompts in
`evals/reports/terra-memory-routing-v1.json` against
[`evals/reports/codex-plugin-v1.json`](../evals/reports/codex-plugin-v1.json).
The final plugin probe selected graph recall without naming the MCP, followed
evidence and latest-state calls, and disclosed failed/unseen ingestion coverage.
The isolated learning probe submitted a new decision without a "remember" request
and correctly treated the receipt as queued rather than retrievable. Keep raw
answers and private source references outside version control. Synthetic learning
probes use a capture-only MCP fixture with the real tool schemas and no database
connection; they test selection and valid ingestion calls, not extraction quality.

These are routing smoke tests, not a golden accuracy benchmark. Background
ingestion continues during live read probes, so graph contents are not frozen.

Official references: [skills](https://learn.chatgpt.com/docs/build-skills),
[plugins](https://learn.chatgpt.com/docs/plugins), and
[MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
