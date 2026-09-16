from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from graph_memory.models import DreamCreate, DreamOutput, DreamRequest, Extraction
from graph_memory.service import MemoryService

from .helpers import MYSQL, PG, PROJECT, ingest

pytestmark = pytest.mark.integration


def test_out_of_order_plans_inference_retract_and_restart(graph):
    store, ns = graph
    base = {"subject": "project:atlas", "relation": "uses_database", "slot": "primary"}
    new, _, _ = ingest(
        store,
        ns,
        "new",
        "Postgres is deployed as primary.",
        [PROJECT, PG],
        [{**base, "target": PG["key"], "valid_at": "2026-09-14T09:00:00Z"}],
    )
    ingest(
        store,
        ns,
        "old",
        "MySQL is the primary database.",
        [PROJECT, MYSQL],
        [{**base, "target": MYSQL["key"], "valid_at": "2026-09-10T09:00:00Z"}],
    )
    assert store.recall(ns, "Atlas")["current"][0]["target"] == PG["key"]
    assert (
        store.recall(ns, "Atlas", datetime.fromisoformat("2026-09-11T00:00:00+00:00"))["current"][
            0
        ]["target"]
        == MYSQL["key"]
    )
    store.retract(ns, new["fact_ids"][0], "Wrong deployment claim")
    ingest(
        store,
        ns,
        "plan",
        "Postgres is planned, not deployed.",
        [PROJECT, PG],
        [{**base, "target": PG["key"], "status": "planned", "valid_at": "2026-09-15T09:00:00Z"}],
    )
    context = store.recall(ns, "Atlas")
    assert context["current"][0]["target"] == MYSQL["key"]
    assert context["planned"][0]["target"] == PG["key"]
    framework = {"key": "framework:laravel", "name": "Laravel", "kind": "framework"}
    php = {"key": "language:php", "name": "PHP", "kind": "language"}
    receipt, _, _ = ingest(
        store,
        ns,
        "framework",
        "Atlas uses Laravel. Laravel is implemented in PHP.",
        [PROJECT, framework, php],
        [
            {
                "subject": PROJECT["key"],
                "relation": "uses_framework",
                "target": framework["key"],
                "valid_at": "2026-09-14T00:00:00Z",
            },
            {
                "subject": framework["key"],
                "relation": "implemented_in",
                "target": php["key"],
                "valid_at": "2026-09-14T00:00:00Z",
            },
        ],
    )
    assert store.recall(ns, "Atlas")["inferred"][0]["target"] == php["key"]
    store.retract(ns, receipt["fact_ids"][1], "Unsupported framework-language claim")
    assert store.recall(ns, "Atlas")["inferred"] == []


def test_habit_event_time_cross_sessions_and_idempotence(graph):
    store, ns = graph
    habit = {"key": "habit:rob:pushups", "name": "Pushups", "kind": "habit"}
    receipts = []
    for day in (15, 12, 14):
        event = {"key": f"event:pushups:2026-09-{day}", "name": f"Pushups {day}", "kind": "event"}
        receipt, transcript, extraction = ingest(
            store,
            ns,
            f"session-{day}",
            f"I did pushups on September {day} at 08:00 UTC.",
            [habit, event],
            [
                {
                    "subject": habit["key"],
                    "relation": "occurred",
                    "target": event["key"],
                    "valid_at": f"2026-09-{day}T08:00:00Z",
                }
            ],
        )
        receipts.append(receipt)
    assert store.latest(ns, "Pushups")["latest"]["valid_at"].startswith("2026-09-15")
    assert store.commit(ns, receipt["episode_id"], extraction)["replayed"]
    assert store.stage(transcript)["status"] == "complete"
    assert store.recall(ns, "Pushups")["totals"]["events"] == 3
    assert store.latest("different-namespace", "Pushups")["status"] == "not_found"
    # A different process/driver sees the committed last occurrence immediately.
    import os

    from graph_memory.store import GraphStore

    second = GraphStore(
        os.environ["MEMORY_TEST_NEO4J_URI"], password=os.environ.get("MEMORY_TEST_NEO4J_PASSWORD")
    )
    try:
        assert second.latest(ns, "Pushups")["latest"]["id"] == receipts[0]["fact_ids"][0]
    finally:
        second.close()


