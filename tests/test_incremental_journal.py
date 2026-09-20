"""Journal cost follows the change, legacy journals migrate, and intake survives restarts."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from graph_memory import journal as journal_module
from graph_memory.daemon import reclaim_leases, run_daemon
from graph_memory.follow import CURSOR, follow_once
from graph_memory.journal import Journal, capture, hexhash, set_hash
from graph_memory.models import EpisodeRequest, Extraction, Message, Transcript
from graph_memory.service import MemoryService
from graph_memory.store import GraphStore, digest

from .helpers import MYSQL, PG, PROJECT, ingest
from .test_session_sources import claude


def fact(store, ns, source, target=MYSQL, day=10):
    return ingest(
        store,
        ns,
        source,
        f"Atlas uses {target['name']}.",
        [PROJECT, target],
        [
            {
                "subject": PROJECT["key"],
                "target": target["key"],
                "relation": "uses_database",
                "slot": "primary",
                "valid_at": f"2026-09-{day:02d}T10:00:00Z",
            }
        ],
    )


def head(store, ns):
    return store.transaction(
        lambda tx: tx.run(
            "MATCH (s:MemorySpace {id:$ns}) RETURN properties(s) AS s", ns=ns
        ).single()["s"]
    )


def test_scoped_writes_never_capture_the_namespace(graph, monkeypatch):
    store, ns = graph
    fact(store, ns, "one")
    monkeypatch.delenv("MEMORY_JOURNAL_AUDIT")
    full, real = [], journal_module.capture

    def counting(tx, namespace, ids=None):
        if ids is None:
            full.append(namespace)
        return real(tx, namespace, ids)

    monkeypatch.setattr(journal_module, "capture", counting)
    fact(store, ns, "two", target=PG, day=12)
    assert full == []
    monkeypatch.undo()
    live = store.transaction(lambda tx: capture(tx, ns))
    assert head(store, ns)["journal_set_hash"] == hexhash(set_hash(live))
    assert Journal(store).snapshot(ns)["state"] == live
    assert Journal(store).verify(ns)["verified"]


def test_undeclared_scoped_write_is_caught_by_the_audit_and_by_verify(graph, monkeypatch):
    store, ns = graph
    fact(store, ns, "one")

    def sneaky(tx):
        tx.run(
            'CREATE (:MemoryEntity {id:$id,namespace:$ns,key:"x"})', id=ns + ":x", ns=ns
        ).consume()

    with pytest.raises(ValueError, match="differs from its journal"):
        store.transaction(lambda tx: store.mutate(tx, ns, "test", {}, sneaky, scoped=True))
    monkeypatch.delenv("MEMORY_JOURNAL_AUDIT")
    store.transaction(lambda tx: store.mutate(tx, ns, "test", {}, sneaky, scoped=True))
    with pytest.raises(ValueError, match="differs from journal"):
        Journal(store).verify(ns)
    # A full-capture write still refuses to build on an untracked change.
    with pytest.raises(ValueError, match="differs from its journal"):
        store.retract(ns, "missing", "reason")


def test_legacy_full_digest_journal_migrates_and_replays(graph):
    store, ns = graph
    receipt, _, _ = fact(store, ns, "one")
    journal = Journal(store)

    def legacy_baseline(tx):
        tx.run("MATCH (e:MemoryChange {scope:$ns}) DETACH DELETE e", ns=ns).consume()
        state = capture(tx, ns)
        event = {
            "version": 1,
            "sequence": 0,
            "scope": ns,
            "kind": "baseline",
            "recorded_us": 1,
            "recorded_at": "2026-09-01T00:00:00+00:00",
            "revision": 1,
            "engine": store.engine,
            "previous_hash": None,
            "state_hash": digest(state),
            "details": {},
            "changes": [],
            "snapshot": state,
        }
        tx.run(
            "CREATE (e:MemoryChange {id:$id,namespace:$audit,scope:$ns,sequence:0,kind:'baseline',"
            "recorded_at:$at,recorded_us:1,payload:$payload,hash:$hash}) WITH e "
            "MATCH (s:MemorySpace {id:$ns}) SET s.journal_sequence=0,s.journal_hash=$hash,"
            "s.journal_us=1,s.journal_state_hash=$state REMOVE s.journal_set_hash",
            id=digest(["journal", ns, 0]),
            audit="audit:" + ns,
            ns=ns,
            at=event["recorded_at"],
            payload=json.dumps(event, sort_keys=True),
            hash=digest(event),
            state=event["state_hash"],
        ).consume()

    store.transaction(legacy_baseline)
    fact(store, ns, "two", target=PG, day=12)  # migrates on a full capture
    assert "journal_set_hash" in head(store, ns)
    store.retract(ns, receipt["fact_ids"][0], "superseded")
    fact(store, ns, "three", day=14)  # scoped from here on
    assert [e["kind"] for e in journal.events(ns)][0] == "baseline"
    live = store.transaction(lambda tx: capture(tx, ns))
    assert journal.snapshot(ns)["state"] == live
    assert journal.verify(ns)["verified"]
    assert len(journal.snapshot(ns, sequence=0)["state"]["MemoryFact"]) == 1
    # An untracked edit made before the migration is refused, as before.
    store.transaction(legacy_baseline)
    store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFact {namespace:$ns}) SET f.summary='edited'", ns=ns
        ).consume()
    )
    with pytest.raises(ValueError, match="differs from its journal"):
        fact(store, ns, "four", day=15)


def test_no_periodic_snapshot_and_replay_past_one_hundred_events(graph):
    store, ns = graph
    receipt, _, _ = fact(store, ns, "one")
    for i in range(101):
        store.retract(ns, receipt["fact_ids"][0], f"note {i}")
    journal = Journal(store)
    flagged = store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryChange {scope:$ns}) WHERE e.checkpoint RETURN collect(e.sequence) AS s",
            ns=ns,
        ).single()["s"]
    )
    assert flagged == [0]
    final = journal.snapshot(ns)
    assert final["sequence"] > 100
    assert final["state"] == store.transaction(lambda tx: capture(tx, ns))
    past = journal.snapshot(ns, sequence=100)["state"]["MemoryFact"][receipt["fact_ids"][0]]
    assert past["retraction_reason"] == "note 97"


def test_two_stores_commit_concurrently_without_double_work_or_drift(graph):
    store, ns = graph
    other = GraphStore(
        os.environ["MEMORY_TEST_NEO4J_URI"], password=os.environ.get("MEMORY_TEST_NEO4J_PASSWORD")
    )
    try:
        for i in range(6):
            store.stage(
                Transcript(
                    namespace=ns,
                    source_id=f"s{i}",
                    session_id=f"s{i}",
                    messages=[Message(id="1", role="user", content=f"Note number {i}.")],
                )
            )

        class Counting:
            def __init__(self):
                self.calls, self.lock = [], threading.Lock()

            def generate(self, instructions, payload, output):
                with self.lock:
                    self.calls.append(payload["transcript"]["source_id"])
                time.sleep(0.05)
                return Extraction(entities=[], facts=[])

        model = Counting()
        services = [MemoryService(store, model), MemoryService(other, model)]
        from graph_memory.feeds import worker_tick

        def drain(service):
            while worker_tick(service, ns, limit=1)["receipts"] or store.pending(ns):
                pass

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(drain, services * 2))
        assert sorted(model.calls) == [f"s{i}" for i in range(6)]
        assert store.pending(ns) == []
        assert Journal(store).verify(ns)["verified"]
    finally:
        other.close()


def test_restart_reclaims_leases_and_reports_timings(graph, tmp_path, capsys):
    store, ns = graph
    (tmp_path / "note.md").write_text("No durable information.")

    class Empty:
        timeout = 600

        def generate(self, instructions, payload, output):
            return Extraction(entities=[], facts=[])

    service = MemoryService(store, Empty())
    from graph_memory.daemon import scan_bank

    scan_bank(service, ns, [tmp_path], {})
    episode = store.pending(ns)[0]["episode_id"]
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {id:$id}) SET e.lease_until=$until,e.worker='dead-process'",
            id=episode,
            until=time.time() + 2460,
        ).consume()
    )
    from graph_memory.feeds import worker_tick

    assert worker_tick(service, ns)["receipts"] == []  # orphaned until the lease expires
    run_daemon(service, ns, [tmp_path], workers=1, once=True)
    assert store.pending(ns) == []
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    by_name = {e["event"]: e for e in events}
    assert by_name["daemon_start"]["reclaimed_leases"] == 1
    assert by_name["daemon_exit"]["reason"] == "once"
    processed = by_name["processed"]
    assert processed["model_calls"] == 1 and processed["cached"] is False
    assert {"prepare", "model", "validation", "checkpoint", "commit"} <= processed["timings"].keys()
    assert all("ts" in e for e in events)
    assert reclaim_leases(service, ns) == 0
    receipt = service.extract(EpisodeRequest(namespace=ns, episode_id=episode))
    assert receipt["replayed"]


def session(path, text):
    path.write_text(claude("user", text))
    return path


def test_intake_survives_restart_without_reparsing_or_starving(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    files = [session(tmp_path / f"{i:02d}.jsonl", f"Note number {i}.") for i in range(7)]
    seen = {}
    staged = []
    for _ in range(3):
        staged += follow_once(service, ns, [tmp_path], seen, source_records=True)
    assert len(staged) == 7  # four files per scan, resuming after the cursor
    parsed = []
    from graph_memory import session_sources

    real = session_sources.feed_records

    def counting(service, namespace, path, session_id, **kwargs):
        parsed.append(path.name)
        return real(service, namespace, path, session_id, **kwargs)

    monkeypatch.setattr(session_sources, "feed_records", counting)
    # A new process has no memory of what it fed; the graph does.
    assert follow_once(service, ns, [tmp_path], {}, source_records=True) == []
    assert parsed == []
    with files[0].open("a") as handle:
        handle.write(claude("user", "A later note."))
    restarted = {}
    fed = follow_once(service, ns, [tmp_path], restarted, source_records=True)
    assert parsed == ["00.jsonl"] and len(fed) == 1
    assert restarted[CURSOR].endswith("00.jsonl")


def test_already_fed_files_do_not_spend_the_scan_budget(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    for i in range(6):
        session(tmp_path / f"{i:02d}.jsonl", f"Note number {i}.")
    seen = {}
    while follow_once(service, ns, [tmp_path], seen, source_records=True):
        pass
    store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {namespace:$ns}) REMOVE f.caught_up_size,f.caught_up_mtime_ns",
            ns=ns,
        ).consume()
    )
    session(tmp_path / "99.jsonl", "The newest note.")
    # Six fed files precede the new one; confirming them is free, so one scan reaches it.
    fed = follow_once(service, ns, [tmp_path], {}, source_records=True)
    assert [f["source"].rsplit("/", 1)[-1] for f in fed] == ["99.jsonl"]
