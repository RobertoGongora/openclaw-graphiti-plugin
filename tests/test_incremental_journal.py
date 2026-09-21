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
    # Source text is journaled by reference; resolving reads it back and checks it.
    assert Journal(store).resolve(ns, Journal(store).snapshot(ns)["state"]) == live
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
    journal = Journal(store)
    with pytest.raises(ValueError, match="differs from journal"):
        journal.verify_live(ns)
    # A full-capture write still refuses to build on an untracked change.
    with pytest.raises(ValueError, match="differs from its journal"):
        journal.checkpoint(ns)
    # Accepting the live graph is the explicit way forward; history stays readable.
    before = journal.snapshot(ns)["sequence"]
    accepted = journal.checkpoint(ns, accept_live=True)
    assert accepted["accepted_untracked_state"] and accepted["sequence"] == before + 1
    assert journal.verify(ns)["verified"]
    assert ns + ":x" in journal.snapshot(ns)["state"]["MemoryEntity"]
    assert ns + ":x" not in journal.snapshot(ns, sequence=before)["state"]["MemoryEntity"]
    fact(store, ns, "after", target=PG, day=18)
    assert journal.verify(ns)["verified"]


def test_legacy_full_digest_journal_migrates_and_replays(graph):
    store, ns = graph
    receipt, transcript, _ = fact(store, ns, "one")
    journal = Journal(store)
    legacy = {}

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
        legacy["hash"] = event["state_hash"]
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
    # A first write that changes nothing still migrates, and moves both head hashes
    # together: a process on the older code then stops at its own state check
    # instead of appending to a journal it no longer understands.
    store.stage(transcript)
    migrated = head(store, ns)
    assert migrated["journal_sequence"] == 0
    # The state field is fenced: an older engine compares it with its own hash of the
    # state, never finds them equal, and refuses to write.
    assert migrated["journal_state_hash"] == "v3:" + migrated["journal_set_hash"]
    assert migrated["journal_set_hash"] != legacy["hash"]
    fact(store, ns, "two", target=PG, day=12)
    store.retract(ns, receipt["fact_ids"][0], "superseded")
    fact(store, ns, "three", day=14)  # scoped from here on
    assert [e["kind"] for e in journal.events(ns)][0] == "baseline"
    live = store.transaction(lambda tx: capture(tx, ns))
    assert journal.resolve(ns, journal.snapshot(ns)["state"]) == live
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
    assert journal.checkpoint(ns)["accepted_untracked_state"] is False
    marked = journal.snapshot(ns)
    fact(store, ns, "later", target=PG, day=19)
    assert journal.snapshot(ns, sequence=marked["sequence"])["state"] == marked["state"]
    assert journal.verify(ns)["verified"]
    flagged = store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryChange {scope:$ns}) WHERE e.checkpoint RETURN collect(e.sequence) AS s",
            ns=ns,
        ).single()["s"]
    )
    assert sorted(flagged) == [0, marked["sequence"]]  # never periodic
    final = journal.snapshot(ns)
    assert final["sequence"] > 100
    assert journal.resolve(ns, final["state"]) == store.transaction(lambda tx: capture(tx, ns))
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
    assert reclaim_leases(service, ns) == 1  # what a long-running daemon does at start
    run_daemon(service, ns, [tmp_path], workers=1, once=True)
    assert store.pending(ns) == []
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    by_name = {e["event"]: e for e in events}
    assert by_name["daemon_start"]["reclaimed_leases"] == 0  # one-shot runs leave leases alone
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


def long_session(path, messages):
    path.write_text("".join(claude("user", f"{path.stem} note {i}.") for i in range(messages)))
    return path


def names(fed):
    return [f["source"].rsplit("/", 1)[-1] for f in fed]


def drain_queue(store, ns):
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns,status:'pending'}) SET e.retry_after=$later",
            ns=ns,
            later=time.time() + 3600,
        ).consume()
    )


