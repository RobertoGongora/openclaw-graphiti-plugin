from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from graph_memory.journal import Journal, capture
from graph_memory.models import (
    DreamCreate,
    DreamOutput,
    DreamRequest,
    EpisodeRequest,
    Message,
    Transcript,
)
from graph_memory.service import MemoryService

from .helpers import MYSQL, PG, PROJECT, ingest


def fact(store, ns, source="one", project=PROJECT, target=MYSQL, day=10):
    return ingest(
        store,
        ns,
        source,
        f"{project['name']} uses {target['name']}.",
        [project, target],
        [
            {
                "subject": project["key"],
                "target": target["key"],
                "relation": "uses_database",
                "slot": "primary",
                "valid_at": f"2026-09-{day:02d}T10:00:00Z",
            }
        ],
    )


def test_two_clocks_retraction_and_read_parity(graph):
    store, ns = graph
    journal = Journal(store)
    receipt, transcript, extraction = fact(store, ns)
    first = journal.snapshot(ns)
    assert (
        store.recall(ns, "Atlas")["current"]
        == store.recall(ns, "Atlas", at_change=first["sequence"])["current"]
    )
    fact(store, ns, source="new", target=PG, day=15)
    assert store.latest(ns, "Atlas")["latest"]["target"] == PG["key"]
    assert (
        store.latest(ns, "Atlas", known_at=datetime.fromisoformat(first["known_at"]))["latest"][
            "target"
        ]
        == MYSQL["key"]
    )
    assert (
        store.recall(ns, "Atlas", datetime(2026, 9, 12, tzinfo=UTC))["current"][0]["target"]
        == MYSQL["key"]
    )
    before_retract = journal.snapshot(ns)
    store.retract(ns, receipt["fact_ids"][0], "Incorrect attribution")
    old = store.recall(ns, "Atlas", at_change=first["sequence"])
    assert old["current"][0]["target"] == MYSQL["key"]
    assert store.recall(ns, "Atlas", datetime(2026, 9, 12, tzinfo=UTC))["current"] == []
    size = len(journal.events(ns))
    assert store.stage(transcript)["status"] == "complete"
    assert store.commit(ns, receipt["episode_id"], extraction)["replayed"]
    assert len(journal.events(ns)) == size
    assert (
        journal.snapshot(ns, sequence=before_retract["sequence"])["state"]["MemoryFact"][
            receipt["fact_ids"][0]
        ]["retracted"]
        is False
    )


def test_merge_replay_preserves_old_identity_and_isolates_target(graph):
    store, ns = graph
    alternate = {**PROJECT, "key": "project:atlas-old", "name": "Atlas legacy"}
    fact(store, ns)
    receipt, transcript, _ = fact(store, ns, source="alt", project=alternate, target=PG)
    journal = Journal(store)
    before = journal.snapshot(ns)
    store.merge(ns, alternate["key"], PROJECT["key"], "Confirmed same project")
    assert len(store.recall(ns, "Atlas")["conflicts"]) == 2
    old = store.recall(ns, alternate["key"], at_change=before["sequence"])
    assert old["current"][0]["subject"] == alternate["key"]
    assert old["current"][0]["target"] == PG["key"]
    target = "replay:" + ns
    try:
        journal.replay(ns, target, sequence=before["sequence"])
        assert store.recall(target, alternate["key"])["current"][0]["subject"] == alternate["key"]
        with pytest.raises(ValueError, match="read-only"):
            store.retract(target, store.recall(target, alternate["key"])["current"][0]["id"], "no")
        with pytest.raises(ValueError, match="read-only"):
            MemoryService(store).extract(EpisodeRequest(namespace=target, episode_id="unused"))
        with pytest.raises(ValueError, match="already exists"):
            journal.replay(ns, target)
        assert journal.snapshot(ns)["sequence"] == before["sequence"] + 1
    finally:
        store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR (n:MemoryChange AND n.scope=$ns) OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                ns=target,
            ).consume()
        )


