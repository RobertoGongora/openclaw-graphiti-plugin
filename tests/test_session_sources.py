import json

import pytest

from graph_memory.journal import Journal
from graph_memory.models import Evidence, Extraction, Transcript
from graph_memory.service import MemoryService
from graph_memory.session_sources import (
    OPAQUE_CALL,
    SHELL_OUTPUT,
    before_shell_results,
    feed_records,
    records,
)
from graph_memory.store import digest
from tests.helpers import MYSQL, PROJECT


def claude(role, blocks, stamp="2026-09-16T10:00:00Z"):
    return (
        json.dumps({"type": role, "timestamp": stamp, "message": {"role": role, "content": blocks}})
        + "\n"
    )


def codex(t, **props):
    return (
        json.dumps(
            {
                "type": "response_item",
                "timestamp": "2026-09-16T10:00:00Z",
                "payload": {"type": t, **props},
            }
        )
        + "\n"
    )


def extraction(mid, quote="Atlas uses MySQL.", **props):
    return Extraction(
        entities=[PROJECT, MYSQL],
        facts=[
            {
                "subject": PROJECT["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "summary": quote,
                "evidence": [{"message_id": mid, "quote": quote}],
                **props,
            }
        ],
    )


def test_tools_pair_and_historical_memory_never_reads_current_file(tmp_path):
    memory = tmp_path / "memory" / "state.md"
    memory.parent.mkdir()
    memory.write_text("NEW CONTENT MUST NOT BE READ")
    p = tmp_path / "session.jsonl"
    p.write_text(
        claude(
            "assistant",
            [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": str(memory)}}],
        )
        + claude(
            "user",
            [{"type": "tool_result", "tool_use_id": "a", "content": "Atlas used MySQL in 2024."}],
        )
        + claude(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "b",
                    "name": "Edit",
                    "input": {
                        "file_path": str(memory),
                        "old_string": "MySQL",
                        "new_string": "Postgres",
                    },
                }
            ],
        )
        + claude("user", [{"type": "tool_result", "tool_use_id": "b", "content": "Success"}])
    )
    ms = list(records(p))
    assert [m.source_type for m in ms] == ["tool_call", "memory_read", "tool_call", "memory_write"]
    assert ms[1].call_id == ms[0].call_id
    assert ms[1].touches[0].content == "Atlas used MySQL in 2024."
    assert ms[1].touches[0].captured == "excerpt"
    assert "old_string" in ms[3].touches[0].content
    assert "NEW CONTENT" not in json.dumps([m.model_dump(mode="json") for m in ms])


def test_codex_functions_custom_outputs_compaction_and_no_event_duplicates(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        codex("message", role="user", content=[{"type": "input_text", "text": "Atlas uses MySQL."}])
        + codex("function_call", call_id="c", name="db_status", arguments="{}")
        + codex("function_call_output", call_id="c", output="MySQL online")
        + codex(
            "custom_tool_call",
            call_id="p",
            name="apply_patch",
            input="*** Update File: /bank/memory/state.md\n-MySQL\n+Postgres",
        )
        + codex("custom_tool_call_output", call_id="p", output="Success")
        + json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "DUPLICATE"}}
        )
        + "\n"
        + json.dumps({"type": "compacted", "message": "Atlas used SQLite."})
        + "\n"
        + codex("reasoning", summary="PRIVATE REASONING")
        + codex("message", role="developer", content="NOT EVIDENCE")
    )
    ms = list(records(p))
    assert len(ms) == 6
    assert ms[2].source_type == "tool_result"
    assert ms[4].source_type == "memory_write"
    assert ms[5].source_type == "context"
    assert all("DUPLICATE" not in m.content and "PRIVATE REASONING" not in m.content for m in ms)


@pytest.mark.parametrize(
    "source", ["tool_result", "memory_read", "memory_write", "context", "tool_call"]
)
def test_outputs_and_artifacts_cannot_originate_facts(source):
    t = Transcript(
        namespace="test",
        source_id="s",
        session_id="s",
        source_format="session-records-v1",
        messages=[
            {"id": "m", "role": "tool", "source_type": source, "content": "Atlas uses MySQL."}
        ],
    )
    with pytest.raises(ValueError, match="conversational claim"):
        extraction("m", status="uncertain").validate_evidence(t)


def test_assistant_claim_requires_validation_and_memory_read_cannot_validate():
    t = Transcript(
        namespace="test",
        source_id="s",
        session_id="s",
        source_format="session-records-v1",
        messages=[
            {
                "id": "a",
                "role": "assistant",
                "source_type": "assistant_report",
                "content": "Atlas uses MySQL.",
            },
            {"id": "v", "role": "tool", "source_type": "memory_read", "content": "MySQL online"},
        ],
    )
    e = extraction("a", valid_at="2026-09-16T10:00:00Z")
    e.facts[0].validation_evidence.append(Evidence(message_id="v", quote="MySQL online"))
    e = Extraction.model_validate(e.model_dump())
    with pytest.raises(ValueError, match="unvalidated"):
        e.validate_evidence(t)
    t.messages[1].source_type = "tool_result"
    e.validate_evidence(t)
    t.messages[1].tool_failed = True
    with pytest.raises(ValueError, match="unvalidated"):
        e.validate_evidence(t)


