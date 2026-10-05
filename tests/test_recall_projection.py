"""Historical recall, search and latest read a projected reconstruction by default.
Every answer must equal the unprojected baseline's, with the journal verified as before."""

import json
import sys
from datetime import datetime

import pytest

from evals.historical_recall import FullJournal, FullStore
from graph_memory.journal import LABELS, RECALL_LABELS, Journal, recall_node
from graph_memory.models import Extraction
from graph_memory.service import MemoryService
from graph_memory.store import GraphStore

from .helpers import PROJECT
from .test_reference_journal import extraction, mixed_chain, query, transcript, v2_write
from .test_related_recall import QUESTION, VM, seed

DUCKDB = {"key": "database:duckdb", "name": "DuckDB", "kind": "database"}
RECALLS = (
    ("memory_recall", {"entity": "Atlas"}),
    ("memory_search", {"question": "database"}),
    ("memory_latest", {"entity": "Atlas"}),
)


def unprojected(store):
    """The same connection read through the eval baseline: complete historical state."""
    full = object.__new__(FullStore)
    full.__dict__.update(store.__dict__)
    return full


def answer(store, ns, name, args):
    schema, handler, _ = MemoryService(store).session_tools()[name]
    return handler(schema.model_validate({"namespace": ns, **args}))


@pytest.fixture
def reads(monkeypatch):
    """Every reconstruction requested, with its options and the state it returned."""
    seen = []
    original = Journal.snapshot

    def snapshot(self, namespace, **kwargs):
        result = original(self, namespace, **kwargs)
        seen.append((kwargs, result["state"]))
        return result

    monkeypatch.setattr(Journal, "snapshot", snapshot)
    return seen


def compare(store, ns, reads, name, args):
    """The ordinary handler's answer, which must equal the unprojected baseline's."""
    reads.clear()
    ordinary = answer(store, ns, name, args)
    projected = list(reads)
    reads.clear()
    assert answer(unprojected(store), ns, name, args) == ordinary
    # The same reconstructions: the ordinary ones projected, the baseline's complete.
    assert len(projected) == len(reads)
    for kwargs, state in projected:
        assert kwargs["project_node"] is recall_node
        for label, nodes in state.items():
            assert nodes == (
                {key: recall_node(label, node) for key, node in nodes.items()}
                if label in RECALL_LABELS
                else {}
            )
    assert not any("project_node" in kwargs or "select" in kwargs for kwargs, _ in reads)
    return ordinary


def test_historical_reads_ask_for_the_reconstruction_they_need(monkeypatch):
    """Without a database: what each read asks the journal for by default."""
    requested = []

    class Requested(Exception):
        pass

    def snapshot(self, namespace, **kwargs):
        requested.append((type(self), kwargs))
        raise Requested

    monkeypatch.setattr(Journal, "snapshot", snapshot)
    store = object.__new__(GraphStore)  # no driver: nothing here can reach a database
    for reader, journal in ((store, Journal), (unprojected(store), FullJournal)):
        tools = MemoryService(reader).session_tools()
        for cutoff in ({"at_change": 3}, {"known_at": "2026-09-20T00:00:00Z"}):
            for name, args in RECALLS:
                requested.clear()
                schema, handler, _ = tools[name]
                with pytest.raises(Requested):
                    handler(schema.model_validate({"namespace": "ns", **args, **cutoff}))
                [(kind, kwargs)] = requested
                assert kind is journal
                if journal is Journal:
                    assert kwargs["project_node"] is recall_node
                    kept = {label for label in LABELS if kwargs["select"](label, {"id": "x"})}
                    assert kept == RECALL_LABELS
                else:
                    assert set(kwargs) == {"known_at", "sequence"}
            # Evidence and entity search select their own nodes, never projected.
            for name, args in (
                ("memory_evidence", {"fact_ids": ["f1"]}),
                ("memory_search_entities", {"query": "Atlas"}),
            ):
                requested.clear()
                schema, handler, _ = tools[name]
                with pytest.raises(Requested):
                    handler(schema.model_validate({"namespace": "ns", **args, **cutoff}))
                [(kind, kwargs)] = requested
                assert kind is Journal and "project_node" not in kwargs and kwargs["select"]
    with pytest.raises(ValueError, match="historical cutoff"):
        unprojected(store).recall("ns", "Atlas")


