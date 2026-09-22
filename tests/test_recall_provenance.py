import json

import pytest

from graph_memory.models import Evidence, Message, Transcript
from graph_memory.recall_provenance import latest_report_time, report_origins
from graph_memory.service import MemoryService
from graph_memory.session_sources import feed_records, records
from tests.test_session_sources import codex, extraction


def message(mid, kind, content, **kwargs):
    return Message(
        id=mid,
        role="tool" if kind in {"tool_result", "memory_read", "context"} else "assistant",
        source_type=kind,
        content=content,
        **kwargs,
    )


def transcript(messages, **kwargs):
    return Transcript(
        namespace="test",
        source_id="source",
        session_id="session",
        source_format="session-records-v1",
        messages=messages,
        **kwargs,
    )


@pytest.mark.parametrize(
    "name",
    ["mcp__graph_memory__memory_recall", "mcp__graph_memory__memory_evidence", "functions.exec"],
)
def test_recalled_report_is_not_a_new_fact_even_when_uncertain(name):
    fid = "a" * 64
    t = transcript(
        [
            message(
                "call",
                "tool_call",
                "await tools.mcp__graph_memory__memory_recall({entity:'Atlas'})",
                call_id="c",
                tool_name=name,
            ),
            message(
                "read",
                "tool_result",
                json.dumps({"facts": [{"id": fid, "text": "Atlas uses MySQL."}]}),
                call_id="c",
                tool_name=name,
            ),
            message("report", "assistant_report", "Atlas uses MySQL."),
        ],
        focus_message_ids=["report"],
    )
    assert t.memory_origins["report"].result_ids == ["read"]
    assert t.memory_origins["report"].fact_ids == [fid]
    assert not t.can_yield_facts()
    with pytest.raises(ValueError, match="memory-derived report"):
        extraction("report", status="uncertain").validate_evidence(t)
    candidate = extraction("report", status="active", valid_at="2026-09-01T00:00:00Z")
    candidate.facts[0].validation_evidence = [
        Evidence(message_id="read", quote="Atlas uses MySQL.")
    ]
    with pytest.raises(ValueError, match="memory-derived report"):
        candidate.validate_evidence(t)


def test_fresh_work_and_user_corrections_survive_memory_read():
    msgs = [
        message("read", "memory_read", "Old memory: Atlas uses Postgres."),
        message("tool", "tool_result", "Atlas uses MySQL.", tool_name="database_status"),
        message("report", "assistant_report", "Atlas uses MySQL."),
    ]
    t = transcript(msgs, focus_message_ids=["report"])
    assert t.can_yield_facts()
    candidate = extraction(
        "report",
        status="active",
        valid_at="2026-09-01T00:00:00Z",
        validation_evidence=[{"message_id": "tool", "quote": "Atlas uses MySQL."}],
    )
    candidate.validate_evidence(t)
    correction = Message(
        id="user", role="user", source_type="user_assertion", content="Atlas uses MySQL."
    )
    t = transcript([*msgs, correction], focus_message_ids=["user"])
    extraction("user", status="active", valid_at="2026-09-01T00:00:00Z").validate_evidence(t)
    assert "user" not in t.memory_origins


def test_new_unvalidated_agent_claim_without_memory_still_survives():
    t = transcript([message("report", "assistant_report", "Atlas uses MySQL.")])
    assert t.can_yield_facts()
    extraction("report", status="uncertain").validate_evidence(t)


def test_old_user_context_cannot_launder_a_new_memory_echo():
    t = transcript(
        [
            Message(
                id="old", role="user", source_type="user_assertion", content="Atlas uses MySQL."
            ),
            message("read", "memory_read", "remembered"),
            message("report", "assistant_report", "Atlas uses MySQL."),
        ],
        focus_message_ids=["report"],
    )
    candidate = extraction("report", status="uncertain")
    candidate.facts[0].evidence.append(Evidence(message_id="old", quote="Atlas uses MySQL."))
    with pytest.raises(ValueError, match="memory-derived report"):
        candidate.validate_evidence(t)


def test_reading_code_that_mentions_a_memory_tool_is_not_a_memory_call():
    t = transcript(
        [
            message(
                "call",
                "tool_call",
                'rg "memory_recall" graph_memory',
                tool_name="exec_command",
                call_id="c",
            ),
            message("output", "tool_result", "source code", call_id="c", tool_name="exec_command"),
            message("report", "assistant_report", "New implementation finding."),
        ]
    )
    assert not t.memory_origins


def test_malformed_nested_fact_shape_does_not_break_intake():
    t = transcript(
        [
            message("read", "memory_read", '{"facts":[{"fact":null},{"fact":"unknown"}]}'),
            message("report", "assistant_report", "A recalled report."),
        ]
    )
    assert t.memory_origins["report"].fact_ids == []
    assert not t.can_yield_facts()


def test_report_time_compares_instants_across_timezones():
    assert (
        latest_report_time(["2026-09-22T12:00:00+03:00", "2026-09-22T10:00:00Z"])
        == "2026-09-22T10:00:00+00:00"
    )


def test_new_user_turn_resets_origins_and_unknown_memory_output_still_counts():
    msgs = [
        message("read", "memory_read", "unstructured output"),
        message("first", "assistant_report", "Remembered claim."),
        Message(
            id="user",
            role="user",
            source_type="user_assertion",
            content="Check current deployment.",
        ),
        message("second", "assistant_report", "New report."),
    ]
    assert list(report_origins(msgs)) == ["first"]


def test_source_cursor_unchanged_and_origins_cross_long_batch_boundary(graph, tmp_path):
    store, ns = graph
    p = tmp_path / "session.jsonl"
    p.write_text(
        codex(
            "function_call",
            call_id="recall",
            name="functions.exec",
            arguments="await tools.mcp__graph_memory__memory_recall({})",
        )
        + codex(
            "function_call_output",
            call_id="recall",
            output=json.dumps({"facts": [{"id": "b" * 64}]}),
        )
        + "".join(
            codex("function_call", call_id=f"f{i}", name="other_tool", arguments="{}")
            for i in range(25)
        )
        + codex("message", role="assistant", content="Atlas uses MySQL.")
    )
    before = [m.model_dump(mode="json") for m in records(p)]
    service = MemoryService(store)
    result = feed_records(service, ns, p, "child-session", max_batches=10)
    assert result["caught_up"]
    assert [m.model_dump(mode="json") for m in records(p)] == before
    last = store.episode(ns, result["receipts"][-1]["episode_id"])
    saved = Transcript.model_validate_json(last["payload"])
    report = next(m for m in saved.messages if m.source_type == "assistant_report")
    assert saved.memory_origins[report.id].fact_ids == ["b" * 64]
    assert not saved.can_yield_facts()
    assert feed_records(service, ns, p, "child-session")["receipts"] == []
    from graph_memory.models import EpisodeRequest

    assert (
        service.extract(EpisodeRequest(namespace=ns, episode_id=last["id"]))["status"] == "complete"
    )
    assert store.episode(ns, last["id"])["fact_count"] == 0
