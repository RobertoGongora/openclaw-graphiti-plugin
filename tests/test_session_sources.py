import json

import pytest

from graph_memory.journal import Journal
from graph_memory.models import Evidence, Extraction, Transcript
from graph_memory.service import MemoryService
from graph_memory.session_sources import (
    OPAQUE_CALL,
    SHELL_OUTPUT,
    before_shell_results,
    cursor_entries,
    cursor_matches,
    episode_format,
    feed_records,
    is_cursor_format,
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
                "timestamp": "2026-09-16T10:00:00Z",
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


def grok_transcript(messages, focus=None, source_format="grok-bot"):
    return Transcript(
        namespace="transcripts",
        source_id="grok",
        session_id="grok",
        source_format=source_format,
        messages=messages,
        focus_message_ids=focus if focus is not None else [m.id for m in messages],
    )


def test_claude_dumps_omit_empty_grok_speaker_fields(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(claude("user", "Atlas uses MySQL."))
    dumped = list(records(p))[0].model_dump(mode="json")
    assert "from_agent" not in dumped
    assert "channel" not in dumped


def test_grok_bot_feed_yields_facts_and_relays_are_not_user_assertions(tmp_path):
    from graph_memory.extraction_policy import extraction_payload
    from graph_memory.feed_identity import parse_root, source_files
    from graph_memory.llm import extraction_instructions

    root = tmp_path / "sessions" / "grok-bot"
    path = root / "desk-agent-7.jsonl"
    path.parent.mkdir(parents=True)
    person = json.loads(claude("user", "Atlas uses MySQL.", stamp="2026-09-28T12:00:00Z"))
    person["channel"] = "desk"
    person["message"]["content"] = [
        {
            "type": "text",
            "text": "Atlas uses MySQL. token tskey-auth-kabcdefghijklmnopqrstuvwxyz",
        }
    ]
    relay = json.loads(claude("user", "Atlas uses MySQL.", stamp="2026-09-28T12:00:01Z"))
    relay["isSidechain"] = True
    relay["fromAgent"] = {"id": "fleet-agent-2", "name": "Fleet"}
    relay["channel"] = "fleet"
    report = json.loads(claude("assistant", "Atlas uses MySQL.", stamp="2026-09-28T12:01:00Z"))
    report["channel"] = "desk"
    report["fromAgent"] = {"id": "desk-agent-7", "name": "Sand"}
    path.write_text(
        json.dumps(person) + "\n" + json.dumps(relay) + "\n" + json.dumps(report) + "\n"
    )
    said, forwarded, told = list(records(path))
    assert said.source_type == "user_assertion"
    assert said.timestamp.isoformat() == "2026-09-28T12:00:00+00:00"
    assert said.channel == "desk"
    assert said.from_agent is None
    assert "tskey-auth-kabcdefghijklmnopqrstuvwxyz" not in said.content
    assert "Atlas uses MySQL." in said.content
    assert forwarded.source_type == "context"
    assert forwarded.source_type != "user_assertion"
    assert "delegated_instruction" in forwarded.gaps
    assert forwarded.from_agent == "fleet-agent-2"
    assert forwarded.channel == "fleet"
    assert told.source_type == "assistant_report"
    assert told.from_agent == "desk-agent-7"
    assert told.timestamp.isoformat() == "2026-09-28T12:01:00+00:00"

    sourced = grok_transcript([said, forwarded, told])
    assert sourced.can_yield_facts()
    extraction(said.id).validate_evidence(sourced)
    extraction(told.id, status="uncertain").validate_evidence(sourced)
    with pytest.raises(ValueError, match="unvalidated"):
        extraction(told.id).validate_evidence(sourced)
    with pytest.raises(ValueError, match="focus message|conversational claim"):
        extraction(forwarded.id).validate_evidence(sourced)
    assert grok_transcript([forwarded]).can_yield_facts() is False

    reduced = extraction_payload({"transcript": sourced.model_dump(mode="json")})
    hidden = next(m for m in reduced["transcript"]["messages"] if m["id"] == forwarded.id)
    assert hidden["content"].startswith("[Context text omitted")
    assert "user_assertion" in extraction_instructions(sourced)

    assert parse_root(root).label == "grok-bot"
    assert source_files([root])[str(path.resolve())] == "grok-bot:desk-agent-7.jsonl"
    assert episode_format(path) == "grok-bot"
    assert episode_format(tmp_path / "claude" / "s.jsonl") == "session-records-v1"


def test_flat_grok_bot_lines_keep_channel_and_from_agent_relays(tmp_path):
    path = tmp_path / "grok-bot" / "desk-agent-7.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "source_format": "grok-bot",
                "role": "user",
                "content": "Atlas uses MySQL.",
                "timestamp": "2026-09-28T12:02:00Z",
                "channel": "desk",
            }
        )
        + "\n"
        + json.dumps(
            {
                "source_format": "grok-bot",
                "role": "user",
                "content": "Atlas uses MySQL.",
                "fromAgent": {"id": "fleet-agent-2"},
                "channel": "fleet",
                "timestampMs": 1759053721000,
            }
        )
        + "\n"
        + json.dumps(
            {
                "source_format": "grok-bot",
                "role": "user",
                "content": "Atlas uses MySQL.",
                "fromUser": {"id": "roberto"},
                "channel": "desk",
                "timestamp": "2026-09-28T12:03:00Z",
            }
        )
        + "\n"
        + json.dumps({"source_format": "grok-bot", "kind": "widget", "channel": "desk"})
        + "\n"
    )
    person, relay, human = list(records(path))
    assert person.source_type == "user_assertion"
    assert person.channel == "desk"
    assert person.timestamp.isoformat() == "2026-09-28T12:02:00+00:00"
    assert relay.source_type == "context"
    assert "delegated_instruction" in relay.gaps
    assert relay.from_agent == "fleet-agent-2"
    assert relay.channel == "fleet"
    assert relay.timestamp is not None
    assert human.source_type == "user_assertion"
    assert human.from_agent is None
    assert human.channel == "desk"
    sourced = grok_transcript([person])
    assert sourced.can_yield_facts()
    extraction(person.id).validate_evidence(sourced)
    assert grok_transcript([relay]).can_yield_facts() is False


