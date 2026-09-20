import os

import pytest

from graph_memory.feeds import worker_tick
from graph_memory.journal import Journal
from graph_memory.models import Scope, Transcript
from graph_memory.service import MemoryService
from graph_memory.status import status
from graph_memory.store import GraphStore
from tests.test_retry_feedback import candidate


def stage(store, ns):
    return store.stage(
        Transcript(
            namespace=ns,
            source_id="retry-budget",
            session_id="session",
            messages=[{"id": "m1", "role": "user", "content": "Atlas uses MySQL."}],
        )
    )["episode_id"]


def due(store, ns, eid):
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {id:$id,namespace:$ns}) SET e.retry_after=0",
            id=eid,
            ns=ns,
        ).consume()
    )


def test_commit_retry_after_restart_uses_durable_candidate(graph, monkeypatch):
    store, ns = graph
    eid = stage(store, ns)

    class Model:
        calls = 0

        def generate(self, *args):
            self.calls += 1
            return candidate()

    model = Model()

    def unavailable(*args, **kwargs):
        raise RuntimeError("Simulated exhausted database retries")

    monkeypatch.setattr(store, "commit", unavailable)
    result = worker_tick(MemoryService(store, model), ns)["receipts"][0]
    assert result["status"] == "failed"
    assert result["validation_failures"] == 0
    assert model.calls == 1
    assert status(store, Scope(namespace=ns))["processing"]["cached_extractions"] == 1
    assert Journal(store).verify(ns)["verified"]
    assert "cached_extraction" not in str(Journal(store).events(ns))

    restarted = GraphStore(
        os.environ["MEMORY_TEST_NEO4J_URI"], password=os.environ.get("MEMORY_TEST_NEO4J_PASSWORD")
    )
    try:
        due(store, ns, eid)
        fresh_model = Model()
        receipt = worker_tick(MemoryService(restarted, fresh_model), ns)["receipts"][0]
        assert receipt["status"] == "complete"
        assert fresh_model.calls == 0
        assert restarted.episode(ns, eid).get("cached_extraction") is None
        assert Journal(restarted).verify(ns)["verified"]
    finally:
        restarted.close()


def test_three_validation_failures_quarantine_without_losing_sources(graph):
    store, ns = graph
    eid = stage(store, ns)

    class Model:
        calls = 0
        unavailable = False

        def generate(self, *args):
            self.calls += 1
            if self.unavailable:
                raise RuntimeError("Codex extraction timed out; durable input can be retried")
            return candidate("invented quote")

    model = Model()
    service = MemoryService(store, model)
    for n in range(1, 4):
        due(store, ns, eid)
        result = worker_tick(service, ns)["receipts"][0]
        assert result["validation_failures"] == n
        assert result["quarantined"] == (n == 3)
        if n == 1:
            model.unavailable = True
            due(store, ns, eid)
            assert worker_tick(service, ns)["receipts"][0]["validation_failures"] == 1
            model.unavailable = False
    calls = model.calls
    due(store, ns, eid)
    assert worker_tick(service, ns)["receipts"] == []
    assert model.calls == calls
    stats = status(store, Scope(namespace=ns))
    assert stats["processing"]["quarantined"] == 1
    assert stats["processing"]["queued"] == 0
    assert store.episode(ns, eid)["payload"]
    assert Journal(store).verify(ns)["verified"]
    with pytest.raises(ValueError, match="No idle quarantined"):
        store.retry_quarantined(ns + "other", eid)
    assert store.retry_quarantined(ns, eid)["queued"]
    assert worker_tick(service, ns)["receipts"][0]["validation_failures"] == 1
    due(store, ns, eid)
    # A previous engine's budget must not quarantine a revised extractor.
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {id:$id}) SET e.quarantine_engine='old',e.validation_engine='old'",
            id=eid,
        ).consume()
    )
    assert worker_tick(service, ns)["receipts"][0]["validation_failures"] == 1


@pytest.mark.parametrize("field", ["cached_engine", "cached_model"])
def test_changed_engine_or_model_cannot_reuse_candidate(graph, monkeypatch, field):
    store, ns = graph
    eid = stage(store, ns)

    class Model:
        calls = 0

        def generate(self, *args):
            self.calls += 1
            return candidate()

    model = Model()
    store.cache_extraction(
        ns, eid, candidate(), {"provider": "Model", "model": None, "effort": None}
    )
    store.transaction(
        lambda tx: tx.run(
            f"MATCH (e:MemoryEpisode {{id:$id}}) SET e.{field}='old'",
            id=eid,
        ).consume()
    )
    assert worker_tick(MemoryService(store, model), ns)["receipts"][0]["status"] == "complete"
    assert model.calls == 1
