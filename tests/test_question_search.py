"""Question lookup must find facts without weakening identity or evidence rules."""

from types import SimpleNamespace

from graph_memory import retrieval as v
from graph_memory.journal import Journal
from graph_memory.mcp import Protocol
from graph_memory.service import MemoryService

from .helpers import MYSQL, PG, PROJECT, ingest
from .test_contracts import rpc

KETCH = {"key": "service:ketch", "name": "Ketch", "kind": "service"}
DROP = {"key": "project:drop", "name": "DROP", "kind": "project"}
QUESTION = "Ketch DROP upload batching CalPrivacy one CSV per list"


def test_phrase_search_finds_facts_and_entity_miss_guides_recovery(graph):
    store, ns = graph
    receipt, _, _ = ingest(
        store,
        ns,
        "ketch-update",
        "Ketch released an update to reduce DROP uploads.",
        [KETCH, DROP],
        [
            {
                "subject": KETCH["key"],
                "relation": "related_to",
                "valid_at": "2026-09-14T10:00:00Z",
                "target": DROP["key"],
            }
        ],
    )
    request = v.QuestionSearch(namespace=ns, question=QUESTION)
    miss = v.search_entities(store, v.EntitySearch(namespace=ns, query=QUESTION))
    assert miss["total"] == 0
    assert miss["suggested_call"] == {"tool": "memory_search", "arguments": {"question": QUESTION}}
    assert (
        v.search_entities(store, v.EntitySearch(namespace=ns, query="Ketch"))["matches"][0]["key"]
        == KETCH["key"]
    )
    found = v.search(store, request)
    assert found["status"] == "found"
    assert found["facts"][0]["id"] == receipt["fact_ids"][0]
    assert found["facts"][0]["lane"] == "current"
    assert found["evidence_tool"] == "memory_evidence"
    assert not v.search(store, request.model_copy(update={"namespace": ns + ":other"}))["facts"]
    p = Protocol(MemoryService(store), namespace=ns, read_only=True)
    result = p.dispatch(rpc("tools/call", name="memory_search", arguments={"question": QUESTION}))[
        1
    ]["result"]
    assert not result["isError"]
    assert result["structuredContent"]["facts"] == found["facts"]
    assert (
        p.dispatch(
            rpc(
                "tools/call",
                name="memory_search",
                arguments={"question": QUESTION, "namespace": "other"},
            )
        )[0]
        == 403
    )


def test_search_keeps_replacement_and_historical_knowledge(graph):
    store, ns = graph
    old, _, _ = ingest(
        store,
        ns,
        "old",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [
            {
                "subject": PROJECT["key"],
                "relation": "uses_database",
                "target": MYSQL["key"],
                "slot": "primary",
                "valid_at": "2026-09-01T00:00:00Z",
            }
        ],
    )
    snapshot = Journal(store).snapshot(ns)
    new, _, _ = ingest(
        store,
        ns,
        "new",
        "Atlas uses Postgres.",
        [PROJECT, PG],
        [
            {
                "subject": PROJECT["key"],
                "relation": "uses_database",
                "target": PG["key"],
                "slot": "primary",
                "valid_at": "2026-09-02T00:00:00Z",
            }
        ],
    )
    r = v.QuestionSearch(namespace=ns, question="MySQL")
    assert [f["id"] for f in v.search(store, r)["facts"]] == new["fact_ids"]
    historic = v.search(store, r.model_copy(update={"at_change": snapshot["sequence"]}))
    assert [f["id"] for f in historic["facts"]] == old["fact_ids"]
    assert historic["knowledge_history"]["sequence"] == snapshot["sequence"]
    miss = v.search_entities(
        store, v.EntitySearch(namespace=ns, query="Missing phrase", at_change=snapshot["sequence"])
    )
    assert miss["suggested_call"]["arguments"]["at_change"] == snapshot["sequence"]
    assert {
        f["id"] for f in v.search(store, r.model_copy(update={"include_history": True}))["facts"]
    } == set(old["fact_ids"] + new["fact_ids"])


def test_question_search_spans_subjects_pages_and_keeps_uncertainty(graph):
    store, ns = graph
    for i, (subject, state) in enumerate([(KETCH, "active"), (PROJECT, "uncertain")]):
        ingest(
            store,
            ns,
            str(i),
            f"{subject['name']} may reduce upload batching.",
            [subject, DROP],
            [
                {
                    "subject": subject["key"],
                    "relation": "related_to",
                    "valid_at": "2026-09-14T10:00:00Z",
                    "target": DROP["key"],
                    "status": state,
                }
            ],
        )
    r = v.QuestionSearch(namespace=ns, question="upload batching", limit=1)
    first = v.search(store, r)
    second = v.search(store, r.model_copy(update={"offset": first["next_offset"]}))
    assert first["status"] == "found"  # Several subjects do not make a topic ambiguous.
    assert first["facts"][0]["lane"] == "current"
    assert second["facts"][0]["lane"] == "uncertain"
    assert first["facts"][0]["id"] != second["facts"][0]["id"]
    assert second["next_offset"] is None
    assert not v.search(store, r.model_copy(update={"question": "unfindablequux"}))["facts"]


def test_search_requires_content_words_without_reading_graph():
    result = v.search(SimpleNamespace(), v.QuestionSearch(namespace="test", question="what is the"))
    assert result["status"] == "needs_search_terms"
    assert result["facts"] == []


def test_search_preserves_conflicts_retractions_and_event_time(graph):
    from datetime import datetime

    store, ns = graph
    receipts = []
    for name, target in [("first", MYSQL), ("second", PG)]:
        receipt, _, _ = ingest(
            store,
            ns,
            name,
            f"Atlas uses {target['name']}.",
            [PROJECT, target],
            [
                {
                    "subject": PROJECT["key"],
                    "relation": "uses_database",
                    "target": target["key"],
                    "slot": "primary",
                    "valid_at": "2026-09-01T00:00:00Z",
                }
            ],
        )
        receipts.append(receipt)
    r = v.QuestionSearch(namespace=ns, question="MySQL database", limit=1)
    result = v.search(store, r)
    assert result["status"] == "conflict"
    assert set(result["conflict_fact_ids"]) == {
        f for receipt in receipts for f in receipt["fact_ids"]
    }
    assert not v.search(
        store, r.model_copy(update={"as_of": datetime.fromisoformat("2026-08-01T00:00:00Z")})
    )["facts"]
    store.retract(ns, receipts[0]["fact_ids"][0], "Wrong database")
    result = v.search(store, r.model_copy(update={"question": "database"}))
    assert result["status"] == "found"
    assert result["facts"][0]["id"] == receipts[1]["fact_ids"][0]


def test_question_search_accepts_its_full_advertised_length():
    from .test_retrieval import fact, raw

    store = SimpleNamespace(recall=lambda *a, **kw: raw(current=[fact("found")]))
    question = "Atlas MySQL " * 80
    result = v.search(store, v.QuestionSearch(namespace="test", question=question))
    assert result["facts"][0]["id"] == "found"
