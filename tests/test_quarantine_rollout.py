"""An engine upgrade must not send quarantined work back to the model by itself."""

import os
import time

import pytest

from graph_memory import store as store_module
from graph_memory.feeds import worker_tick
from graph_memory.follow import follow_once
from graph_memory.models import Scope
from graph_memory.service import MemoryService
from graph_memory.status import status
from graph_memory.store import GraphStore
from tests.test_retry_budget import due, stage
from tests.test_retry_feedback import candidate
from tests.test_session_sources import claude


class Model:
    def __init__(self, quote="Atlas uses MySQL."):
        self.quote, self.calls = quote, 0

    def generate(self, *args):
        self.calls += 1
        return candidate(self.quote)


def quarantine(service, ns, eid):
    for _ in range(3):
        due(service.store, ns, eid)
        receipt = worker_tick(service, ns)["receipts"][0]
    assert receipt["quarantined"]


@pytest.fixture
def upgraded(graph, monkeypatch):
    """The same database seen by a process whose engine files differ."""
    store, ns = graph
    monkeypatch.setattr(store_module, "engine_fingerprint", lambda fresh=False: "engine-b")
    new = GraphStore(
        os.environ["MEMORY_TEST_NEO4J_URI"], password=os.environ.get("MEMORY_TEST_NEO4J_PASSWORD")
    )
    assert new.engine == "engine-b" != store.engine
    yield store, new, ns
    new.close()


def test_new_engine_leaves_old_quarantine_and_runs_new_work(upgraded):
    old, new, ns = upgraded
    held = stage(old, ns)
    quarantine(MemoryService(old, Model("invented quote")), ns, held)
    before = old.episode(ns, held)
    old.transaction(
        lambda tx: tx.run("MATCH (e:MemoryEpisode {id:$id}) SET e.retry_after=0", id=held).consume()
    )

    model = Model()
    service = MemoryService(new, model)
    assert worker_tick(service, ns)["receipts"] == []
    assert model.calls == 0
    processing = status(new, Scope(namespace=ns))["processing"]
    assert (processing["quarantined"], processing["queued"]) == (1, 0)
    after = new.episode(ns, held)
    for key in ("status", "quarantine_engine", "quarantine_reason", "attempts", "retry_feedback"):
        assert after.get(key) == before.get(key)

    # New work is not held back by the quarantine.
    fresh = new.stage(candidate_transcript(ns, "fresh", "Atlas uses MySQL."))["episode_id"]
    receipts = worker_tick(service, ns)["receipts"]
    assert [(r["episode_id"], r["status"]) for r in receipts] == [(fresh, "complete")]

    # An explicit per-episode retry still runs it under the new engine.
    assert new.retry_quarantined(ns, held)["queued"]
    receipts = worker_tick(service, ns)["receipts"]
    assert [(r["episode_id"], r["status"]) for r in receipts] == [(held, "complete")]
    assert new.episode(ns, held)["engine"] == "engine-b"


def test_old_quarantine_does_not_fill_the_intake_gate(upgraded, tmp_path, monkeypatch):
    old, new, ns = upgraded
    quarantine(MemoryService(old, Model("invented quote")), ns, stage(old, ns))
    old.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) SET e.retry_after=0", ns=ns
        ).consume()
    )
    monkeypatch.setenv("MEMORY_INTAKE_QUEUE", "1")
    root = tmp_path / "claude"
    (root / "-Users-rob").mkdir(parents=True)
    (root / "-Users-rob" / "new.jsonl").write_text(
        claude("user", "Atlas uses MySQL in production.") + claude("assistant", "Noted.")
    )
    staged = follow_once(MemoryService(new, Model()), ns, [root], {}, source_records=True)
    assert [r["episode_id"] for out in staged for r in out["receipts"]]


def candidate_transcript(ns, source, text):
    from graph_memory.models import Transcript

    return Transcript(
        namespace=ns,
        source_id=source,
        session_id=source,
        messages=[{"id": "m1", "role": "user", "content": text}],
    )


def held(store, ns, eid):
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {id:$id}) SET e.status='failed',e.retry_after=0,"
            "e.quarantine_engine=$engine,e.quarantine_reason='evidence_quote_mismatch'",
            id=eid,
            engine=store.engine,
        ).consume()
    )