def test_cursor_written_before_current_redaction_resumes(graph, tmp_path):
    from graph_memory.importers import redact_v1
    from graph_memory.inventory import census

    store, ns = graph
    p = tmp_path / "s.jsonl"
    p.write_text(claude("user", 'Ticket PLAN-1234: {"api_key": "private-value"}'))
    service = MemoryService(store)
    fid = feed_records(service, ns, p, "s")["feed_id"]
    old = [m.model_dump(mode="json") for m in records(p, redact_v1)]
    assert old != [m.model_dump(mode="json") for m in records(p)]
    store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {id:$id}) SET f.prefix_hash=$hash", id=fid, hash=digest(old)
        ).consume()
    )
    assert cursor_matches(p, list(records(p)), 1, digest(old))
    assert census(store, ns, [tmp_path])["gaps"]["prefix_mismatches"] == 0
    with p.open("a") as f:
        f.write(claude("assistant", "Noted."))
    assert feed_records(service, ns, p, "s")["receipts"]
    # A different file is still refused.
    p.write_text(claude("user", "Something else.") + claude("assistant", "Noted."))
    with pytest.raises(ValueError, match="prefix changed"):
        feed_records(service, ns, p, "s")


def test_any_from_agent_value_marks_a_relay(tmp_path):
    path = tmp_path / "grok-bot" / "a.jsonl"
    path.parent.mkdir()
    lines = [
        {"source_format": "grok-bot", "role": "user", "content": "Deploy is done.", "fromAgent": v}
        for v in ({}, {"displayName": "Sand"}, "", " ", True, 42, ["sand"], {"id": 123})
    ]
    shaped = json.loads(claude("user", "Deploy is done."))
    shaped["fromAgent"] = {"displayName": "Sand"}
    path.write_text("".join(json.dumps(x) + "\n" for x in [*lines, shaped]))
    parsed = list(records(path))
    assert len(parsed) == 9
    for m in parsed:
        assert m.source_type == "context"
        assert "delegated_instruction" in m.gaps
        assert m.from_agent == "unknown-agent"