def test_runner_modes_are_the_runtime_store_and_the_full_baseline(monkeypatch, tmp_path):
    from evals import historical_recall as runner

    queries = tmp_path / "queries.json"
    queries.write_text('{"calls": []}')
    used = []
    monkeypatch.setattr(GraphStore, "__init__", lambda self, uri, password=None: None)
    monkeypatch.setattr(GraphStore, "close", lambda self: None)
    monkeypatch.setattr(runner, "run", lambda store, *args: used.append(type(store)))
    for mode in ("full", "projected"):
        argv = ["runner", "--uri", "bolt://127.0.0.1:47687", "--frozen-copy", "--namespace"]
        argv += ["ns", "--queries", str(queries), "--mode", mode, "--output", str(tmp_path)]
        monkeypatch.setattr(sys, "argv", argv)
        runner.main()
    assert used == [FullStore, GraphStore]


@pytest.mark.integration
def test_ordinary_historical_reads_match_the_unprojected_baseline(graph, monkeypatch, reads):
    store, ns = graph
    seen, boundary, fact = mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    for sequence in seen:
        full = journal.snapshot(ns, sequence=sequence)
        projected = journal.recall_snapshot(ns, sequence=sequence)
        assert {k: v for k, v in projected.items() if k != "state"} == {
            k: v for k, v in full.items() if k != "state"
        }
        for label, nodes in projected["state"].items():
            assert nodes == (
                {key: recall_node(label, node) for key, node in full["state"][label].items()}
                if label in RECALL_LABELS
                else {}
            )
        for cutoff in ({"at_change": sequence}, {"known_at": full["known_at"]}):
            for detail in ("compact", "full"):
                for question in (None, "Which database does Atlas use?"):
                    compare(
                        store,
                        ns,
                        reads,
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
                compare(
                    store,
                    ns,
                    reads,
                    "memory_latest",
                    {"entity": "Atlas", "detail": detail, **cutoff},
                )
            for question in ("database", "Postgres", "the", "no-match-xyz"):
                compare(
                    store,
                    ns,
                    reads,
                    "memory_search",
                    {"question": question, "include_history": True, **cutoff},
                )
            missing = {"question": "no-match-xyz", **cutoff}
            assert compare(store, ns, reads, "memory_search", missing)["facts"] == []
            nobody = compare(store, ns, reads, "memory_recall", {"entity": "Nobody", **cutoff})
            assert nobody["status"] == "not_found"
            compare(
                store,
                ns,
                reads,
                "memory_recall",
                {"entity": "Atlas", "as_of": "2026-09-12T00:00:00Z", "offset": 100, **cutoff},
            )
    # Evidence and entity search still reconstruct their own nodes in full.
    reads.clear()
    past = answer(
        store,
        ns,
        "memory_evidence",
        {"fact_ids": [fact], "at_change": boundary + 2, "detail": "full"},
    )
    assert past["facts"][0]["claims"][0]["quote"] == "Atlas uses Postgres."
    assert past["facts"][0]["source"]["status"] == "complete"
    answer(store, ns, "memory_search_entities", {"query": "Atlas", "at_change": boundary + 2})
    assert len(reads) == 3 and not any("project_node" in kwargs for kwargs, _ in reads)


@pytest.mark.integration
def test_related_decision_does_not_leak_from_future(graph, reads):
    store, ns = graph
    plan, decision, before = seed(store, ns)
    Journal(store).checkpoint(ns)
    head = Journal(store).snapshot(ns)["sequence"]
    args = {"entity": VM["key"], "question": QUESTION, "limit": 2}
    early = compare(store, ns, reads, "memory_recall", {**args, "at_change": before})
    assert [f["id"] for f in early["facts"]] == [plan]
    later = compare(store, ns, reads, "memory_recall", {**args, "at_change": head})
    assert {f["id"] for f in later["facts"]} == {plan, decision}


@pytest.mark.integration
def test_legacy_whole_state_hash_is_verified_before_projection(graph, monkeypatch, reads):
    store, ns = graph
    query(store, "MERGE (s:MemorySpace {id:$ns}) ON CREATE SET s.revision=0", ns=ns)
    with monkeypatch.context() as patch:
        patch.setattr(store, "mutate", lambda tx, ns, kind, details, op, scoped=False: op(tx))
        receipt = store.stage(transcript(ns, "legacy"))
        store.commit(ns, receipt["episode_id"], extraction())
    sequence = v2_write(store, ns, monkeypatch, checkpoint=True, version=1)
    compare(store, ns, reads, "memory_recall", {"entity": "Atlas", "at_change": sequence})


@pytest.mark.integration
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

    store.transaction(lambda tx: store.mutate(tx, ns, "projection-test", {}, update))
    result = journal.recall_snapshot(ns)
    assert result["sequence"] == before["sequence"] + 1
    assert result["state"]["MemoryEpisode"][episode] == {"id": episode, "status": "pending"}
    old = journal.recall_snapshot(ns, known_at=datetime.fromisoformat(before["known_at"]))
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

    request = {"entity": "Atlas", "at_change": result["sequence"]}
    with monkeypatch.context() as patch:
        patch.setattr(Journal, "_events", corrupt)
        for reader in (store, unprojected(store)):
            with pytest.raises(ValueError, match="integrity"):
                answer(reader, ns, "memory_recall", request)
    query(
        store, "MATCH (:MemoryChange {scope:$ns})-[:PART]->(p) SET p.data=$bad", ns=ns, bad=b"bad"
    )
    for reader in (store, unprojected(store)):
        with pytest.raises(ValueError, match="checkpoint integrity"):
            answer(reader, ns, "memory_recall", request)


@pytest.mark.integration
def test_ordinary_recall_compacts_unchanged_nodes_while_restoring(graph, monkeypatch):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    journal.checkpoint(ns)
    episode = next(
        key
        for key, node in journal.snapshot(ns)["state"]["MemoryEpisode"].items()
        if node["status"] == "complete"
    )

    def update(tx):
        tx.run("MATCH (e:MemoryEpisode {id:$id}) SET e.status='pending'", id=episode).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "projection-test", {}, update))
    head = journal.snapshot(ns)["sequence"]
    restored = []
    original = Journal._restore

    def inspect(self, *args, **kwargs):
        state, sealed, total = original(self, *args, **kwargs)
        # The fields each node held when the checkpoint was restored, before replay.
        restored.append(
            (
                {
                    label: {k: set(dict.keys(n)) for k, n in nodes.items()}
                    for label, nodes in state.items()
                },
                total,
            )
        )
        return state, sealed, total

    monkeypatch.setattr(Journal, "_restore", inspect)
    answer(store, ns, "memory_recall", {"entity": "Atlas", "at_change": head})
    [(state, total)] = restored
    assert "payload" in state["MemoryEpisode"][episode]  # changed later: complete
    assert all(
        fields == {"id", "status"}
        for key, fields in state["MemoryEpisode"].items()
        if key != episode
    )
    assert state["MemoryMessage"] and all(
        "id" in fields and fields <= {"id", "timestamp"}
        for fields in state["MemoryMessage"].values()
    )
    assert any("timestamp" in fields for fields in state["MemoryMessage"].values())
    assert not any(state[label] for label in LABELS if label not in RECALL_LABELS)
    # The baseline restores every node complete, and both sum the same original hashes.
    restored.clear()
    answer(unprojected(store), ns, "memory_recall", {"entity": "Atlas", "at_change": head})
    [(complete, complete_total)] = restored
    assert complete_total == total
    assert complete["MemorySession"] and all(
        "payload" in fields for fields in complete["MemoryEpisode"].values()
    )


