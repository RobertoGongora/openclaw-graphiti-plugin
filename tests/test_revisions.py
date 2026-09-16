import pytest

from evals.run import load_cases, suite_fingerprint
from graph_memory.models import DreamCreate, DreamOutput, DreamRequest
from graph_memory.revisions import Revisions
from graph_memory.service import MemoryService
from graph_memory.version import engine_fingerprint

from .helpers import MYSQL, PG, PROJECT, ingest

pytestmark = pytest.mark.integration


def report():
    return {
        "engine": engine_fingerprint(),
        "suite": suite_fingerprint(),
        "passed": True,
        "deterministic_passed": True,
        "model": "test",
        "reasoning_effort": "xhigh",
        "results": [{"case": c["id"], "passed": True} for c in load_cases()],
    }


def test_shadow_replay_diff_gates_promotion_and_dream_revalidation(graph):
    store, ns = graph
    receipt, _, original = ingest(
        store,
        ns,
        "source",
        "Atlas uses MySQL. PostgreSQL is planned.",
        [PROJECT, MYSQL, PG],
        [
            {
                "subject": PROJECT["key"],
                "relation": "uses_database",
                "target": MYSQL["key"],
                "valid_at": "2026-09-14T09:00:00Z",
            }
        ],
    )

    class Model:
        model, effort = "test", "xhigh"

        def generate(self, instructions, payload, output):
            if output == DreamOutput:
                f = payload["graph"]["current"][0]
                return DreamOutput(
                    insights=[
                        {
                            "summary": "Atlas runs MySQL; a migration needs deployment verification.",
                            "entity_keys": [PROJECT["key"]],
                            "supporting_fact_ids": [f["id"]],
                            "confidence": 0.9,
                        }
                    ],
                    observations=[],
                )
            result = original.model_copy(deep=True)
            result.facts.append(
                result.facts[0].model_copy(update={"target": PG["key"], "status": "planned"})
            )
            return result

    service = MemoryService(store, Model())
    d = service.dream_create(
        DreamCreate(namespace=ns, query="Atlas", episode_ids=[receipt["episode_id"]])
    )
    dr = DreamRequest(namespace=ns, dream_id=d["dream_id"])
    service.dream_run(dr)
    service.dream_apply(dr)
    revisions = Revisions(service)
    created = revisions.create(ns, [receipt["episode_id"]])
    rid = created["revision_id"]
    try:
        assert created["affected_dreams"] == [d["dream_id"]]
        result = revisions.build(ns, rid)
        assert len(result["diff"]["added"]) == 1
        assert store.recall(ns, "Atlas")["planned"] == []
        with pytest.raises(ValueError, match="Validate"):
            revisions.promote(ns, rid)
        checks = [
            {
                "tool": "memory_recall",
                "arguments": {"query": "Atlas"},
                "path": "planned",
                "contains": {"relation": "uses_database", "target_contains": "postgre"},
            }
        ]
        assert revisions.validate(ns, rid, report(), checks)["passed"]
        with pytest.raises(ValueError, match="Behavior changed"):
            revisions.promote(ns, rid)
        result = revisions.promote(ns, rid, result["diff"]["digest"])
        assert result["status"] == "promoted"
        assert store.recall(ns, "Atlas")["planned"][0]["target"] == PG["key"]
        assert store.recall(ns, "Atlas")["current"][0]["target"] == MYSQL["key"]
        assert revisions.promote(ns, rid)["replayed"]
    finally:
        store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                ns=created["candidate"],
            ).consume()
        )


def test_live_writes_after_validation_block_promotion(graph):
    store, ns = graph
    receipt, _, original = ingest(
        store,
        ns,
        "source",
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

    class Model:
        model, effort = "test", "xhigh"

        def generate(self, *args):
            return original

    revisions = Revisions(MemoryService(store, Model()))
    created = revisions.create(ns, [receipt["episode_id"]])
    rid = created["revision_id"]
    try:
        revisions.build(ns, rid)
        checks = [
            {
                "tool": "memory_recall",
                "arguments": {"query": "Atlas"},
                "path": "current",
                "contains": {"target_contains": "mysql"},
            }
        ]
        revisions.validate(ns, rid, report(), checks)
        store.retract(ns, receipt["fact_ids"][0], "Concurrent correction")
        with pytest.raises(ValueError, match="changed since"):
            revisions.promote(ns, rid)
        assert store.recall(ns, "Atlas")["current"] == []
    finally:
        store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                ns=created["candidate"],
            ).consume()
        )
