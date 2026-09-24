"""The experimental projection must preserve answers and journal verification."""

import json
from datetime import datetime

import pytest

from evals.historical_recall import ProjectedStore, RecallJournal, recall_node
from graph_memory.journal import Journal
from graph_memory.service import MemoryService

from .test_reference_journal import extraction, mixed_chain, query, transcript, v2_write
from .test_related_recall import QUESTION, VM, seed

pytestmark = pytest.mark.integration


def answer(store, ns, name, args):
    schema, handler, _ = MemoryService(store).session_tools()[name]
    return handler(schema.model_validate({"namespace": ns, **args}))


def compare(store, ns, monkeypatch, name, args):
    full = answer(store, ns, name, args)
    with monkeypatch.context() as patch:
        patch.setattr(store, "recall", ProjectedStore.recall.__get__(store))
        projected = answer(store, ns, name, args)
    assert projected == full
    return projected


def test_projection_parity_across_mixed_journal_and_cutoffs(graph, monkeypatch):
    store, ns = graph
    seen, _, _ = mixed_chain(store, ns, monkeypatch)
    for sequence in seen:
        full = Journal(store).snapshot(ns, sequence=sequence)
        projected = RecallJournal(store).snapshot(ns, sequence=sequence)
        for label, nodes in projected["state"].items():
            assert nodes == (
                {key: recall_node(label, node) for key, node in full["state"][label].items()}
                if label
                in {"MemoryEntity", "MemoryFact", "MemoryInsight", "MemoryEpisode", "MemoryMessage"}
                else {}
            )
        for cutoff in ({"at_change": sequence}, {"known_at": full["known_at"]}):
            for detail in ("compact", "full"):
                for question in (None, "Which database does Atlas use?"):
                    compare(
                        store,
                        ns,
                        monkeypatch,
                        "memory_recall",
                        {
                            "entity": "Atlas",
                            "detail": detail,
                            "question": question,
                            "include_history": True,
                            "limit": 1,
                            **cutoff,
                        },
                    )
            for question in ("database", "Postgres", "the", "no-match-xyz"):
                compare(
                    store,
                    ns,
                    monkeypatch,
                    "memory_search",
                    {
                        "question": question,
                        "include_history": True,
                        **cutoff,
                    },
                )
            compare(store, ns, monkeypatch, "memory_latest", {"entity": "Atlas", **cutoff})
            compare(
                store,
                ns,
                monkeypatch,
                "memory_recall",
                {
                    "entity": "Atlas",
                    "as_of": "2026-09-12T00:00:00Z",
                    "offset": 100,
                    **cutoff,
                },
            )


def test_related_decision_does_not_leak_from_future(graph, monkeypatch):
    store, ns = graph
    plan, decision, before = seed(store, ns)
    Journal(store).checkpoint(ns)
    head = Journal(store).snapshot(ns)["sequence"]
    args = {"entity": VM["key"], "question": QUESTION, "limit": 2}
    early = compare(store, ns, monkeypatch, "memory_recall", {**args, "at_change": before})
    assert [f["id"] for f in early["facts"]] == [plan]
    later = compare(store, ns, monkeypatch, "memory_recall", {**args, "at_change": head})
    assert {f["id"] for f in later["facts"]} == {plan, decision}


def test_legacy_whole_state_hash_is_verified_before_projection(graph, monkeypatch):
    store, ns = graph
    query(store, "MERGE (s:MemorySpace {id:$ns}) ON CREATE SET s.revision=0", ns=ns)
    with monkeypatch.context() as patch:
        patch.setattr(store, "mutate", lambda tx, ns, kind, details, op, scoped=False: op(tx))
        receipt = store.stage(transcript(ns, "legacy"))
        store.commit(ns, receipt["episode_id"], extraction())
    sequence = v2_write(store, ns, monkeypatch, checkpoint=True, version=1)
    compare(store, ns, monkeypatch, "memory_recall", {"entity": "Atlas", "at_change": sequence})


