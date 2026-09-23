"""Read provenance, never fresh evidence. No source bytes or cursor hashes change."""

import json
import re
from datetime import UTC, datetime

MEMORY_CALL = re.compile(r"memory_(?:recall|latest|evidence|search_entities|render|status)\b")
# Literal nested calls in Codex code mode: tools.x.memory_recall(, tools['x.memory_recall'](
# and tools['x'].memory_recall(. Dynamic dispatch is not recognized.
NESTED_READ = re.compile(
    r"\btools(?:\.[\w.]*|\[['\"][\w.]*|\[['\"][\w.]*['\"]\]\.[\w.]*)"
    r"memory_(?:recall|latest|evidence|search_entities|render|status)\b\w*(?:['\"]\])?\s*\("
)
# Exact tool names only: Codex multi-agent tools and Claude Code sub-agent tools.
# A chat tool whose name merely contains send_message is not delegation.
DELEGATION = frozenset(
    {"spawn_agent", "wait_agent", "send_message", "followup_task", "Task", "Agent"}
)
# The memory bank is notes anywhere under a memory directory. A source file whose
# path happens to contain the word is code, not memory.
MEMORY_FILE = re.compile(
    r"(?i)(?:^|/)memor(?:y|ies)/(?:[^/]+/)*[^/]+\.(?:md|txt)$|(?:^|/)memory\.md$"
)
# The parser labels every memory tool read; a process-memory statistic is not one.
MEMORY_LABEL = "derived_memory_retrieval_not_fresh_verification"
PROCESS_MEMORY = {"usage", "stats", "statistics", "metric", "metrics", "consumption", "pressure"}
# Only a command or program can call a nested tool; an edit that writes the
# literal call into a file reads nothing.
CODE_TOOL = re.compile(r"(?i)exec|bash|shell|python")
FACT_ID = re.compile(r'\\?"id\\?"\s*:\s*\\?"([a-f0-9]{64})\\?"')


def field(msg, name, default=None):
    """Messages are models from the parser and JSON objects in the model view."""
    return msg.get(name, default) if isinstance(msg, dict) else getattr(msg, name, default)


def latest_report_time(stamps):
    observed = [datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps if s]
    return max(observed).astimezone(UTC).isoformat() if observed else None


def recalled_ids(content):
    """Best-effort IDs from structured responses; unknown formats still taint the report."""
    found = set()

    def visit(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            for fact in value.get("facts", []) if isinstance(value.get("facts"), list) else []:
                if isinstance(fact, dict):
                    nested = fact.get("fact")
                    fid = fact.get("id") or (nested.get("id") if isinstance(nested, dict) else None)
                    if isinstance(fid, str) and re.fullmatch(r"[a-f0-9]{64}", fid):
                        found.add(fid)
            # A record in a lane of the full view; an entity has a kind, not a relation.
            fid = value.get("id")
            if (
                isinstance(fid, str)
                and re.fullmatch(r"[a-f0-9]{64}", fid)
                and ("relation" in value or "lane" in value)
            ):
                found.add(fid)
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                visit(json.loads(value), depth + 1)
            except (ValueError, RecursionError):
                pass

    visit(content)
    if not found:
        for line in content.splitlines():
            visit(line)
    if not found:
        # Lane-keyed full views and output split into chunks still name their facts.
        found.update(FACT_ID.findall(content))
    return sorted(found)[:200]


def process_memory(name):
    """Whole words of the tool name, so stateful_memory is not a statistic."""
    words = re.split(r"[_.\-]+", name.lower())
    return any(w in PROCESS_MEMORY or w.startswith("alloc") for w in words)


def memory_read(msg):
    """A read of this or any other memory tool, or of a memory-bank note. The
    parser's memory_read label also covers process-memory statistics and code
    under a memory directory; those do not taint the report that follows."""
    name = field(msg, "tool_name") or ""
    if MEMORY_CALL.search(name):
        return True
    if field(msg, "source_type") != "memory_read":
        return False
    if not name:
        return True
    if MEMORY_LABEL in field(msg, "gaps", []):
        return not process_memory(name)
    return any(
        field(t, "operation") == "read" and MEMORY_FILE.search(field(t, "path") or "")
        for t in field(msg, "touches", [])
    )


def read_results(messages):
    calls = set()
    reads = set()
    for msg in messages:
        name = field(msg, "tool_name") or ""
        if field(msg, "source_type") == "tool_call" and (
            MEMORY_CALL.search(name)
            or (CODE_TOOL.search(name) and NESTED_READ.search(field(msg, "content", "")))
            or name in DELEGATION
        ):
            if field(msg, "call_id"):
                calls.add(field(msg, "call_id"))
        if field(msg, "role") == "tool" and (
            field(msg, "call_id") in calls or name in DELEGATION or memory_read(msg)
        ):
            reads.add(field(msg, "id"))
    return reads


def report_origins(messages):
    """Track a report's memory/delegation inputs across batches within one turn.
    Every user-role message starts a turn, including the delegated or automated
    instructions the parser holds back as context."""
    reads = read_results(messages)
    result_ids, fact_ids, origins = [], set(), {}
    for msg in messages:
        if field(msg, "role") == "user":
            result_ids, fact_ids = [], set()
        if field(msg, "id") in reads:
            result_ids.append(field(msg, "id"))
            fact_ids.update(recalled_ids(field(msg, "content", "")))
        if field(msg, "source_type") == "assistant_report" and result_ids:
            origins[field(msg, "id")] = {
                "result_ids": result_ids[-200:],
                "fact_ids": sorted(fact_ids)[:200],
            }
    return origins


def fresh_results(transcript):
    reads = read_results(transcript.messages)
    return {
        m.id
        for m in transcript.messages
        if m.source_type == "tool_result" and m.tool_failed is not True and m.id not in reads
    }
