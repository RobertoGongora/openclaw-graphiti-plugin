"""An engine upgrade must not send quarantined work back to the model by itself."""

import os

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