def test_grok_bot_bad_timestamps_do_not_stop_the_file(tmp_path):
    path = tmp_path / "grok-bot" / "a.jsonl"
    path.parent.mkdir()
    base = {"source_format": "grok-bot", "role": "user", "content": "Atlas uses MySQL."}
    path.write_text(
        "".join(
            json.dumps({**base, **extra}) + "\n"
            for extra in (
                {"timestamp": "2026-09-28T10:00:00"},
                {"timestamp": "yesterday"},
                {"timestampMs": 1e20},
                {"timestampMs": 1759053720000},
            )
        )
    )
    stamps = [m.timestamp for m in records(path)]
    assert stamps[:3] == [None, None, None]
    assert stamps[3] is not None and stamps[3].isoformat().startswith("2025-09-28T10:02:00")


def test_grok_bot_labels_are_redacted(tmp_path):
    path = tmp_path / "grok-bot" / "a.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "source_format": "grok-bot",
                "role": "assistant",
                "content": "hi",
                "fromAgent": "bot password=hunter2 ghp_abcdefghijklmnopqrstuvwxyz",
                "channel": {"name": "desk ?token=chansecret123"},
            }
        )
        + "\n"
    )
    (m,) = records(path)
    assert "hunter2" not in m.from_agent and "ghp_" not in m.from_agent
    assert "chansecret123" not in m.channel


def test_claude_string_content_without_role_keeps_the_line_type(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "Atlas uses MySQL."}}) + "\n")
    (m,) = records(p)
    assert (m.role, m.source_type) == ("user", "user_assertion")


def test_episode_format_follows_the_root_label(tmp_path):
    assert episode_format(tmp_path / "exports" / "a.jsonl", "grok-bot:a.jsonl") == "grok-bot"
    nested = tmp_path / "claude" / "grok-bot" / "s.jsonl"
    assert episode_format(nested, "claude:grok-bot/s.jsonl") == "session-records-v1"
    assert episode_format(nested) == "grok-bot"


def test_feed_staged_under_earlier_redaction_keeps_appending(graph, tmp_path, monkeypatch):
    import functools

    from graph_memory import session_sources
    from graph_memory.importers import redact_v1

    store, ns = graph
    p = tmp_path / "s.jsonl"
    p.write_text(
        claude("user", "export GITHUB_TOKEN=abcd1234efgh then run it")
        + claude("user", 'curl -H "Authorization: Bearer $HARVEST_TOKEN" x')
    )
    service = MemoryService(store)
    current = session_sources.records
    monkeypatch.setattr(session_sources, "records", functools.partial(current, scrub=redact_v1))
    fid = feed_records(service, ns, p, "s")["feed_id"]
    monkeypatch.setattr(session_sources, "records", current)
    for reply in ("First.", "Second."):
        with p.open("a") as f:
            f.write(claude("assistant", reply))
        assert feed_records(service, ns, p, "s")["receipts"]
    cursor = store.read(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {id:$id}) RETURN f.message_count AS n", id=fid
        ).single()["n"]
    )
    assert cursor == 4
    # Stored text keeps its earlier redaction; nothing was rewritten in place.
    stored = store.read(
        lambda tx: [
            r["c"]
            for r in tx.run("MATCH (m:MemoryMessage {namespace:$ns}) RETURN m.content AS c", ns=ns)
        ]
    )
    assert any("abcd1234efgh" in c for c in stored)
    # A genuinely different earlier message is still refused.
    p.write_text(
        claude("user", "export GITHUB_TOKEN=zzzz9999yyyy then run it")
        + claude("user", 'curl -H "Authorization: Bearer $HARVEST_TOKEN" x')
        + claude("assistant", "First.")
        + claude("assistant", "Second.")
        + claude("assistant", "Third.")
    )
    with pytest.raises(ValueError, match="changed"):
        feed_records(service, ns, p, "s")


