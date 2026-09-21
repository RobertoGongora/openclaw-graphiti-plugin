---
name: Bug Report
about: Report a bug to help us improve
title: ""
labels: bug
assignees: ""
---

## Description

A clear and concise description of the bug.

## Steps to Reproduce

1. ...
2. ...
3. ...

## Expected Behavior

What you expected to happen.

## Actual Behavior

What actually happened.

## Component

- [ ] `graph_memory` Python service (CLI, daemon, MCP server, Docker image)
- [ ] Legacy OpenClaw plugin (`@robertogongora/graphiti`, archived — see `docs/legacy-openclaw.md`)

## Environment

Fill in the block for the component you ticked.

### graph_memory service

- **Python version:** (`python --version`)
- **Neo4j version / image:**
- **Namespace:** (`--namespace` / `MEMORY_NAMESPACE`)
- **MCP client and version:** (Claude Code, Codex, other)
- **Image tag:** (`GRAPH_MEMORY_TAG`, or "source checkout" + commit)
- **OS:**

### Legacy OpenClaw plugin

- **Plugin version:**
- **OpenClaw version:**
- **Node.js version:**
- **Graphiti server version:**
- **OS:**

## Logs / Screenshots

If applicable, add logs or screenshots to help explain the problem. Remove secrets, API keys, and private transcript content first.

## Debug Log (legacy plugin only)

Paste the output of `openclaw graphiti logs` below. This contains only operational metadata (status codes, timing, counts) and is safe to share.

```
(paste output here)
```
