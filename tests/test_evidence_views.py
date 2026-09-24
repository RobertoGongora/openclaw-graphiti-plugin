import json
from copy import deepcopy

from graph_memory.retrieval import EvidenceView, evidence_excerpt


def test_index_and_compact_bound_text_without_masquerading_as_exact_quotes():
    quote = {
        "message_id": "m",
        "source_message_id": "stored-m",
        "role": "tool",
        "quote": "original " * 1000,
        "message_available": True,
        "source_context": {
            "kind": "memory_derived_report",
            "gaps": ["excerpt"],
            "tool_call": {"arguments": "large" * 1000},
        },
        "memory_origin": {"result_ids": ["r"]},
    }
    item = {
        "fact": {
            "id": "f",
            "summary": "claim" * 1000,
            "subject": "person:user",
            "relation": "prefers",
            "target": "topic:evidence",
            "status": "uncertain",
        },
        "claims": [quote] * 5,
        "validation": [],
        "source": {"id": "episode", "source_uri": "/private/source"},
        "source_available": True,
    }
    original = deepcopy(item)
    compact = evidence_excerpt(item, "compact")
    assert len(compact["fact"]["summary"]) == 240
    assert compact["fact"]["summary_truncated"]
    assert compact["claims_truncated"] and compact["counts"]["claims"] == 5
    assert len(compact["claims"]) == 3
    q = compact["claims"][0]
    assert "quote" not in q and len(q["quote_excerpt"]) == 400 and q["quote_truncated"]
    assert q["memory_derived"] and q["has_source_gaps"]
    index = evidence_excerpt(item, "index")
    assert "claims" not in index
    assert index["sources"] == [{"id": "stored-m", "role": "tool", "kind": "memory_derived_report"}]
    assert len(json.dumps(index)) < len(json.dumps(compact)) < len(json.dumps(item))
    assert item == original
    assert EvidenceView(namespace="test", fact_ids=["f"]).detail == "compact"
