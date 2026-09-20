from copy import deepcopy

from graph_memory.extraction_policy import extraction_payload


def test_model_view_preserves_claims_validation_and_durable_input():
    kinds = [
        "assistant_report",
        "user_assertion",
        "tool_result",
        "tool_call",
        "memory_read",
        "memory_write",
        "context",
    ]
    payload = {
        "transcript": {
            "source_format": "session-records-v1",
            "messages": [
                {
                    "id": k,
                    "source_type": k,
                    "content": k + " original",
                    "timestamp": "2026-09-20T10:00:00Z",
                }
                for k in kinds
            ],
        },
        "existing_entities": [{"key": "project:a"}],
    }
    before = deepcopy(payload)
    view = extraction_payload(payload)
    assert payload == before
    assert view["existing_entities"] == before["existing_entities"]
    assert view["transcript"]["messages"][:-1] == before["transcript"]["messages"][:-1]
    context = view["transcript"]["messages"][-1]
    assert context["content"] != "context original"
    assert context["id"] == "context"
    assert context["timestamp"] == "2026-09-20T10:00:00Z"
    payload["transcript"]["source_format"] = None
    assert extraction_payload(payload) == payload


def test_reduced_view_and_committed_fact_keep_original_evidence(graph):
    import json

    from graph_memory.models import Transcript
    from tests.test_retry_feedback import candidate

    store, ns = graph
    transcript = Transcript(
        namespace=ns,
        source_id="reduced",
        session_id="reduced",
        source_format="session-records-v1",
        messages=[
            {
                "id": "m1",
                "role": "assistant",
                "source_type": "assistant_report",
                "content": "Atlas uses MySQL.",
            },
            {
                "id": "c",
                "role": "tool",
                "source_type": "context",
                "content": "Opaque original tool output: Postgres",
            },
        ],
    )
    eid = store.stage(transcript)["episode_id"]

    view = extraction_payload({"transcript": transcript.model_dump(mode="json")})
    assert view["transcript"]["messages"][1]["content"].startswith("[Context text omitted")
    result = candidate()
    result.facts[0].status = "uncertain"
    result.facts[0].valid_at = None
    result.validate_evidence(transcript)
    assert store.commit(ns, eid, result)["status"] == "complete"
    saved = json.loads(store.episode(ns, eid)["payload"])
    assert saved["messages"][1]["content"] == "Opaque original tool output: Postgres"
