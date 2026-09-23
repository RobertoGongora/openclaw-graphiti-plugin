"""Resolve MCP-supplied evidence against engine-ingested source messages."""

from . import models as m


def prepare(store, request):
    transcript = request.transcript
    submitted = {msg.id: msg for msg in transcript.messages}
    if not set(request.sources) <= submitted.keys():
        raise ValueError("Source references must identify submitted message IDs")
    sources = {}
    if request.sources:
        rows = store.transaction(
            lambda tx: tx.run(
                "MATCH (s:MemoryMessage {namespace:$ns}) WHERE s.id IN $ids "
                "RETURN properties(s) AS source",
                ns=transcript.namespace,
                ids=list(set(request.sources.values())),
            ).data()
        )
        sources = {row["source"]["id"]: row["source"] for row in rows}
    messages = []
    origins = {}
    for msg in transcript.messages:
        ref = request.sources.get(msg.id)
        if ref:
            source = sources.get(ref)
            if source is None or msg.content not in source["content"]:
                raise ValueError(
                    "Source reference must exist in this namespace and quote its source message exactly"
                )
            # Caller-provided roles, timestamps, successes and artifact observations
            # cannot override the independently ingested source record.
            messages.append(
                m.Message(
                    id=msg.id,
                    content=source["content"],
                    role=source["role"],
                    source_type=source["source_type"],
                    timestamp=source.get("timestamp"),
                    call_id=source.get("call_id"),
                    tool_name=source.get("tool_name"),
                    tool_failed=source.get("tool_failed"),
                    gaps=source.get("gaps", []),
                )
            )
            if source.get("memory_read_refs"):
                origins[msg.id] = m.RecallOrigin(
                    result_ids=source["memory_read_refs"],
                    fact_ids=source.get("recalled_fact_ids", []),
                )
        else:
            messages.append(
                m.Message(
                    id=msg.id,
                    content=msg.content,
                    role="assistant",
                    source_type="context",
                    timestamp=msg.timestamp,
                    gaps=["direct_write_without_verified_source"],
                )
            )
    return m.Transcript.model_validate(
        {
            **transcript.model_dump(),
            "source_format": "direct-mcp-v1",
            "source_kind": "transcript",
            "verified_source_refs": dict(request.sources),
            "messages": messages,
            "memory_origins": origins,
            "source_created_at": None,
            "source_updated_at": None,
        }
    )