def test_earlier_redaction_that_moved_a_chunk_boundary_is_refused(graph, tmp_path, monkeypatch):
    import functools

    from graph_memory import session_sources
    from graph_memory.importers import redact_v1

    store, ns = graph
    p = tmp_path / "s.jsonl"
    p.write_text(claude("assistant", "x" * 23_950 + " GITHUB_TOKEN=" + "a" * 200))
    service = MemoryService(store)
    current = session_sources.records
    monkeypatch.setattr(session_sources, "records", functools.partial(current, scrub=redact_v1))
    feed_records(service, ns, p, "s")
    monkeypatch.setattr(session_sources, "records", current)
    with p.open("a") as f:
        f.write(claude("user", "Atlas moved to Postgres."))
    # Skipping the appended message silently would be worse than refusing.
    with pytest.raises(ValueError, match="prefix changed"):
        feed_records(service, ns, p, "s")


def test_recarried_memory_write_keeps_one_observation(graph, tmp_path, monkeypatch):
    import functools

    from graph_memory import session_sources
    from graph_memory.importers import redact_v1

    store, ns = graph
    p = tmp_path / "s.jsonl"
    p.write_text(
        claude(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "w",
                    "name": "Write",
                    "input": {
                        "file_path": "/x/memory/notes.md",
                        "content": "GITHUB_TOKEN=abcd1234efgh",
                    },
                }
            ],
        )
    )
    service = MemoryService(store)
    current = session_sources.records
    monkeypatch.setattr(session_sources, "records", functools.partial(current, scrub=redact_v1))
    feed_records(service, ns, p, "s")
    monkeypatch.setattr(session_sources, "records", current)
    for reply in ("First.", "Second."):
        with p.open("a") as f:
            f.write(claude("assistant", reply))
        assert feed_records(service, ns, p, "s")["receipts"]
    count = store.read(
        lambda tx: tx.run(
            "MATCH (o:MemoryArtifactObservation {namespace:$ns}) RETURN count(o) AS n", ns=ns
        ).single()["n"]
    )
    assert count == 1