def test_atomic_evidence_rejection_pending_and_conflicts(graph):
    store, ns = graph
    fact = {
        "subject": PROJECT["key"],
        "relation": "uses_database",
        "target": MYSQL["key"],
        "slot": "primary",
        "valid_at": "2026-09-14T09:00:00Z",
    }
    receipt, transcript, extraction = ingest(
        store, ns, "one", "Atlas uses MySQL.", [PROJECT, MYSQL], [fact]
    )
    bad = transcript.model_copy(update={"source_id": "bad"})
    pending = store.stage(bad)
    raw = extraction.model_dump()
    raw["facts"][0]["evidence"][0]["quote"] = "fabricated quote"
    with pytest.raises(ValueError, match="exact substring"):
        store.commit(ns, pending["episode_id"], Extraction.model_validate(raw))
    assert store.recall(ns, "Atlas")["freshness"]["pending_episodes"] == 1
    assert store.recall(ns, "Atlas")["totals"]["current"] == 1
    ingest(store, ns, "two", "Atlas uses Postgres.", [PROJECT, PG], [{**fact, "target": PG["key"]}])
    context = store.recall(ns, "Atlas")
    assert context["current"] == []
    assert len(context["conflicts"]) == 2
    with pytest.raises(ValueError):
        store.commit("other", receipt["episode_id"], extraction)


def test_concurrent_retries_and_structural_healing(graph):
    store, ns = graph
    receipt, _, extraction = ingest(
        store,
        ns,
        "one",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [
            {
                "subject": PROJECT["key"],
                "relation": "uses_database",
                "target": MYSQL["key"],
                "valid_at": "2026-09-14T09:00:00Z",
            }
        ],
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: store.commit(ns, receipt["episode_id"], extraction), range(6))
        )
    assert all(r["replayed"] for r in results)
    store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFact {namespace:$ns})-[r:TARGET]->() DELETE r", ns=ns
        ).consume()
    )
    assert store.repair(ns) == {
        "checked_facts": 1,
        "orphaned_facts": 0,
        "temporal_projection": "recomputed on every read",
    }


def test_dream_separate_output_stale_guard_and_dependency_invalidation(graph):
    store, ns = graph
    receipt, _, _ = ingest(
        store,
        ns,
        "one",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [
            {
                "subject": PROJECT["key"],
                "relation": "uses_database",
                "target": MYSQL["key"],
                "valid_at": "2026-09-14T09:00:00Z",
            }
        ],
    )

    class FakeLLM:
        def generate(self, instructions, payload, output):
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

    service = MemoryService(store, FakeLLM())
    created = service.dream_create(
        DreamCreate(namespace=ns, query="Atlas", episode_ids=[receipt["episode_id"]])
    )
    request = DreamRequest(namespace=ns, dream_id=created["dream_id"])
    assert service.dream_run(request)["status"] == "completed"
    assert store.recall(ns, "Atlas")["insights"] == []
    assert service.dream_apply(request)["insights"] == 1
    assert len(store.recall(ns, "Atlas")["insights"]) == 1
    created = service.dream_create(
        DreamCreate(namespace=ns, query="Atlas", episode_ids=[receipt["episode_id"]])
    )
    request = DreamRequest(namespace=ns, dream_id=created["dream_id"])
    service.dream_run(request)
    store.retract(ns, receipt["fact_ids"][0], "Incorrect")
    assert store.recall(ns, "Atlas")["insights"] == []
    with pytest.raises(ValueError, match="stale"):
        service.dream_apply(request)