def test_sync_ingest_extract_and_commit_leave_a_held_episode_alone(graph):
    from graph_memory.models import Commit, EpisodeRequest, Ingest, Pending

    store, ns = graph
    source = candidate_transcript(ns, "held", "Atlas uses MySQL.")
    eid = store.stage(source)["episode_id"]
    held(store, ns, eid)
    fresh = store.stage(candidate_transcript(ns, "fresh", "Atlas uses MySQL."))["episode_id"]
    model = Model()
    service = MemoryService(store, model)

    # import --extract and ingest --extract stage the same content again, then extract.
    receipt = service.ingest(Ingest(transcript=source, extract=True))
    assert (receipt["episode_id"], receipt["status"]) == (eid, "held")
    assert service.ingest(Ingest(transcript=source))["processing"] == "held"
    assert service.call("memory_extract", {"namespace": ns, "episode_id": eid})["status"] == "held"
    with pytest.raises(ValueError, match="quarantined"):
        service.call(
            "memory_commit",
            Commit(namespace=ns, episode_id=eid, extraction=candidate()).model_dump(mode="json"),
        )
    assert model.calls == 0
    episode = store.episode(ns, eid)
    assert (episode["status"], episode["quarantine_engine"]) == ("failed", store.engine)
    pending = service.call("memory_pending", Pending(namespace=ns).model_dump())["episodes"]
    assert [p["episode_id"] for p in pending] == [fresh]

    # The explicit per-episode release still works through the same path.
    assert store.retry_quarantined(ns, eid)["queued"]
    assert service.ingest(Ingest(transcript=source, extract=True))["status"] == "complete"
    assert service.extract(EpisodeRequest(namespace=ns, episode_id=fresh))["status"] == "complete"


def test_hold_written_while_a_claim_waits_on_the_node_lock_wins(graph):
    import threading

    store, ns = graph
    eid = stage(store, ns)
    model = Model()
    service = MemoryService(store, model)
    result = {}
    # A separate session: store.transaction would serialise the two in-process.
    with store.driver.session(database=store.database) as session:
        tx = session.begin_transaction()
        tx.run(
            "MATCH (e:MemoryEpisode {id:$id}) SET e.quarantine_engine=$engine,"
            "e.quarantine_reason='operator_hold',e.retry_after=0",
            id=eid,
            engine=store.engine,
        ).consume()
        worker = threading.Thread(target=lambda: result.update(worker_tick(service, ns)))
        worker.start()
        # The worker read the committed, unheld state and now waits for the lock.
        worker.join(2)
        assert worker.is_alive()
        tx.commit()
    worker.join(60)
    assert result["receipts"] == []
    assert model.calls == 0
    assert store.episode(ns, eid)["quarantine_reason"] == "operator_hold"


def test_sync_run_leases_the_episode_so_a_worker_cannot_hold_it_midway(graph):
    import threading

    from graph_memory.models import EpisodeRequest

    store, ns = graph
    eid = stage(store, ns)
    bad = MemoryService(store, Model("invented quote"))
    for _ in range(2):  # One more charged failure would hold it.
        due(store, ns, eid)
        worker_tick(bad, ns)
    due(store, ns, eid)
    good = Model()
    sync = MemoryService(store, good)
    prepare, inside, go = sync.prepare, threading.Event(), threading.Event()

    def paused(request):
        inside.set()
        assert go.wait(10)
        return prepare(request)

    sync.prepare = paused
    result = {}
    run = threading.Thread(
        target=lambda: result.update(sync.extract(EpisodeRequest(namespace=ns, episode_id=eid)))
    )
    run.start()
    assert inside.wait(10)
    try:
        assert worker_tick(bad, ns)["receipts"] == []
    finally:
        go.set()
    run.join(30)
    assert result["status"] == "complete" and good.calls == 1
    assert store.episode(ns, eid).get("quarantine_engine") is None


def test_an_episode_a_worker_leased_is_not_extracted_synchronously(graph):
    from graph_memory.models import Ingest

    store, ns = graph
    source = candidate_transcript(ns, "leased", "Atlas uses MySQL.")
    eid = store.stage(source)["episode_id"]
    assert store.claim(ns, eid, time.time() + 600, "worker-token")
    model = Model()
    receipt = MemoryService(store, model).ingest(Ingest(transcript=source, extract=True))
    assert (receipt["status"], model.calls) == ("leased", 0)
    assert store.episode(ns, eid)["worker"] == "worker-token"