def test_grok_bot_readtranscript_entries_are_all_read(tmp_path):
    """Entry shapes as the Grok Bot app replicates them; the text is invented."""
    path = tmp_path / "grok-bot" / "desk.jsonl"
    path.parent.mkdir()
    ms = 1_759_053_720_000
    entries = [
        {
            "kind": "message",
            "id": "u1",
            "role": "user",
            "content": "Atlas uses MySQL.",
            "isStreaming": False,
            "timestampMs": ms,
            "requestId": "r",
            "seq": 1,
        },
        {
            "kind": "send-message",
            "id": "t1",
            "message": {"type": "text", "content": "Noted."},
            "timestampMs": ms + 1,
            "requestId": "r",
            "seq": 2,
        },
        {
            "kind": "message",
            "id": "u2",
            "role": "user",
            "content": "Atlas uses MySQL.",
            "fromAgent": {"id": "agent-gm", "name": "graph-memory"},
            "isStreaming": False,
            "timestampMs": ms + 2,
            "requestId": "r",
            "seq": 3,
        },
        {
            "kind": "message",
            "id": "a1",
            "role": "assistant",
            "content": "Checked.",
            "toAgent": {"id": "agent-gm", "name": "graph-memory", "kind": "agent"},
            "isStreaming": False,
            "timestampMs": ms + 3,
            "requestId": "r",
            "seq": 4,
        },
        {
            "kind": "send-message",
            "id": "w1",
            "respondedValue": "Postgres",
            "message": {
                "type": "widget",
                "widget": {
                    "prompt": "Which database?",
                    "options": [{"label": "MySQL", "value": "mysql"}],
                },
            },
            "timestampMs": ms + 4,
            "requestId": "r",
            "seq": 5,
        },
        {
            "kind": "message",
            "id": "v1",
            "role": "user",
            "content": "Ship it Friday.",
            "author": {"kind": "cursor_user", "id": "github|me", "name": "the call"},
            "fromUser": {"name": "the call"},
            "channel": "voice:call-1",
            "isStreaming": False,
            "timestampMs": ms + 5,
            "seq": 6,
        },
        {
            "kind": "message",
            "id": "s1",
            "role": "assistant",
            "content": "Parti",
            "isStreaming": True,
            "timestampMs": ms + 6,
            "seq": 7,
        },
        {
            "kind": "send-message",
            "id": "k1",
            "secretProvided": True,
            "message": {"type": "secret-request", "secretRequest": {"label": "GitHub token"}},
            "timestampMs": ms + 7,
            "seq": 8,
        },
        {
            "kind": "user-attachment",
            "id": "f1",
            "file_name": "shot.png",
            "file_path": "/tmp/shot.png",
            "timestampMs": ms + 8,
            "seq": 9,
        },
        {
            "kind": "event",
            "id": "e1",
            "event": {"type": "automation", "action": "deleted", "automationName": "standup"},
            "timestampMs": ms + 9,
            "seq": 10,
        },
        {
            "kind": "voice-call",
            "id": "c1",
            "call": {"durationMs": 60_000, "turnCount": 4},
            "timestampMs": ms + 10,
            "seq": 11,
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    parsed = list(records(path))
    shape = [(m.record_id.split("-")[1], m.role, m.source_type) for m in parsed]
    assert shape == [
        ("1", "user", "user_assertion"),
        ("2", "assistant", "assistant_report"),
        ("3", "user", "context"),
        ("4", "assistant", "assistant_report"),
        ("5", "assistant", "assistant_report"),
        ("5", "user", "user_assertion"),
        ("6", "user", "user_assertion"),
        # line 7 is a partial streaming message
        ("8", "note", "context"),
        ("9", "note", "context"),
        ("10", "note", "context"),
        ("11", "note", "context"),
    ]
    assert all(m.timestamp is not None for m in parsed)
    relay = parsed[2]
    assert "delegated_instruction" in relay.gaps and relay.from_agent == "agent-gm"
    assert "Which database?" in parsed[4].content and "MySQL" in parsed[4].content
    assert parsed[5].content == "Postgres" and "widget_response" in parsed[5].gaps
    assert "Parti" not in "".join(m.content for m in parsed)
    assert "GitHub token" in parsed[7].content and "non_text_attachment" in parsed[8].gaps
    sourced = grok_transcript(parsed)
    assert sourced.can_yield_facts()
    extraction(parsed[0].id).validate_evidence(sourced)


def test_another_author_on_a_readtranscript_line_is_a_relay(tmp_path):
    path = tmp_path / "grok-bot" / "desk.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "kind": "message",
                "id": "x",
                "role": "user",
                "content": "Deploy is done.",
                "author": {"kind": "agent", "id": "agent-2", "name": "homelab"},
                "isStreaming": False,
                "timestampMs": 1_759_053_720_000,
                "seq": 1,
            }
        )
        + "\n"
    )
    (m,) = records(path)
    assert (m.source_type, m.from_agent) == ("context", "agent-2")


def test_malformed_readtranscript_entries_do_not_stop_the_file(tmp_path):
    path = tmp_path / "grok-bot" / "desk.jsonl"
    path.parent.mkdir()
    bad = [
        {"kind": "send-message", "message": {"type": "widget", "widget": {"options": 5}}},
        {"kind": "send-message", "message": {"type": "secret-request", "secretRequest": "x"}},
        {"kind": "send-message", "message": {"type": "auto-review-approval", "approval": [1]}},
        {"kind": "send-message", "message": "text"},
        {"kind": "event", "event": "x"},
    ]
    good = {
        "kind": "message",
        "role": "user",
        "content": "Atlas uses MySQL.",
        "isStreaming": False,
        "timestampMs": 1_759_053_720_000,
    }
    path.write_text("".join(json.dumps(e) + "\n" for e in [*bad, good]))
    parsed = list(records(path))
    assert parsed[-1].source_type == "user_assertion"
    assert all(m.source_type == "context" for m in parsed[:-1])


def test_skipped_widget_value_is_not_an_answer(tmp_path):
    path = tmp_path / "grok-bot" / "desk.jsonl"
    path.parent.mkdir()
    widget = {
        "type": "widget",
        "widget": {"prompt": "Deploy now?", "options": [{"label": "Yes", "value": "yes"}]},
    }
    path.write_text(
        json.dumps(
            {
                "kind": "send-message",
                "id": "w",
                "message": widget,
                "respondedValue": "yes",
                "widgetSkipped": True,
            }
        )
        + "\n"
    )
    prompt, value = records(path)
    assert value.source_type == "context" and "widget_skipped" in value.gaps


