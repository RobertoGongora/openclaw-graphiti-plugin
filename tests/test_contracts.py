import json
from datetime import datetime

import pytest

from graph_memory.importers import transcripts
from graph_memory.mcp import PREFIX, VERSION, Protocol
from graph_memory.models import Extraction
from graph_memory.service import MemoryService
from graph_memory.temporal import project


def rpc(method="tools/list", **params):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "_meta": {PREFIX + "protocolVersion": VERSION, PREFIX + "clientCapabilities": {}},
            **params,
        },
    }


def headers(message):
    h = {"mcp-protocol-version": VERSION, "mcp-method": message["method"]}
    if message["method"] == "tools/call":
        h["mcp-name"] = message["params"]["name"]
    return h


def test_mcp_stateless_discovery_metadata_headers_and_catalog():
    protocol = Protocol(MemoryService(None), namespace="personal")
    message = rpc()
    status, response = protocol.dispatch(message, headers(message))
    assert status == 200
    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] > 0
    names = [t["name"] for t in result["tools"]]
    assert names == sorted(names)
    assert set(names) == {
        "memory_ingest",
        "memory_recall",
        "memory_latest",
        "memory_merge",
        "memory_retract",
        "memory_confirm",
        "memory_render",
        "memory_evidence",
        "memory_search_entities",
        "memory_status",
    }
    ingest_schema = next(t["inputSchema"] for t in result["tools"] if t["name"] == "memory_ingest")
    assert "extract" not in ingest_schema["properties"]
    for internal in set(protocol.service.tools()) - set(names):
        attempted = rpc("tools/call", name=internal, arguments={"namespace": "personal"})
        assert protocol.dispatch(attempted)[1]["error"]["code"] == -32602
    # Fresh protocol instance, no initialize call or session id.
    discover = rpc("server/discover")
    assert Protocol(MemoryService(None)).dispatch(discover, headers(discover))[1]["result"][
        "supportedVersions"
    ] == [VERSION]
    assert protocol.dispatch(message, {})[1]["error"]["code"] == -32020
    message["params"]["_meta"][PREFIX + "protocolVersion"] = "wrong"
    assert protocol.dispatch(message, headers(message))[1]["error"]["code"] == -32022
    unauthorized = rpc(
        "tools/call", name="memory_recall", arguments={"namespace": "other", "query": "Atlas"}
    )
    assert protocol.dispatch(unauthorized, headers(unauthorized))[0] == 403
    malformed = rpc()
    malformed["params"] = []
    assert protocol.dispatch(malformed, headers(malformed))[0] == 400


def test_model_rejects_untyped_relationships_and_missing_event_time():
    raw = {
        "entities": [
            {"key": "habit", "name": "Habit", "kind": "habit"},
            {"key": "event", "name": "Event", "kind": "event"},
        ],
        "facts": [
            {
                "subject": "habit",
                "target": "event",
                "relation": "occurred",
                "summary": "Did habit",
                "evidence": [{"message_id": "m", "quote": "Did habit"}],
            }
        ],
    }
    with pytest.raises(ValueError, match="occurrence time"):
        Extraction.model_validate(raw)
    raw["facts"][0]["relation"] = "uses_database"
    with pytest.raises(ValueError, match="target kind"):
        Extraction.model_validate(raw)


def test_import_preserves_timestamp_ignores_duplicate_events_and_redacts(tmp_path):
    path = tmp_path / "transcript.jsonl"
    message = {
        "type": "response_item",
        "timestamp": "2026-09-14T10:00:00Z",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Did pushups. api_key=private-value"}],
        },
    }
    path.write_text(
        json.dumps(message)
        + "\n"
        + json.dumps({"type": "event_msg", "payload": {"message": "duplicate"}})
        + "\n"
    )
    result = list(transcripts(path, "test"))
    assert len(result) == 1
    assert len(result[0].messages) == 1
    assert "private-value" not in result[0].messages[0].content
    assert result[0].messages[0].timestamp.year == 2026
    note = tmp_path / "memory.md"
    note.write_text("Atlas uses MySQL")
    assert list(transcripts(note, "test"))[0].messages[0].timestamp is None