def test_shell_output_is_a_result_unless_the_command_names_memory(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        codex("custom_tool_call", call_id="c", name="exec", input="run some JS")
        + codex("custom_tool_call_output", call_id="c", output="Atlas uses MySQL.")
        + codex("function_call_output", call_id="missing", output="Success")
        + codex("custom_tool_call", call_id="m", name="exec", input="graph-memory recall atlas")
        + codex("custom_tool_call_output", call_id="m", output="Atlas uses MySQL.")
    )
    ms = list(records(p))
    assert (ms[1].source_type, ms[1].gaps) == ("tool_result", [SHELL_OUTPUT])
    assert ms[2].source_type == ms[4].source_type == "context"
    assert ms[2].gaps and ms[4].gaps == [OPAQUE_CALL]


def test_cursor_written_before_shell_results_resumes(graph, tmp_path):
    store, ns = graph
    p = tmp_path / "s.jsonl"
    p.write_text(
        claude("user", "Atlas uses MySQL.")
        + codex("custom_tool_call", call_id="c", name="exec", input="make test")
        + codex("custom_tool_call_output", call_id="c", output="12 passed")
    )
    service = MemoryService(store)
    fid = feed_records(service, ns, p, "s")["feed_id"]
    old = [before_shell_results(m.model_dump(mode="json")) for m in records(p)]
    assert old[2]["source_type"] == "context" and old[2]["gaps"] == [OPAQUE_CALL]
    store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {id:$id}) SET f.prefix_hash=$hash", id=fid, hash=digest(old)
        ).consume()
    )
    with p.open("a") as f:
        f.write(claude("assistant", "The tests pass."))
    assert feed_records(service, ns, p, "s")["receipts"]


def test_report_sees_the_successful_results_of_its_turn(graph, tmp_path):
    store, ns = graph
    p = tmp_path / "s.jsonl"
    lines = [
        codex("function_call", call_id="before", name="status", arguments="{}"),
        codex("function_call_output", call_id="before", output="an earlier turn"),
        claude("user", "Run the tests."),
        codex("custom_tool_call", call_id="run", name="exec", input="make test"),
        codex("custom_tool_call_output", call_id="run", output="12 passed"),
        claude("assistant", [{"type": "tool_use", "id": "bad", "name": "Grep", "input": {}}]),
        claude(
            "user",
            [{"type": "tool_result", "tool_use_id": "bad", "is_error": True, "content": "boom"}],
        ),
    ]
    lines += [claude("assistant", f"Working on step {i}.") for i in range(12)]
    lines += [claude("assistant", "The tests pass.")]
    p.write_text("".join(lines))
    service = MemoryService(store)
    r = feed_records(service, ns, p, "s", max_batches=10)
    t = Transcript.model_validate_json(
        store.episode(ns, r["receipts"][-1]["episode_id"])["payload"]
    )
    carried = {m.call_id: m.source_type for m in t.messages if m.id not in t.focus_message_ids}
    assert [m.content for m in t.messages if m.call_id == "run"] == ["make test", "12 passed"]
    assert "before" not in carried and "bad" not in carried
    assert [m.id for m in t.messages] == sorted(
        (m.id for m in t.messages), key=lambda i: [int(x) for x in i.split("-")[1::2]]
    )


def test_record_chunks_preserve_all_content_and_partial_line(tmp_path):
    p = tmp_path / "s.jsonl"
    content = "x" * 60_000
    p.write_text(claude("user", content) + '{"partial"')
    ms = list(records(p))
    assert "".join(m.content for m in ms) == content
    assert len({m.id for m in ms}) == 3


@pytest.mark.parametrize(
    "content, offsets",
    [
        ("x" * 24_000 + " ", [0]),
        ("x" * 24_000 + " " * 24_000 + "y", [0, 48_000]),
        (" \n\t", [0]),
    ],
)
def test_whitespace_chunks_do_not_reject_tool_records(tmp_path, content, offsets):
    p = tmp_path / "s.jsonl"
    p.write_text(codex("function_call_output", call_id="call-1", output=content))
    messages = list(records(p))
    assert [m.id for m in messages] == [f"line-1-block-0-{n}" for n in offsets]
    assert all(m.content.strip() for m in messages)
    if content.strip():
        assert "".join(m.content for m in messages) == content.replace(" ", "")
    else:
        assert messages[0].content == "[Empty tool or source record]"