def test_a_widget_exported_again_once_answered_adds_only_the_answer(tmp_path):
    path = tmp_path / "grok-bot" / "desk.jsonl"
    path.parent.mkdir()
    sent = {
        "kind": "send-message",
        "id": "w",
        "seq": 5,
        "timestampMs": 1_759_053_720_000,
        "message": {"type": "widget", "widget": {"prompt": "Which database?"}},
    }
    path.write_text(json.dumps(sent) + "\n")
    first = list(records(path))
    assert [m.source_type for m in first] == ["assistant_report"]
    with path.open("a") as f:
        f.write(json.dumps({**sent, "respondedValue": "Postgres"}) + "\n")
    again = list(records(path))
    # The prefix is unchanged, so the feed cursor resumes; only the answer is new.
    assert again[: len(first)] == first
    assert [(m.role, m.source_type, m.content) for m in again[len(first) :]] == [
        ("user", "user_assertion", "Postgres")
    ]


def test_an_entry_edited_after_it_was_read_is_kept_as_context(tmp_path):
    path = tmp_path / "grok-bot" / "desk.jsonl"
    path.parent.mkdir()
    sent = {
        "kind": "message",
        "id": "m",
        "role": "user",
        "content": "Atlas uses MySQL.",
        "isStreaming": False,
        "timestampMs": 1_759_053_720_000,
    }
    path.write_text(
        json.dumps(sent)
        + "\n"
        + json.dumps(sent)
        + "\n"
        + json.dumps({**sent, "content": "Atlas uses Postgres."})
        + "\n"
    )
    first, revised = records(path)
    assert first.source_type == "user_assertion" and first.content == "Atlas uses MySQL."
    assert revised.source_type == "context" and "entry_revised_after_read" in revised.gaps
    assert revised.content == "Atlas uses Postgres."


# --- Cursor agent-transcript adapter tests ---


def cursor(role, content, stamp=None):
    """Generate a Cursor agent-transcript JSONL line."""
    record = {"role": role, "message": {"content": content}}
    if stamp:
        record["timestamp"] = stamp
    return json.dumps(record) + "\n"


def cursor_direct(role, content, stamp=None):
    """Cursor format with content at top level (alternative shape)."""
    record = {"role": role, "content": content}
    if stamp:
        record["timestamp"] = stamp
    return json.dumps(record) + "\n"


def test_cursor_format_detection():
    assert is_cursor_format({"role": "user", "message": {"content": []}})
    assert is_cursor_format({"role": "assistant", "message": {"content": "text"}})
    assert is_cursor_format({"role": "user", "content": "text"})
    assert not is_cursor_format({"type": "user", "message": {"content": []}})
    assert not is_cursor_format({"type": "response_item", "role": "user"})
    assert not is_cursor_format({"type": "session_meta"})
    assert not is_cursor_format({"role": "system", "message": {"content": []}})


def test_cursor_text_messages(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        cursor("user", [{"type": "text", "text": "Atlas uses MySQL."}])
        + cursor("assistant", [{"type": "text", "text": "I understand."}])
    )
    ms = list(records(p))
    assert len(ms) == 2
    assert ms[0].role == "user"
    assert ms[0].source_type == "user_assertion"
    assert ms[0].content == "Atlas uses MySQL."
    assert ms[1].role == "assistant"
    assert ms[1].source_type == "assistant_report"


def test_cursor_string_content(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(cursor("user", "Plain string content."))
    ms = list(records(p))
    assert len(ms) == 1
    assert ms[0].content == "Plain string content."


def test_cursor_direct_content_format(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(cursor_direct("user", [{"type": "text", "text": "Direct format."}]))
    ms = list(records(p))
    assert len(ms) == 1
    assert ms[0].content == "Direct format."


def test_cursor_tool_use_and_result_pairing(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        cursor(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "call_abc",
                    "name": "Read",
                    "input": {"file_path": "/bank/memory/state.md"},
                }
            ],
        )
        + cursor(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_abc",
                    "content": "Memory content here.",
                }
            ],
        )
    )
    ms = list(records(p))
    assert len(ms) == 2
    assert ms[0].source_type == "tool_call"
    assert ms[0].tool_name == "Read"
    assert ms[0].call_id == "call_abc"
    assert ms[1].source_type == "memory_read"
    assert ms[1].call_id == "call_abc"