@pytest.mark.integration
def test_report_time_fallback_reads_projected_message_timestamps(graph, monkeypatch, reads):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)

    def legacy(tx):
        # Facts committed before reported_at existed take their messages' time.
        tx.run("MATCH (f:MemoryFact {namespace:$ns}) REMOVE f.reported_at", ns=ns).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "projection-test", {}, legacy))
    Journal(store).checkpoint(ns)
    store.stage(transcript(ns, "four", "Atlas uses DuckDB."))
    head = Journal(store).snapshot(ns)["sequence"]
    args = {"entity": "Atlas", "at_change": head, "detail": "full", "include_history": True}
    result = compare(store, ns, reads, "memory_recall", args)
    assert any(f.get("reported_at") for lane in ("current", "history") for f in result[lane])
    compare(
        store,
        ns,
        reads,
        "memory_latest",
        {"entity": "Atlas", "at_change": head, "detail": "full"},
    )


@pytest.mark.integration
def test_report_chronology_uses_projected_message_times(graph, reads):
    store, ns = graph
    claim = "Atlas might use DuckDB."
    uncertain = Extraction.model_validate(
        {
            "entities": [PROJECT, DUCKDB],
            "facts": [
                {
                    "summary": claim,
                    "evidence": [{"message_id": "m0", "quote": claim}],
                    "subject": PROJECT["key"],
                    "target": DUCKDB["key"],
                    "relation": "uses_database",
                    "slot": "primary",
                    "status": "uncertain",
                }
            ],
        }
    )
    ids = {}
    # Committed in this order, so ingestion time alone would show the "late" copy.
    for source, day in (("early", 15), ("newest", 18), ("late", 16)):
        source_text = transcript(ns, source, claim, messages=1, stamp=f"2026-09-{day}T10:00:00Z")
        receipt = store.stage(source_text)
        ids[source] = store.commit(ns, receipt["episode_id"], uncertain)["fact_ids"][0]

    def legacy(tx):
        tx.run("MATCH (f:MemoryFact {namespace:$ns}) REMOVE f.reported_at", ns=ns).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "projection-test", {}, legacy))
    journal = Journal(store)
    journal.checkpoint(ns)
    store.stage(transcript(ns, "after", "Atlas uses SQLite.", messages=1))
    final = journal.snapshot(ns)
    assert not any("reported_at" in f for f in final["state"]["MemoryFact"].values())
    for cutoff in ({"at_change": final["sequence"]}, {"known_at": final["known_at"]}):
        for detail in ("compact", "full"):
            for question in (None, "Which database might Atlas use?"):
                args = {"entity": "Atlas", "detail": detail, "question": question, **cutoff}
                result = compare(store, ns, reads, "memory_recall", args)
                if detail == "full" and not question:
                    continue  # unranked engine lanes, compared above
                [shown] = result["facts"]
                assert shown["id"] == ids["newest"]
                assert shown["reported_at"] == "2026-09-18T10:00:00+00:00"
                assert shown["report_count"] == shown["source_sessions"] == 3
            latest = compare(
                store, ns, reads, "memory_latest", {"entity": "Atlas", "detail": detail, **cutoff}
            )
            assert latest["status"] == "uncertain"
        search = compare(store, ns, reads, "memory_search", {"question": "DuckDB", **cutoff})
        assert [f["id"] for f in search["facts"]] == [ids["newest"]]