def test_feed_restart_append_rewrite_and_provenance_replay(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    p = tmp_path / "s.jsonl"
    p.write_text(
        claude("user", "Atlas uses MySQL.")
        + claude(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "r",
                    "name": "Read",
                    "input": {"file_path": "/old/memory/db.md"},
                }
            ],
        )
        + claude(
            "user", [{"type": "tool_result", "tool_use_id": "r", "content": "Atlas used SQLite."}]
        )
    )
    first = feed_records(service, ns, p, "s")
    eid = first["receipts"][0]["episode_id"]
    assert feed_records(service, ns, p, "s")["receipts"] == []
    assert first["caught_up"]
    source = Transcript.model_validate_json(store.episode(ns, eid)["payload"])
    e = extraction(source.messages[0].id, valid_at="2026-09-16T10:00:00Z")
    store.commit(ns, eid, e)
    assert store.recall(ns, "Atlas")["current"][0]["target"] == MYSQL["key"]
    counts = store.transaction(
        lambda tx: tx.run(
            "MATCH (n {namespace:$ns}) RETURN labels(n)[0] AS kind,count(n) AS count", ns=ns
        ).data()
    )
    assert {r["kind"]: r["count"] for r in counts}["MemoryArtifactObservation"] == 2
    assert Journal(store).verify(ns)["verified"]
    target = "replay:" + ns
    try:
        Journal(store).replay(ns, target)
        assert Journal(store).verify(target)["verified"]
        assert (
            store.transaction(
                lambda tx: tx.run(
                    "MATCH (:MemoryFact {namespace:$ns})-[:CITES]->(:MemoryMessage {namespace:$ns}) RETURN count(*) AS c",
                    ns=target,
                ).single()["c"]
            )
            == 1
        )
    finally:
        store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR n.scope=$ns OR n.id=$ns DETACH DELETE n",
                ns=target,
            ).consume()
        )
    with p.open("a") as f:
        f.write(claude("user", "Migration to Postgres is only planned."))
    second = feed_records(service, ns, p, "s")
    assert len(second["receipts"]) == 1
    assert feed_records(service, ns, p, "s")["receipts"] == []
    p.write_text(claude("user", "Changed past"))
    with pytest.raises(ValueError, match="prefix changed"):
        feed_records(service, ns, p, "s")


def test_bounded_feed_and_late_tool_pair(graph, tmp_path):
    store, ns = graph
    p = tmp_path / "s.jsonl"
    lines = [codex("function_call", call_id="long", name="status", arguments="{}")]
    lines += [claude("user", f"User statement {i}") for i in range(20)]
    lines += [codex("function_call_output", call_id="long", output="success")]
    p.write_text("".join(lines))
    service = MemoryService(store)
    r = feed_records(service, ns, p, "s", max_batches=1)
    assert not r["caught_up"]
    while not r["caught_up"]:
        r = feed_records(service, ns, p, "s", max_batches=1)
    t = Transcript.model_validate_json(
        store.episode(ns, r["receipts"][-1]["episode_id"])["payload"]
    )
    assert any(m.source_type == "tool_call" and m.call_id == "long" for m in t.messages)
    assert len(t.focus_message_ids) <= 8


def test_nested_exec_memory_reads_are_references_not_fabricated_file_versions(tmp_path):
    p = tmp_path / "s.jsonl"
    code = "text(await tools.exec_command({cmd:\"sed -n '1,8p' /Users/rob/.codex/memories/MEMORY.md; cat /bank/memory/a.md\"}));"
    p.write_text(
        codex("custom_tool_call", call_id="nested", name="functions.exec", input=code)
        + codex("custom_tool_call_output", call_id="nested", output="mixed command output")
    )
    ms = list(records(p))
    assert len(ms[1].touches) == 2
    assert ms[1].source_type == "memory_read"
    assert all(t.captured == "unavailable" and t.content == "" for t in ms[1].touches)
    assert ms[1].content == "mixed command output"


def test_relative_artifact_resolves_against_recorded_cwd(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/historical/project"}})
        + "\n"
        + codex(
            "function_call",
            call_id="r",
            name="Read",
            arguments=json.dumps({"file_path": "memory/state.md"}),
        )
        + codex("function_call_output", call_id="r", output="old content")
    )
    # Relative memory/ paths must also be recognized.
    ms = list(records(p))
    assert ms[1].touches[0].path == "/historical/project/memory/state.md"


def test_memory_mcp_retrieval_is_not_fresh_verification(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        codex("function_call", call_id="r", name="mcp__graph__memory_recall", arguments="{}")
        + codex("function_call_output", call_id="r", output="Atlas uses MySQL.")
    )
    ms = list(records(p))
    assert ms[1].source_type == "memory_read"
    assert "derived_memory_retrieval_not_fresh_verification" in ms[1].gaps


@pytest.mark.parametrize("origin", [{"subagent": "review"}, "exec"])
def test_codex_delegated_and_automated_prompts_are_not_human_claims(tmp_path, origin):
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"type": "session_meta", "payload": {"source": origin}})
        + "\n"
        + codex("message", role="user", content="Atlas uses MySQL.")
    )
    assert list(records(p))[0].source_type == "context"


def test_claude_sidechain_instruction_is_context(tmp_path):
    p = tmp_path / "s.jsonl"
    r = json.loads(claude("user", "Atlas uses MySQL."))
    r["isSidechain"] = True
    p.write_text(json.dumps(r) + "\n")
    assert list(records(p))[0].source_type == "context"