def test_same_named_habits_keep_distinct_qualified_owners(graph):
    store, ns = graph
    for owner, date in (("alice", "2026-09-14T09:00:00Z"), ("bob", "2026-09-15T09:00:00Z")):
        habit = {"key": f"habit:{owner}:pushups", "name": "Pushups", "kind": "habit"}
        event = {"key": f"event:{owner}:one", "name": "Pushups repetition", "kind": "event"}
        ingest(
            store,
            ns,
            owner,
            f"{owner} did pushups.",
            [habit, event],
            [
                {
                    "subject": habit["key"],
                    "target": event["key"],
                    "relation": "occurred",
                    "valid_at": date,
                }
            ],
        )
    assert store.latest(ns, "Pushups")["status"] == "ambiguous"
    alice = store.latest(ns, "habit:alice:pushups")
    bob = store.latest(ns, "habit:bob:pushups")
    assert alice["latest"]["valid_at"] == "2026-09-14T09:00:00+00:00"
    assert bob["latest"]["valid_at"] == "2026-09-15T09:00:00+00:00"


def test_extraction_repairs_bad_quote_before_atomic_commit(graph):
    from graph_memory.models import Ingest, Transcript

    store, ns = graph
    source = Transcript(
        namespace=ns,
        source_id="repair",
        session_id="s",
        messages=[
            {
                "id": "m",
                "role": "user",
                "timestamp": "2026-09-15T10:00:00Z",
                "content": "Atlas uses MySQL.",
            }
        ],
    )

    class Model:
        calls = 0

        def generate(self, instructions, payload, output):
            self.calls += 1
            if self.calls == 2:
                assert "validation_error" in payload
                assert store.recall(ns, "Atlas")["current"] == []
            return Extraction(
                entities=[PROJECT, MYSQL],
                facts=[
                    {
                        "subject": PROJECT["key"],
                        "target": MYSQL["key"],
                        "relation": "uses_database",
                        "valid_at": "2026-09-15T10:00:00Z",
                        "summary": "Atlas uses MySQL.",
                        "evidence": [
                            {
                                "message_id": "m",
                                "quote": "Atlas uses MySQL."
                                if self.calls == 2
                                else "Invented quote",
                            }
                        ],
                    }
                ],
            )

    model = Model()
    result = MemoryService(store, model).ingest(Ingest(transcript=source, extract=True))
    assert result["status"] == "complete"
    assert model.calls == 2
    assert len(store.recall(ns, "Atlas")["current"]) == 1


def test_undated_habit_claim_is_preserved_without_fabricating_latest_time(graph):
    from graph_memory.models import Transcript

    store, ns = graph
    habit = {"key": "habit:rob:pushups", "name": "Pushups", "kind": "habit"}
    dated = {"key": "event:dated", "name": "Dated repetition", "kind": "event"}
    ingest(
        store,
        ns,
        "dated",
        "Did pushups on September 14.",
        [habit, dated],
        [
            {
                "subject": habit["key"],
                "target": dated["key"],
                "relation": "occurred",
                "valid_at": "2026-09-14T09:00:00Z",
            }
        ],
    )
    source = Transcript(
        namespace=ns,
        source_id="undated",
        session_id="notes",
        source_kind="memory_import",
        source_updated_at="2026-09-15T10:00:00Z",
        messages=[{"id": "m", "role": "note", "content": "Rob did pushups."}],
    )
    event = {"key": "event:undated", "name": "Undated repetition", "kind": "event"}
    extraction = Extraction(
        entities=[habit, event],
        facts=[
            {
                "subject": habit["key"],
                "target": event["key"],
                "relation": "occurred",
                "status": "uncertain",
                "summary": "An undated repetition was reported.",
                "evidence": [{"message_id": "m", "quote": "Rob did pushups."}],
            }
        ],
    )
    receipt = store.stage(source)
    store.commit(ns, receipt["episode_id"], extraction)
    result = store.latest(ns, "Pushups")
    assert result["status"] == "uncertain"
    assert result["latest"]["target"] == dated["key"]
    assert result["unresolved"][0]["documented_at"] == "2026-09-15T10:00:00+00:00"
    assert result["unresolved"][0].get("valid_at") is None
