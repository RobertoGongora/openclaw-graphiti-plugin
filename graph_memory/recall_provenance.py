"""Read provenance, never fresh evidence. No source bytes or cursor hashes change."""

import json
import re
from datetime import UTC, datetime

MEMORY_CALL = re.compile(r"memory_(?:recall|latest|evidence|search_entities|render|status)\b")
NESTED_READ = re.compile(
    r"\btools\.[\w.]*memory_(?:recall|latest|evidence|search_entities|render|status)\s*\("
)
DELEGATION = re.compile(r"(?:spawn_agent|wait_agent|send_message|followup_task)\b")


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
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                visit(json.loads(value), depth + 1)
            except ValueError:
                pass

    visit(content)
    if not found:
        for line in content.splitlines():
            visit(line)
    return sorted(found)[:200]


def read_results(messages):
    calls = set()
    reads = set()
    for msg in messages:
        if msg.source_type == "tool_call" and (
            MEMORY_CALL.search(msg.tool_name or "")
            or NESTED_READ.search(msg.content)
            or DELEGATION.search(msg.tool_name or "")
        ):
            if msg.call_id:
                calls.add(msg.call_id)
        if msg.role == "tool" and (
            msg.source_type == "memory_read"
            or msg.call_id in calls
            or MEMORY_CALL.search(msg.tool_name or "")
            or "delegated_agent_report_not_execution_evidence" in msg.gaps
        ):
            reads.add(msg.id)
    return reads


def report_origins(messages):
    """Track a report's memory/delegation inputs across batches within a user turn."""
    reads = read_results(messages)
    result_ids, fact_ids, origins = [], set(), {}
    for msg in messages:
        if msg.source_type == "user_assertion":
            result_ids, fact_ids = [], set()
        if msg.id in reads:
            result_ids.append(msg.id)
            fact_ids.update(recalled_ids(msg.content))
        if msg.source_type == "assistant_report" and result_ids:
            origins[msg.id] = {
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
