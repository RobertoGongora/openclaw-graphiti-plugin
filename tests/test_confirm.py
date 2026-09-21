"""A person can vouch for a claim the engine could not verify, and take it back."""

import pytest

from graph_memory.journal import Journal
from graph_memory.service import MemoryService
from graph_memory.session_sources import feed_records

from .helpers import MYSQL, PROJECT
from .test_session_sources import claude, extraction


def claimed_by_assistant(store, ns, tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(claude("assistant", [{"type": "text", "text": "Atlas uses MySQL."}]))
    service = MemoryService(store)
    eid = feed_records(service, ns, path, "s")["receipts"][0]["episode_id"]
    from graph_memory.models import Transcript

    message = Transcript.model_validate_json(store.episode(ns, eid)["payload"]).messages[0]
    receipt = store.commit(ns, eid, extraction(message.id, status="uncertain"))
    return receipt["fact_ids"][0]


def test_confirming_an_uncertain_fact_makes_it_current_and_is_reversible(graph, tmp_path):
    store, ns = graph
    fact_id = claimed_by_assistant(store, ns, tmp_path)
    before = store.recall(ns, "Atlas")
    assert before["current"] == [] and len(before["uncertain"]) == 1
    sequence = Journal(store).snapshot(ns)["sequence"]

    result = store.confirm(ns, fact_id, "I run it myself.")
    assert result["valid_at"].startswith("2026-09-16")  # when the assistant said it
    after = store.recall(ns, "Atlas")
    assert [f["target"] for f in after["current"]] == [MYSQL["key"]] and after["uncertain"] == []
    assert after["current"][0]["confirmed"] and after["current"][0]["subject"] == PROJECT["key"]
    # The record still says how it was learned, and history still shows the doubt.
    stored = store.transaction(
        lambda tx: tx.run("MATCH (f:MemoryFact {id:$id}) RETURN f.status AS s", id=fact_id).single()
    )
    assert stored["s"] == "uncertain"
    assert store.recall(ns, "Atlas", at_change=sequence)["current"] == []
    assert Journal(store).verify(ns)["verified"] and Journal(store).verify_live(ns)["verified"]

    store.retract(ns, fact_id, "No longer true.")
    assert store.recall(ns, "Atlas")["current"] == []


def test_confirm_refuses_what_it_should_not_touch(graph, tmp_path):
    store, ns = graph
    fact_id = claimed_by_assistant(store, ns, tmp_path)
    with pytest.raises(ValueError, match="Fact not found"):
        store.confirm(ns, "missing", "note")
    store.confirm(ns, fact_id, "Checked.")
    store.retract(ns, fact_id, "Wrong after all.")
    with pytest.raises(ValueError, match="retracted"):
        store.confirm(ns, fact_id, "Again.")


def test_confirm_is_a_session_tool(graph):
    store, _ = graph
    assert "memory_confirm" in MemoryService(store).session_tools()


def test_a_confirmed_fact_fulfils_a_plan_and_is_flagged_in_compact_recall():
    from datetime import UTC, datetime

    from graph_memory.retrieval import compact_fact
    from graph_memory.temporal import project

    def fact(fid, status, ts, **extra):
        return {
            "id": fid,
            "subject": "project:atlas",
            "relation": "uses_database",
            "target": "database:postgres",
            "slot": None,
            "status": status,
            "valid_ts": ts,
            "valid_at": "x",
            "summary": "Atlas uses Postgres.",
            "episode_id": "e",
            **extra,
        }

    plan = fact("p", "planned", 10)
    claim = fact(
        "c",
        "uncertain",
        None,
        confirmed_at="2026-09-21T00:00:00+00:00",
        confirmed_valid_at="2026-09-20T00:00:00+00:00",
        confirmed_valid_ts=20,
    )
    lanes = project([plan, claim], datetime.fromtimestamp(100, UTC))
    assert [f["id"] for f in lanes["current"]] == ["c"] and lanes["planned"] == []
    assert compact_fact(lanes["current"][0], "current")["confirmed_by_user"] is True
    assert "confirmed_by_user" not in compact_fact(plan, "planned")


def test_retracting_takes_the_confirmation_back_and_future_dates_are_refused(graph, tmp_path):
    from datetime import timedelta

    from graph_memory.models import now

    store, ns = graph
    fact_id = claimed_by_assistant(store, ns, tmp_path)
    with pytest.raises(ValueError, match="future"):
        store.confirm(ns, fact_id, "Soon.", now() + timedelta(days=1))
    store.confirm(ns, fact_id, "Checked.")
    store.retract(ns, fact_id, "Wrong after all.")
    props = store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFact {id:$id}) RETURN properties(f) AS f", id=fact_id
        ).single()
    )["f"]
    assert not [k for k in props if k.startswith("confirm")]
    assert Journal(store).verify_live(ns)["verified"]