def test_projection_permutation_invariant_and_undated_not_current():
    from itertools import permutations

    base = {
        "subject": "atlas",
        "relation": "uses_database",
        "target": "postgres",
        "slot": "primary",
        "status": "active",
    }
    facts = [
        {**base, "id": "old", "valid_ts": 10},
        {**base, "id": "new", "valid_ts": 30},
        {**base, "id": "plan", "status": "planned", "valid_ts": 20},
        {**base, "id": "unknown", "valid_ts": None},
    ]
    for ordered in permutations(facts):
        result = project(ordered, datetime.fromisoformat("2026-09-14T00:00:00+00:00"))
        assert result["current"][0]["id"] == "new"
        assert result["planned"] == []
        assert result["uncertain"][0]["id"] == "unknown"


def test_retrieval_only_server_rejects_writes_before_dispatch():
    protocol = Protocol(MemoryService(None), namespace="personal", read_only=True)
    names = {t["name"] for t in protocol.dispatch(rpc())[1]["result"]["tools"]}
    assert "memory_recall" in names
    assert "memory_ingest" not in names
    assert names == {
        "memory_recall",
        "memory_latest",
        "memory_render",
        "memory_evidence",
        "memory_search_entities",
        "memory_status",
    }
    attempted = rpc(
        "tools/call",
        name="memory_retract",
        arguments={"namespace": "personal", "fact_id": "f", "reason": "test"},
    )
    assert protocol.dispatch(attempted)[0] == 403


def test_session_remember_queues_without_extraction_or_internal_next_tool(graph):
    from graph_memory.models import Message, Transcript

    store, ns = graph
    protocol = Protocol(MemoryService(store), namespace=ns)
    transcript = Transcript(
        namespace=ns,
        source_id="remember",
        session_id="chat",
        messages=[Message(id="1", role="user", content="Remember Atlas uses MySQL.")],
    )
    message = rpc(
        "tools/call",
        name="memory_ingest",
        arguments={"transcript": transcript.model_dump(mode="json")},
    )
    result = protocol.dispatch(message)[1]["result"]["structuredContent"]
    assert result["status"] == "pending"
    assert result["processing"] == "queued_for_worker"
    assert result["available_for_recall"] is False
    assert "next_tool" not in result
    assert len(store.pending(ns)) == 1


def test_bound_schema_hides_scope_and_advertises_entity_not_query():
    bound = Protocol(MemoryService(None), namespace="personal")
    catalog = {t["name"]: t["inputSchema"] for t in bound.dispatch(rpc())[1]["result"]["tools"]}
    for schema in catalog.values():
        assert "namespace" not in schema.get("properties", {})
        assert "namespace" not in schema.get("required", [])
    transcript = catalog["memory_ingest"]["$defs"]["Transcript"]
    assert "namespace" not in transcript["properties"]
    assert "namespace" not in transcript["required"]
    assert "entity" in catalog["memory_recall"]["required"]
    assert "query" not in catalog["memory_recall"]["properties"]
    assert "query" in catalog["memory_search_entities"]["required"]
    unbound = Protocol(MemoryService(None))
    exposed = {t["name"]: t["inputSchema"] for t in unbound.dispatch(rpc())[1]["result"]["tools"]}
    assert "namespace" in exposed["memory_recall"]["required"]


def test_recall_legacy_query_is_accepted_but_conflicting_selectors_rejected():
    from graph_memory.retrieval import RecallView

    assert RecallView(namespace="test", query="Atlas").entity == "Atlas"
    with pytest.raises(ValueError, match="different entity"):
        RecallView(namespace="test", query="Atlas", entity="Core")
