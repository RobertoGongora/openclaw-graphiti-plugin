import json

import pytest

from graph_memory import retrieval as v
from graph_memory.journal import Journal
from graph_memory.models import Evidence, Message, Transcript
from graph_memory.recall_provenance import latest_report_time, recalled_ids, report_origins
from graph_memory.service import MemoryService
from graph_memory.session_sources import feed_records, records
from graph_memory.store import digest
from tests.test_session_sources import claude, codex, extraction


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
    "name, call",
    [
        ("mcp__graph_memory__memory_recall", '{"entity":"Atlas"}'),
        ("mcp__graph_memory__memory_evidence", '{"fact_ids":["..."]}'),
        ("functions.exec", "await tools.mcp__graph_memory__memory_recall({entity:'Atlas'})"),
    ],
)
def test_recalled_report_is_not_a_new_fact_even_when_uncertain(name, call):
    fid = "a" * 64
    t = transcript(
        [
            message("call", "tool_call", call, call_id="c", tool_name=name),
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
    # Writing the literal nested call into a file is an edit, not a call.
    edit = json.dumps({"new_string": "await tools.mcp__graph_memory__memory_recall({})"})
    t = transcript(
        [
            message("call", "tool_call", edit, tool_name="Edit", call_id="e"),
            message("output", "tool_result", "File updated.", call_id="e", tool_name="Edit"),
            message("report", "assistant_report", "Added the recall call to the test."),
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


@pytest.mark.parametrize(
    "name, kind, gaps, taints",
    [
        (
            "mcp__claude_ai_Slack__slack_send_message",
            "context",
            ["delegated_agent_report_not_execution_evidence"],
            False,
        ),
        (
            "mcp__perf__get_memory_usage",
            "memory_read",
            ["derived_memory_retrieval_not_fresh_verification"],
            False,
        ),
        ("mcp__graph_memory__memory_status", "tool_result", [], True),
        ("memory_search", "memory_read", ["derived_memory_retrieval_not_fresh_verification"], True),
        (
            "mcp__graphiti__search_memory_facts",
            "memory_read",
            ["derived_memory_retrieval_not_fresh_verification"],
            True,
        ),
        (
            "mcp__memory__read_graph",
            "memory_read",
            ["derived_memory_retrieval_not_fresh_verification"],
            True,
        ),
        (
            "mcp__stateful_memory__get_memory_state",
            "memory_read",
            ["derived_memory_retrieval_not_fresh_verification"],
            True,
        ),
        ("spawn_agent", "context", ["delegated_agent_report_not_execution_evidence"], True),
        ("Agent", "tool_result", [], True),
        ("Task", "tool_result", [], True),
    ],
)
def test_only_exact_delegation_and_memory_tools_taint_the_next_report(name, kind, gaps, taints):
    """The parser's broad labels hold such output back from validation; only the
    exact memory and delegation tools make the following report a memory echo."""
    t = transcript(
        [
            message("call", "tool_call", "{}", call_id="c", tool_name=name),
            message(
                "out",
                kind,
                "Per memory, Atlas uses MySQL.",
                call_id="c",
                tool_name=name,
                gaps=gaps,
            ),
            message("report", "assistant_report", "Atlas uses MySQL."),
        ],
        focus_message_ids=["report"],
    )
    assert ("report" in t.memory_origins) is taints
    assert t.can_yield_facts() is not taints
    candidate = extraction("report", status="uncertain")
    if not taints:
        candidate.validate_evidence(t)
        return
    with pytest.raises(ValueError, match="memory-derived report"):
        candidate.validate_evidence(t)
    if kind == "tool_result":
        # A delegated summary or memory view is not fresh corroboration either.
        validated = extraction(
            "report",
            status="active",
            valid_at="2026-09-01T00:00:00Z",
            validation_evidence=[{"message_id": "out", "quote": "Atlas uses MySQL."}],
        )
        with pytest.raises(ValueError, match="memory-derived report"):
            validated.validate_evidence(t)


@pytest.mark.parametrize(
    "path, taints",
    [
        ("/Users/rob/.claude/projects/-Users-rob-app/memory/atlas.md", True),
        ("/Users/rob/.claude/projects/-Users-rob-app/memory/topics/atlas.md", True),
        ("/Users/rob/notes/memories/atlas.txt", True),
        ("/repo/MEMORY.md", True),
        ("/repo/src/memory/allocator.c", False),
    ],
)
def test_memory_bank_notes_taint_but_code_under_a_memory_directory_does_not(tmp_path, path, taints):
    p = tmp_path / "session.jsonl"
    p.write_text(
        claude("user", [{"type": "text", "text": "Check the notes."}])
        + claude(
            "assistant",
            [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": path}}],
        )
        + claude(
            "user", [{"type": "tool_result", "tool_use_id": "a", "content": "Atlas uses MySQL."}]
        )
        + claude("assistant", [{"type": "text", "text": "Atlas uses MySQL."}])
    )
    msgs = list(records(p))
    assert next(m for m in msgs if m.role == "tool").source_type == "memory_read"
    report = next(m for m in msgs if m.source_type == "assistant_report")
    assert (report.id in report_origins(msgs)) is taints


def test_delegated_instruction_starts_a_new_turn_for_sub_agent_reports(tmp_path):
    p = tmp_path / "subagents" / "child.jsonl"
    p.parent.mkdir()
    p.write_text(
        claude("user", [{"type": "text", "text": "Recall what we know about Atlas."}])
        + claude(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "a",
                    "name": "mcp__graph_memory__memory_recall",
                    "input": {"entity": "Atlas"},
                }
            ],
        )
        + claude(
            "user", [{"type": "tool_result", "tool_use_id": "a", "content": "Atlas uses MySQL."}]
        )
        + claude("assistant", [{"type": "text", "text": "Atlas uses MySQL."}])
        + claude("user", [{"type": "text", "text": "Now check the live database."}])
        + claude(
            "assistant",
            [{"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "mysql -V"}}],
        )
        + claude(
            "user", [{"type": "tool_result", "tool_use_id": "b", "content": "mysql Ver 8.0.36"}]
        )
        + claude("assistant", [{"type": "text", "text": "The live database is MySQL 8.0.36."}])
    )
    msgs = list(records(p))
    instructions = [m for m in msgs if m.role == "user"]
    assert instructions and all(
        m.source_type == "context" and "delegated_instruction" in m.gaps for m in instructions
    )
    echo, finding = [m for m in msgs if m.source_type == "assistant_report"]
    origins = report_origins(msgs)
    assert echo.id in origins
    assert finding.id not in origins


def test_deeply_nested_read_output_does_not_break_intake():
    t = transcript(
        [
            message("read", "memory_read", "[" * 50_000),
            message("report", "assistant_report", "A recalled report."),
        ]
    )
    assert t.memory_origins["report"].fact_ids == []
    assert not t.can_yield_facts()


def test_recalled_ids_from_lane_keyed_full_views_and_chunked_output():
    fid, entity = "d" * 64, "e" * 64
    full_view = json.dumps(
        {
            "entities": [{"id": entity, "key": "project:atlas", "kind": "project"}],
            "current": [{"id": fid, "relation": "uses_database", "summary": "Atlas uses MySQL."}],
            "uncertain": [],
        }
    )
    assert recalled_ids(full_view) == [fid]
    # The same view as an MCP text content block, escaped inside the outer JSON.
    wrapped = json.dumps({"content": [{"type": "text", "text": full_view}]})
    assert recalled_ids(wrapped) == [fid]
    chunk = '"summary": "Atlas uses MySQL."}, {"id": "' + fid + '", "summary": "Atlas'
    assert recalled_ids(chunk) == [fid]
    assert recalled_ids(wrapped[wrapped.index("current") :]) == [fid]
    assert recalled_ids("no identifiers here") == []


def test_transcript_without_reads_serializes_as_before_so_episode_ids_survive_upgrade():
    t = transcript([message("report", "assistant_report", "Atlas uses MySQL.")])
    dumped = t.model_dump(mode="json")
    # Episode IDs and the daemon's change fingerprints digest this payload.
    assert "memory_origins" not in dumped
    assert "memory_origins" not in json.loads(t.model_dump_json())
    assert Transcript.model_validate(dumped).model_dump(mode="json") == dumped
    assert digest(Transcript.model_validate(dumped).model_dump(mode="json")) == digest(dumped)
    tainted = transcript(
        [
            message("read", "memory_read", "remembered"),
            message("report", "assistant_report", "Atlas uses MySQL."),
        ]
    )
    assert tainted.model_dump(mode="json")["memory_origins"] == {
        "report": {"result_ids": ["read"], "fact_ids": []}
    }


def test_parser_output_for_memory_and_delegation_records_is_pinned(tmp_path):
    """The prefix hash a feed cursor stored for these records before ADR 009; the
    parser must keep producing it or existing feeds stop resuming."""
    p = tmp_path / "session.jsonl"
    p.write_text(
        codex("message", role="user", content="What database does Atlas use?")
        + codex(
            "function_call",
            call_id="r1",
            name="mcp__graph_memory__memory_recall",
            arguments='{"entity":"Atlas"}',
        )
        + codex(
            "function_call_output",
            call_id="r1",
            output=json.dumps({"facts": [{"id": "a" * 64, "text": "Atlas uses MySQL."}]}),
        )
        + codex("function_call", call_id="s1", name="spawn_agent", arguments='{"task":"check"}')
        + codex("function_call_output", call_id="s1", output="Sub-agent: Atlas uses MySQL.")
        + codex(
            "function_call",
            call_id="x1",
            name="exec_command",
            arguments=json.dumps({"cmd": "psql -c 'select version()'"}),
        )
        + codex("function_call_output", call_id="x1", output="MySQL 8.0.36 server")
        + codex("message", role="assistant", content="Atlas uses MySQL.")
    )
    messages = list(records(p))
    assert len(messages) == 8
    assert (
        digest([m.model_dump(mode="json") for m in messages])
        == "ff2762de96e2080f80d65c09b64c7673e537a767ffeecef4bfdaaa7897be48b3"
    )
    report = messages[-1]
    assert report_origins(messages)[report.id]["result_ids"] == [messages[2].id, messages[4].id]


@pytest.mark.integration
def test_evidence_exposes_stored_read_ids_and_message_nodes_keep_them(graph, tmp_path):
    store, ns = graph
    p = tmp_path / "session.jsonl"
    p.write_text(
        codex(
            "function_call",
            call_id="r",
            name="mcp__graph_memory__memory_recall",
            arguments='{"entity":"Atlas"}',
        )
        + codex(
            "function_call_output", call_id="r", output=json.dumps({"facts": [{"id": "e" * 64}]})
        )
        + codex("function_call", call_id="x", name="exec_command", arguments='{"cmd":"mysql -V"}')
        + codex("function_call_output", call_id="x", output="mysql  Ver 8.0.36")
        + codex("message", role="assistant", content="Atlas uses MySQL.")
    )
    result = feed_records(MemoryService(store), ns, p, "evidence-session", max_batches=10)
    assert result["caught_up"] and len(result["receipts"]) == 1
    episode = result["receipts"][0]["episode_id"]
    saved = Transcript.model_validate_json(store.episode(ns, episode)["payload"])
    report = next(m for m in saved.messages if m.source_type == "assistant_report")
    read = next(m for m in saved.messages if m.source_type == "memory_read")
    fresh = next(m for m in saved.messages if m.content == "mysql  Ver 8.0.36")
    assert saved.can_yield_facts()
    committed = store.commit(
        ns,
        episode,
        extraction(
            report.id,
            status="active",
            valid_at="2026-09-16T10:00:00Z",
            validation_evidence=[{"message_id": fresh.id, "quote": "mysql  Ver 8.0.36"}],
        ),
    )
    read_ref = digest([ns, "evidence-session", read.id])
    found = v.evidence(store, v.EvidenceRequest(namespace=ns, fact_ids=committed["fact_ids"]))
    claim = found["facts"][0]["claims"][0]
    assert claim["source_message_id"] == digest([ns, "evidence-session", report.id])
    # Stored message IDs, the same form as source_message_id, so the read can be fetched.
    assert claim["memory_origin"] == {"result_ids": [read_ref], "fact_ids": ["e" * 64]}
    node = store.read(
        lambda tx: tx.run(
            "MATCH (m:MemoryMessage {id:$id}) RETURN m.memory_read_refs AS refs,"
            "m.recalled_fact_ids AS facts",
            id=claim["source_message_id"],
        ).single()
    )
    assert node["refs"] == [read_ref] and node["facts"] == ["e" * 64]
    assert (
        store.read(
            lambda tx: tx.run(
                "MATCH (m:MemoryMessage {id:$id}) RETURN m.source_type AS kind", id=read_ref
            ).single()["kind"]
        )
        == "memory_read"
    )
    checkpoint = Journal(store).verify(ns)["sequence"]
    historical = v.evidence(
        store, v.EvidenceRequest(namespace=ns, fact_ids=committed["fact_ids"], at_change=checkpoint)
    )
    assert historical["facts"][0]["claims"] == found["facts"][0]["claims"]


@pytest.mark.parametrize(
    "call",
    [
        "await tools['mcp__graph_memory__memory_recall']({entity:'Atlas'})",
        'await tools["graph_memory"].memory_recall({entity:"Atlas"})',
    ],
)
def test_bracket_form_nested_memory_calls_are_recognized(call):
    t = transcript(
        [
            message("call", "tool_call", call, call_id="c", tool_name="functions.exec"),
            message(
                "read", "tool_result", "Atlas uses MySQL.", call_id="c", tool_name="functions.exec"
            ),
            message("report", "assistant_report", "Atlas uses MySQL."),
        ],
        focus_message_ids=["report"],
    )
    assert t.memory_origins["report"].result_ids == ["read"]
    assert not t.can_yield_facts()


def test_carried_batch_context_stops_at_a_delegated_or_automated_instruction(tmp_path):
    """A later batch carries the turn's tool results as context. An earlier
    turn's memory read must not be carried across an instruction the parser holds
    back as context, or the batch validator would taint the new finding again."""
    from graph_memory.session_sources import batch

    session_meta = (
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-09-16T10:00:00Z",
                "payload": {"source": "exec", "cwd": "/repo"},
            }
        )
        + "\n"
    )
    p = tmp_path / "session.jsonl"
    p.write_text(
        session_meta
        + codex("message", role="user", content="Task one: what does memory say?")
        + codex(
            "function_call", call_id="m", name="mcp__graph_memory__memory_evidence", arguments="{}"
        )
        + codex("function_call_output", call_id="m", output="Atlas uses MySQL.")
        + codex("message", role="assistant", content="Memory says Atlas uses MySQL.")
        + codex("message", role="user", content="Task two: inspect the cache.")
        + "".join(
            codex("function_call", call_id=f"x{i}", name="exec_command", arguments="{}")
            + codex("function_call_output", call_id=f"x{i}", output=f"redis_version:7.2.{i}")
            for i in range(6)
        )
        + codex("message", role="assistant", content="Atlas uses Redis 7.2 for caching.")
    )
    messages = list(records(p))
    instructions = [m for m in messages if m.role == "user"]
    assert all("automated_prompt_not_direct_user_assertion" in m.gaps for m in instructions)
    # An evidence read keeps the parser's tool_result label; it is a read all the same.
    read = next(m for m in messages if m.role == "tool" and "memory_evidence" in m.tool_name)
    echo, finding = [m for m in messages if m.source_type == "assistant_report"]
    origins = report_origins(messages)
    assert origins[echo.id]["result_ids"] == [read.id]
    assert finding.id not in origins
    end, selected = batch(messages, len(messages) - 1)
    assert instructions[1].id not in {m.id for m in selected}
    assert read.id not in {m.id for m in selected}
    t = Transcript(
        namespace="test",
        source_id="source",
        session_id="session",
        source_format="session-records-v1",
        messages=selected,
        memory_origins={m.id: origins[m.id] for m in selected if m.id in origins},
        focus_message_ids=[m.id for m in messages[len(messages) - 1 : end]],
    )
    assert finding.id not in t.memory_origins
    assert t.can_yield_facts()
    extraction(
        finding.id, quote="Atlas uses Redis 7.2 for caching.", status="uncertain"
    ).validate_evidence(t)