def test_changed_projected_nodes_keep_original_fields_until_validation(graph, monkeypatch):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    journal.checkpoint(ns)
    before = journal.snapshot(ns)
    episode = next(
        key
        for key, node in before["state"]["MemoryEpisode"].items()
        if node["status"] == "complete"
    )

    def update(tx):
        tx.run("MATCH (e:MemoryEpisode {id:$id}) SET e.status='pending'", id=episode).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "experiment-test", {}, update))
    result = RecallJournal(store).snapshot(ns)
    assert result["sequence"] == before["sequence"] + 1
    assert result["state"]["MemoryEpisode"][episode] == {"id": episode, "status": "pending"}
    old = RecallJournal(store).snapshot(ns, known_at=datetime.fromisoformat(before["known_at"]))
    assert (
        old["state"]["MemoryEpisode"][episode]["status"]
        == before["state"]["MemoryEpisode"][episode]["status"]
    )
    original = Journal._events

    def corrupt(self, *args):
        for event, tip in original(self, *args):
            for change in event["changes"]:
                if change["id"] == episode:
                    change["shape"] = "0" * 64
            yield event, tip

    with monkeypatch.context() as patch:
        patch.setattr(Journal, "_events", corrupt)
        with pytest.raises(ValueError, match="integrity"):
            RecallJournal(store).snapshot(ns)
    query(
        store, "MATCH (:MemoryChange {scope:$ns})-[:PART]->(p) SET p.data=$bad", ns=ns, bad=b"bad"
    )
    with pytest.raises(ValueError, match="checkpoint integrity"):
        RecallJournal(store).snapshot(ns)


def test_unchanged_nodes_are_projected_when_restored(graph, monkeypatch):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)
    journal = RecallJournal(store)
    journal.checkpoint(ns)
    episode = next(
        key
        for key, node in journal.snapshot(ns)["state"]["MemoryEpisode"].items()
        if node["status"] == "complete"
    )

    def update(tx):
        tx.run("MATCH (e:MemoryEpisode {id:$id}) SET e.status='pending'", id=episode).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "experiment-test", {}, update))
    restored = []
    original = journal._restore

    def inspect(*args, **kwargs):
        state, sealed, total = original(*args, **kwargs)
        restored.append({label: dict(nodes) for label, nodes in state.items()})
        return state, sealed, total

    monkeypatch.setattr(journal, "_restore", inspect)
    journal.snapshot(ns)
    state = restored[-1]
    assert "payload" in state["MemoryEpisode"][episode]  # changed later: complete
    assert all(
        node == recall_node(label, node)
        for label in ("MemoryEpisode", "MemoryMessage")
        for key, node in state[label].items()
        if (label, key) != ("MemoryEpisode", episode)
    )
    assert state["MemoryMessage"] and not any(
        state[label]
        for label in ("MemorySession", "MemoryArtifact", "MemoryArtifactObservation", "MemoryDream")
    )


def test_report_time_fallback_reads_projected_message_timestamps(graph, monkeypatch):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)

    def legacy(tx):
        # Facts committed before reported_at existed take their messages' time.
        tx.run("MATCH (f:MemoryFact {namespace:$ns}) REMOVE f.reported_at", ns=ns).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "experiment-test", {}, legacy))
    Journal(store).checkpoint(ns)
    store.stage(transcript(ns, "four", "Atlas uses DuckDB."))
    head = Journal(store).snapshot(ns)["sequence"]
    args = {"entity": "Atlas", "at_change": head, "detail": "full", "include_history": True}
    result = compare(store, ns, monkeypatch, "memory_recall", args)
    assert any(f.get("reported_at") for lane in ("current", "history") for f in result[lane])
    compare(
        store,
        ns,
        monkeypatch,
        "memory_latest",
        {"entity": "Atlas", "at_change": head, "detail": "full"},
    )