def test_cursor_shell_command_output(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        cursor(
            "assistant",
            [{"type": "tool_use", "id": "sh1", "name": "Shell", "input": {"command": "ls"}}],
        )
        + cursor(
            "user",
            [{"type": "tool_result", "tool_use_id": "sh1", "content": "file1.txt\nfile2.txt"}],
        )
    )
    ms = list(records(p))
    assert ms[1].source_type == "tool_result"
    assert SHELL_OUTPUT in ms[1].gaps


def test_cursor_missing_timestamp(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(cursor("user", [{"type": "text", "text": "No timestamp."}]))
    ms = list(records(p))
    assert ms[0].timestamp is None


def test_cursor_with_timestamp(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        cursor("user", [{"type": "text", "text": "Has timestamp."}], "2026-09-20T15:00:00Z")
    )
    ms = list(records(p))
    assert ms[0].timestamp is not None


def test_cursor_mixed_with_claude_codex(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        claude("user", [{"type": "text", "text": "Claude message."}])
        + cursor("user", [{"type": "text", "text": "Cursor message."}])
        + codex("message", role="user", content=[{"type": "input_text", "text": "Codex message."}])
    )
    ms = list(records(p))
    assert len(ms) == 3
    assert [m.content for m in ms] == ["Claude message.", "Cursor message.", "Codex message."]


def test_cursor_subagent_directory_is_delegated(tmp_path):
    subagent_dir = tmp_path / "project" / "agent-transcripts" / "subagents"
    subagent_dir.mkdir(parents=True)
    p = subagent_dir / "s.jsonl"
    p.write_text(cursor("user", [{"type": "text", "text": "From subagent."}]))
    ms = list(records(p))
    assert ms[0].source_type == "context"
    assert "delegated_instruction" in ms[0].gaps


def test_cursor_image_attachment(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        cursor(
            "user",
            [
                {"type": "text", "text": "See this image:"},
                {"type": "image", "source": {"type": "base64", "data": "..."}},
            ],
        )
    )
    ms = list(records(p))
    assert len(ms) == 2
    assert ms[0].content == "See this image:"
    assert "Non-text attachment" in ms[1].content


def test_cursor_entries_helper():
    item = {"role": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}
    entries = cursor_entries(item)
    assert len(entries) == 1
    assert entries[0] == ("text", {"role": "user", "content": "Hello"})


def test_cursor_string_blocks_in_content(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"role": "user", "message": {"content": ["Plain string block."]}}) + "\n"
    )
    ms = list(records(p))
    assert len(ms) == 1
    assert ms[0].content == "Plain string block."


def test_cursor_metadata_preserves_messages_and_tools_beside_native_grok(tmp_path):
    path = tmp_path / "mixed.jsonl"
    lines = [
        {
            "role": "user",
            "message": {"content": "A relay from another agent."},
            "fromAgent": {"id": "helper"},
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/status.txt"},
                }
            ],
            "channel": "desk",
        },
        {
            "role": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "read-1", "content": "Service ready."}
                ]
            },
            "channel": "desk",
        },
        {
            "kind": "message",
            "id": "native-1",
            "role": "user",
            "content": "Native human statement.",
            "isStreaming": False,
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    relay, call, result, native = records(path)
    assert relay.content == "A relay from another agent."
    assert relay.source_type == "context" and relay.from_agent == "helper"
    assert "delegated_instruction" in relay.gaps
    assert call.source_type == "tool_call" and call.call_id == "read-1"
    assert result.source_type == "tool_result" and result.call_id == call.call_id
    assert result.content == "Service ready."
    assert native.source_type == "user_assertion" and native.content == "Native human statement."