def test_dream_publication_and_support_retraction_have_history(graph):
    store, ns = graph
    receipt, _, _ = fact(store, ns)

    class Model:
        def generate(self, *args):
            return DreamOutput(
                insights=[
                    {
                        "summary": "Atlas depends on MySQL.",
                        "entity_keys": [PROJECT["key"]],
                        "supporting_fact_ids": receipt["fact_ids"],
                        "confidence": 0.9,
                    }
                ],
                observations=[],
            )

    service = MemoryService(store, Model())
    created = service.dream_create(
        DreamCreate(namespace=ns, query="Atlas", episode_ids=[receipt["episode_id"]])
    )
    request = DreamRequest(namespace=ns, dream_id=created["dream_id"])
    service.dream_run(request)
    journal = Journal(store)
    before = journal.snapshot(ns)["sequence"]
    service.dream_apply(request)
    published = journal.snapshot(ns)["sequence"]
    assert store.recall(ns, "Atlas", at_change=before)["insights"] == []
    assert len(store.recall(ns, "Atlas", at_change=published)["insights"]) == 1
    service.dream_apply(request)
    assert journal.snapshot(ns)["sequence"] == published
    store.retract(ns, receipt["fact_ids"][0], "Wrong")
    assert store.recall(ns, "Atlas")["insights"] == []
    assert len(store.recall(ns, "Atlas", at_change=published)["insights"]) == 1


def test_journal_atomic_rollback_concurrent_staging_and_integrity(graph):
    store, ns = graph
    journal = Journal(store)
    transcript = Transcript(
        namespace=ns,
        source_id="s",
        session_id="s",
        messages=[Message(id="1", role="user", content="Hello")],
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: store.stage(transcript), range(8)))
    assert [e["kind"] for e in journal.events(ns)] == ["baseline", "source_saved"]
    before = journal.snapshot(ns)

    def fail(tx):
        tx.run(
            'CREATE (:MemoryEntity {id:$id,namespace:$ns,key:"broken"})', id=ns + ":broken", ns=ns
        ).consume()
        raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        store.transaction(lambda tx: store.mutate(tx, ns, "test", {}, fail))
    assert journal.snapshot(ns) == before
    store.transaction(
        lambda tx: tx.run(
            'MATCH (e:MemoryChange {scope:$ns,sequence:1}) SET e.payload="{}"', ns=ns
        ).consume()
    )
    with pytest.raises(ValueError, match="integrity"):
        journal.snapshot(ns)


def test_bootstrap_coverage_and_checkpoint_reconstruction(graph):
    store, ns = graph
    journal = Journal(store)
    # Existing state is a baseline, never fabricated historical mutations.
    receipt, _, _ = fact(store, ns)
    store.transaction(
        lambda tx: tx.run("MATCH (e:MemoryChange {scope:$ns}) DETACH DELETE e", ns=ns).consume()
    )
    store.transaction(
        lambda tx: tx.run(
            "MATCH (s:MemorySpace {id:$ns}) REMOVE s.journal_sequence,s.journal_hash,s.journal_us,s.journal_state_hash",
            ns=ns,
        ).consume()
    )
    journal.initialize(ns)
    snapshot = journal.snapshot(ns)
    assert snapshot["sequence"] == 0
    with pytest.raises(ValueError, match="predates"):
        journal.snapshot(
            ns, known_at=datetime.fromisoformat(snapshot["known_at"]) - timedelta(seconds=1)
        )
    # Exercise checkpoint boundary without model calls or changing input evidence.
    for i in range(100):
        store.retract(ns, receipt["fact_ids"][0], f"Correction note {i}")
    final = journal.snapshot(ns)
    assert final["sequence"] == 100
    live = store.transaction(lambda tx: capture(tx, ns))
    assert final["state"] == live
    assert (
        journal.snapshot(ns, sequence=99)["state"]["MemoryFact"][receipt["fact_ids"][0]][
            "retraction_reason"
        ]
        == "Correction note 98"
    )
    with pytest.raises(ValueError, match="does not exist"):
        journal.snapshot(ns, sequence=101)


def test_late_arriving_fact_has_independent_event_and_knowledge_cutoffs(graph):
    store, ns = graph
    fact(store, ns, source="newer-first", target=PG, day=15)
    journal = Journal(store)
    first = journal.snapshot(ns)
    fact(store, ns, source="older-later", target=MYSQL, day=10)
    past = datetime(2026, 9, 12, tzinfo=UTC)
    assert store.recall(ns, "Atlas", as_of=past, at_change=first["sequence"])["current"] == []
    assert store.recall(ns, "Atlas", as_of=past)["current"][0]["target"] == MYSQL["key"]
    assert store.recall(ns, "Atlas")["current"][0]["target"] == PG["key"]
    assert journal.verify(ns)["verified"]