def test_scan_budget_rotation_and_queue_gate(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    # 40 messages need five batches of eight: one scan cannot finish such a file.
    for i in range(8):
        long_session(tmp_path / f"{i:02d}.jsonl", 40)
    (tmp_path / "gone.jsonl").symlink_to(tmp_path / "missing.jsonl")
    seen = {}
    first = follow_once(service, ns, [tmp_path, tmp_path], seen, source_records=True)
    assert names(first) == ["00.jsonl", "01.jsonl", "02.jsonl", "03.jsonl"]
    # The next scan resumes after the cursor instead of revisiting unfinished files.
    second = follow_once(service, ns, [tmp_path, tmp_path], seen, source_records=True)
    assert names(second) == ["04.jsonl", "05.jsonl", "06.jsonl", "07.jsonl"]
    assert sum(len(f["receipts"]) for f in first + second) == 32
    # A full queue leaves the remaining source on disk.
    assert follow_once(service, ns, [tmp_path], seen, source_records=True) == []
    drain_queue(store, ns)
    third = follow_once(service, ns, [tmp_path], seen, source_records=True)
    assert names(third) == ["00.jsonl", "01.jsonl", "02.jsonl", "03.jsonl"]
    assert all(f["caught_up"] for f in third)


def test_many_small_files_cannot_flood_the_queue(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    for i in range(50):
        session(tmp_path / f"{i:02d}.jsonl", f"Note number {i}.")
    fed = follow_once(service, ns, [tmp_path], {}, source_records=True)
    assert len(fed) == 4  # every file that stages work spends budget
    assert len(store.pending(ns, limit=100)) == 4


def test_intake_survives_restart_without_reparsing(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    files = [session(tmp_path / f"{i:02d}.jsonl", f"Note number {i}.") for i in range(7)]
    seen = {}
    assert len(follow_once(service, ns, [tmp_path], seen, source_records=True)) == 4
    assert len(follow_once(service, ns, [tmp_path], seen, source_records=True)) == 3
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
    with files[3].open("a") as handle:
        handle.write(claude("user", "A later note."))
    restarted = {}
    fed = follow_once(service, ns, [tmp_path], restarted, source_records=True)
    assert parsed == ["03.jsonl"] and names(fed) == ["03.jsonl"]
    assert restarted[CURSOR].endswith("03.jsonl")


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


def edges(store, ns):
    return store.transaction(
        lambda tx: sorted(
            (r["a"], r["t"], r["b"])
            for r in tx.run(
                "MATCH (a {namespace:$ns})-[r]->(b {namespace:$ns}) "
                "RETURN a.id AS a,type(r) AS t,b.id AS b",
                ns=ns,
            )
        )
    )


def test_staging_links_only_its_own_nodes_and_matches_a_full_repair(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    memory = tmp_path / "memory" / "state.md"
    memory.parent.mkdir()
    memory.write_text("Atlas uses MySQL.")
    call = [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": str(memory)}}]
    result = [{"type": "tool_result", "tool_use_id": "r1", "content": "Atlas uses MySQL."}]
    path = tmp_path / "s.jsonl"
    # The result arrives in a later episode than its call: nine fillers push it out.
    path.write_text(
        claude("assistant", call)
        + "".join(claude("user", f"Filler message {i}.") for i in range(9))
        + claude("user", result)
    )
    from graph_memory import source_graph
    from graph_memory.session_sources import feed_records

    monkeypatch.setattr(source_graph, "repair", lambda *a: pytest.fail("namespace-wide repair"))
    fed = feed_records(service, ns, path, "s")
    assert len(fed["receipts"]) >= 2 and fed["caught_up"]
    monkeypatch.undo()
    staged = edges(store, ns)
    assert {t for _, t, _ in staged} >= {"HAS_EPISODE", "CONTAINS", "HAS_MESSAGE", "RESULT_OF"}
    store.repair(ns)
    assert edges(store, ns) == staged  # nothing was left for the full repair to add


def test_retract_and_merge_journal_only_what_they_touch(graph, monkeypatch):
    store, ns = graph
    receipt, _, _ = fact(store, ns, "one")
    other = {"key": "project:atlas-api", "name": "Atlas API", "kind": "project"}
    ingest(
        store,
        ns,
        "two",
        "Atlas API uses MySQL.",
        [other, MYSQL],
        [{"subject": other["key"], "target": MYSQL["key"], "relation": "uses_database"}],
    )
    monkeypatch.delenv("MEMORY_JOURNAL_AUDIT")
    full, real = [], journal_module.elements

    def counting(tx, namespace, ids=None):
        if ids is None:
            full.append(namespace)
        return real(tx, namespace, ids)

    monkeypatch.setattr(journal_module, "elements", counting)
    store.retract(ns, receipt["fact_ids"][0], "superseded")
    # A fact whose edge went missing still moves with its entity.
    store.transaction(
        lambda tx: tx.run(
            "MATCH (:MemoryEntity {namespace:$ns,key:$key})-[r:HAS_FACT]->() DELETE r",
            ns=ns,
            key=other["key"],
        ).consume()
    )
    store.merge(ns, other["key"], PROJECT["key"], "same project")
    assert full == []
    stranded = store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFact {namespace:$ns,subject:$key}) RETURN count(f) AS n",
            ns=ns,
            key=other["key"],
        ).single()["n"]
    )
    assert stranded == 0
    monkeypatch.undo()
    assert Journal(store).verify(ns)["verified"] and Journal(store).verify_live(ns)["verified"]
    # New facts about the merged-away key land on the entity it became.
    ingest(
        store,
        ns,
        "three",
        "Atlas API uses Postgres.",
        [other, PG],
        [{"subject": other["key"], "target": PG["key"], "relation": "uses_database"}],
    )
    recalled = store.recall(ns, "Atlas")
    assert [e["key"] for e in recalled["entities"]] == [PROJECT["key"]]
    lanes = [v for v in recalled.values() if isinstance(v, list)]
    targets = {f["target"] for lane in lanes for f in lane if isinstance(f, dict) and "target" in f}
    assert PG["key"] in targets
    assert Journal(store).verify_live(ns)["verified"]