@pytest.mark.integration
def test_legacy_checkpoint_and_delta_are_verified_before_projection(graph, monkeypatch, reads):
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
        compare(store, ns, reads, "memory_recall", {"entity": "Atlas", "at_change": sequence})
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
                Journal(store).recall_snapshot(ns, sequence=delta)
            for reader in (store, unprojected(store)):
                with pytest.raises(ValueError, match=message):
                    answer(reader, ns, "memory_recall", {"entity": "Atlas", "at_change": delta})


@pytest.mark.integration
def test_legacy_delta_after_a_parts_checkpoint_replays_in_full(graph, monkeypatch):
    store, ns = graph

    def seed(tx):
        tx.run("CREATE (:MemorySession {id:'s1',namespace:$ns,name:'session'})", ns=ns).consume()
        tx.run(
            "CREATE (:MemoryEntity {id:'e1',namespace:$ns,key:'project:atlas',name:'Atlas',"
            "kind:'project',aliases:['atlas']})",
            ns=ns,
        ).consume()

    store.transaction(lambda tx: store.mutate(tx, ns, "projection-test", {}, seed))
    Journal(store).checkpoint(ns)

    def add(tx):
        tx.run(
            "CREATE (:MemoryEntity {id:'e2',namespace:$ns,key:'database:mysql',name:'MySQL',"
            "kind:'database',aliases:['mysql']})",
            ns=ns,
        ).consume()

    sequence = v2_write(store, ns, monkeypatch, add, version=1)
    full = Journal(store).snapshot(ns, sequence=sequence)
    projected = Journal(store).recall_snapshot(ns, sequence=sequence)
    assert set(full["state"]["MemoryEntity"]) == {"e1", "e2"}
    assert projected["state"]["MemoryEntity"] == full["state"]["MemoryEntity"]
    assert not projected["state"]["MemorySession"]


@pytest.mark.integration
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
    # The baseline records the same bytes for every case.
    full = run(unprojected(store), ns, cases, 1, tmp_path / "full")
    assert [row["sha256"] for row in full] == [row["sha256"] for row in rows]