def test_legacy_checkpoint_and_delta_are_verified_before_projection(graph, monkeypatch):
    store, ns = graph
    query(store, "MERGE (s:MemorySpace {id:$ns}) ON CREATE SET s.revision=0", ns=ns)
    with monkeypatch.context() as patch:
        patch.setattr(store, "mutate", lambda tx, ns, kind, details, op, scoped=False: op(tx))
        receipt = store.stage(transcript(ns, "legacy"))
        store.commit(ns, receipt["episode_id"], extraction())
    checkpoint = v2_write(store, ns, monkeypatch, checkpoint=True, version=1)
    second = transcript(ns, "second", "Atlas uses Postgres.")
    delta = v2_write(
        store, ns, monkeypatch, lambda tx: store.stage(second, transaction=tx), version=1
    )
    for sequence in (checkpoint, delta):
        compare(store, ns, monkeypatch, "memory_recall", {"entity": "Atlas", "at_change": sequence})
    original = Journal._events

    def corrupting(target):
        def corrupt(self, *args):
            for event, tip in original(self, *args):
                if event["sequence"] == target:
                    if "snapshot" in event:
                        next(iter(event["snapshot"]["MemoryEpisode"].values()))["status"] = "x"
                    else:
                        event["changes"][0]["set"]["status"] = "x"
                yield event, tip

        return corrupt

    for target, message in ((checkpoint, "checkpoint integrity"), (delta, "replay integrity")):
        with monkeypatch.context() as patch:
            patch.setattr(Journal, "_events", corrupting(target))
            with pytest.raises(ValueError, match=message):
                RecallJournal(store).snapshot(ns, sequence=delta)


def test_legacy_delta_after_a_parts_checkpoint_replays_in_full(graph, monkeypatch):
    store, ns = graph

    def seed(tx):
        tx.run("CREATE (:MemorySession {id:'s1',namespace:$ns,name:'session'})", ns=ns).consume()
        tx.run(
            "CREATE (:MemoryEntity {id:'e1',namespace:$ns,key:'project:atlas',name:'Atlas',"
            "kind:'project',aliases:['atlas']})",
            ns=ns,
        ).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "experiment-test", {}, seed))
    Journal(store).checkpoint(ns)

    def add(tx):
        tx.run(
            "CREATE (:MemoryEntity {id:'e2',namespace:$ns,key:'database:mysql',name:'MySQL',"
            "kind:'database',aliases:['mysql']})",
            ns=ns,
        ).consume()

    sequence = v2_write(store, ns, monkeypatch, add, version=1)
    full = Journal(store).snapshot(ns, sequence=sequence)
    projected = RecallJournal(store).snapshot(ns, sequence=sequence)
    assert set(full["state"]["MemoryEntity"]) == {"e1", "e2"}
    assert projected["state"]["MemoryEntity"] == full["state"]["MemoryEntity"]
    assert not projected["state"]["MemorySession"]


def test_runner_records_request_and_engine_errors_without_crashing(graph, tmp_path):
    from evals.historical_recall import run

    store, ns = graph
    cases = [
        # Rejected by a cross-field validator: its error context holds an exception.
        {
            "tool": "memory_search",
            "arguments": {"question": "x", "at_change": 0, "known_at": "2026-09-24T00:00:00Z"},
        },
        {"tool": "memory_search_entities", "arguments": {"query": "", "at_change": 0}},
        {"tool": "memory_recall", "arguments": {"entity": "Atlas", "at_change": 0}},
    ]
    rows = run(store, ns, cases, 1, tmp_path / "out")
    responses = [
        json.loads((tmp_path / "out" / f"response-0-{row['case']}.json").read_bytes())
        for row in rows
    ]
    assert list(responses[0]) == list(responses[1]) == ["validation_error"]
    assert responses[2] == {"error": "No journal exists for this namespace"}
    with pytest.raises(ValueError, match="new or empty"):
        run(store, ns, cases, 1, tmp_path / "out")
    with pytest.raises(NotImplementedError):
        RecallJournal(store).replay(ns, "replay:" + ns)
    with pytest.raises(NotImplementedError):
        RecallJournal(store).verify(ns)
