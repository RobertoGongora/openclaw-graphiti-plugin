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

    result = candidate()
    result.facts[0].status = "uncertain"
    result.facts[0].valid_at = None
    result.validate_evidence(transcript)
    from hashlib import sha256

    from graph_memory.models import EpisodeRequest
    from graph_memory.service import MemoryService

    class Model:
        def generate(self, instructions, payload, schema):
            assert (
                sha256(instructions.encode()).hexdigest()
                == "fa0153222d2b667b6a732cc994eda13029e6eb67fc0ff2386095171d98a0f7fd"
            )
            assert payload["transcript"]["messages"][1]["content"].startswith(
                "[Context text omitted"
            )
            return result

    assert (
        MemoryService(store, Model()).extract(EpisodeRequest(namespace=ns, episode_id=eid))[
            "status"
        ]
        == "complete"
    )
    saved = json.loads(store.episode(ns, eid)["payload"])
    assert saved["messages"][1]["content"] == "Opaque original tool output: Postgres"


def test_model_view_keeps_read_ids_in_batch_and_labels_memory_results():
    import json

    fid = "f" * 64
    tool = "mcp__graph_memory__memory_evidence"
    payload = {
        "transcript": {
            "source_format": "session-records-v1",
            "memory_origins": {
                "report": {"result_ids": ["elsewhere", "quotes"], "fact_ids": [fid]}
            },
            "messages": [
                {
                    "id": "call",
                    "role": "assistant",
                    "source_type": "tool_call",
                    "content": "{}",
                    "call_id": "c",
                    "tool_name": tool,
                },
                {
                    "id": "quotes",
                    "role": "tool",
                    "source_type": "tool_result",
                    "content": "quotes",
                    "call_id": "c",
                    "tool_name": tool,
                },
                {
                    "id": "agent",
                    "role": "tool",
                    "source_type": "tool_result",
                    "content": "Sub-agent report",
                    "call_id": "d",
                    "tool_name": "Agent",
                },
                {
                    "id": "fresh",
                    "role": "tool",
                    "source_type": "tool_result",
                    "content": "MySQL 8.0.36",
                    "call_id": "e",
                    "tool_name": "exec_command",
                },
                {
                    "id": "report",
                    "role": "assistant",
                    "source_type": "assistant_report",
                    "content": "Atlas uses MySQL.",
                },
            ],
        },
        "existing_entities": [],
        "existing_relationships": [],
    }
    before = deepcopy(payload)
    view = extraction_payload(payload)
    assert payload == before
    # Which reports repeat memory, and which reads in this batch they follow.
    assert view["transcript"]["memory_origins"] == {"report": {"result_ids": ["quotes"]}}
    assert fid not in json.dumps(view)
    kinds = {m["id"]: m["source_type"] for m in view["transcript"]["messages"]}
    # The validator will not accept these as corroboration; the model sees the same label.
    assert kinds["quotes"] == "memory_read" and kinds["agent"] == "memory_read"
    assert kinds["fresh"] == "tool_result" and kinds["report"] == "assistant_report"