def test_hold_written_during_a_sync_model_call_blocks_checkpoint_and_commit(graph):
    from graph_memory.models import EpisodeRequest

    store, ns = graph
    eid = stage(store, ns)

    class Racing(Model):
        def generate(self, *args):
            result = super().generate(*args)
            held(store, ns, eid)  # Set by someone else while the call was in flight.
            return result

    model = Racing()
    with pytest.raises(ValueError, match="quarantined"):
        MemoryService(store, model).extract(EpisodeRequest(namespace=ns, episode_id=eid))
    episode = store.episode(ns, eid)
    assert model.calls == 1  # The call already in flight, and no other.
    assert episode["status"] != "complete" and episode.get("cached_extraction") is None
    assert episode["quarantine_engine"] == store.engine


def test_held_prepare_and_commit_make_no_model_call(graph):
    from tests.test_claim_support import SupportModel, pair

    store, ns = graph
    source, extraction = pair()
    source.namespace = ns
    eid = store.stage(source)["episode_id"]
    held(store, ns, eid)
    model = SupportModel()
    service = MemoryService(store, model)
    packet = service.call("memory_prepare", {"namespace": ns, "episode_id": eid})
    assert (packet["status"], packet["next_tool"]) == ("held", None)
    assert "transcript" not in packet
    with pytest.raises(ValueError, match="quarantined"):
        service.call(
            "memory_commit",
            {"namespace": ns, "episode_id": eid, "extraction": extraction.model_dump(mode="json")},
        )
    assert model.payloads == []


def test_checkpoint_waiting_on_a_hold_writer_does_not_save_onto_the_held_episode(graph):
    import threading

    store, ns = graph
    eid = stage(store, ns)
    assert store.claim(ns, eid, time.time() + 600, "token")
    done = threading.Event()
    with store.driver.session(database=store.database) as session:
        writer = session.begin_transaction()
        writer.run(
            "MATCH (e:MemoryEpisode {id:$id}) SET e.quarantine_engine=$engine", id=eid, engine="x"
        ).consume()
        checkpoint = threading.Thread(
            target=lambda: (
                store.cache_extraction(ns, eid, candidate(), {"provider": "Model"}, "token"),
                done.set(),
            )
        )
        checkpoint.start()
        try:
            for _ in range(200):  # The checkpoint read the unheld state and waits for the lock.
                if store.read(
                    lambda tx: tx.run(
                        "SHOW TRANSACTIONS YIELD currentQuery,status "
                        "WHERE currentQuery CONTAINS 'cached_extraction' "
                        "AND status STARTS WITH 'Blocked' RETURN count(*) AS n"
                    ).single()["n"]
                ):
                    break
                time.sleep(0.025)
            else:
                pytest.fail("the checkpoint never waited on the episode lock")
            assert not done.is_set()
        finally:
            writer.commit()
    checkpoint.join(30)
    assert done.is_set()
    assert store.episode(ns, eid).get("cached_extraction") is None


def test_a_stale_release_waiting_behind_a_new_claim_keeps_the_new_lease(graph):
    import threading

    store, ns = graph
    eid = stage(store, ns)
    assert store.claim(ns, eid, time.time() - 1, "old")  # Expired: free to claim again.
    successor = GraphStore(
        os.environ["MEMORY_TEST_NEO4J_URI"], password=os.environ.get("MEMORY_TEST_NEO4J_PASSWORD")
    )
    wrote, finish = threading.Event(), threading.Event()
    transaction = successor.transaction

    def paused(fn, *args):
        def run(tx, *args):
            result = fn(tx, *args)
            wrote.set()  # The claim holds the episode lock, uncommitted.
            assert finish.wait(10)
            return result

        return transaction(run, *args)

    successor.transaction = paused
    lease = time.time() + 600
    result = {}
    claim = threading.Thread(
        target=lambda: result.update(new=successor.claim(ns, eid, lease, "new"))
    )
    release = threading.Thread(target=lambda: store.release(ns, eid, "old"))
    try:
        claim.start()
        assert wrote.wait(10)
        release.start()
        for _ in range(200):
            if store.read(
                lambda tx: tx.run(
                    "SHOW TRANSACTIONS YIELD currentQuery,status "
                    "WHERE currentQuery CONTAINS 'SET e.lease_until=0' "
                    "AND status STARTS WITH 'Blocked' RETURN count(*) AS n"
                ).single()["n"]
            ):
                break
            time.sleep(0.025)
        else:
            pytest.fail("the stale release never waited on the episode lock")
    finally:
        finish.set()
        claim.join(30)
        release.join(30)
        successor.close()
    assert result["new"]
    episode = store.episode(ns, eid)
    assert (episode["worker"], episode["lease_until"]) == ("new", lease)
    assert not store.claim(ns, eid, time.time() + 600, "third")
