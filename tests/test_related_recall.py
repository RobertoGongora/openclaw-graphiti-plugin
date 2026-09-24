"""Related decisions must fit inside ordinary recall budgets and temporal scope."""

import pytest

from graph_memory import related
from graph_memory import retrieval as v
from graph_memory.journal import Journal

from .helpers import ingest

VM = {"key": "service:cedar", "name": "Cedar", "kind": "service"}
PLAN = {"key": "event:cedar-vm-plan", "name": "Cedar VM plan", "kind": "event"}
DECISION = {"key": "decision:maintenance", "name": "Maintenance decision", "kind": "decision"}
TOPIC = {"key": "topic:cedar-vm-recreation", "name": "Cedar VM recreation", "kind": "topic"}
QUESTION = "Should we rebuild Cedar now, or did we defer rebuilding it?"


def seed(store, ns):
    plan, _, _ = ingest(
        store,
        ns,
        "plan",
        "Cedar's VM can be recreated after exporting the named volumes.",
        [VM, PLAN],
        [
            {
                "subject": VM["key"],
                "relation": "occurred",
                "target": PLAN["key"],
                "status": "uncertain",
            }
        ],
    )
    before = Journal(store).snapshot(ns)["sequence"]
    decision, _, _ = ingest(
        store,
        ns,
        "defer",
        "We decided not to recreate the Cedar VM for now; keep the option noted.",
        [DECISION, TOPIC],
        [
            {
                "subject": DECISION["key"],
                "relation": "about",
                "target": TOPIC["key"],
                "status": "planned",
                "valid_at": "2026-09-14T10:00:00Z",
            }
        ],
    )
    return plan["fact_ids"][0], decision["fact_ids"][0], before


def test_related_decision_is_in_budget_with_original_source_and_subject(graph):
    store, ns = graph
    plan, decision, _ = seed(store, ns)
    request = v.RecallView(namespace=ns, entity=VM["key"], question=QUESTION, limit=2)
    result = v.recall(store, request)
    assert {f["id"] for f in result["facts"]} == {plan, decision}
    assert result["entities"] == [VM]
    selected = next(f for f in result["facts"] if f["id"] == decision)
    assert selected["subject"] == DECISION["key"]
    assert selected["lane"] == "planned"
    assert selected["source"]
    assert not result.get("derived")
    full = v.recall(store, request.model_copy(update={"detail": "full"}))
    assert [f["id"] for f in full["facts"]] == [f["id"] for f in result["facts"]]
    first = v.recall(store, request.model_copy(update={"limit": 1}))
    second = v.recall(
        store, request.model_copy(update={"limit": 1, "offset": first["next_offset"]})
    )
    assert [first["facts"][0]["id"], second["facts"][0]["id"]] == [f["id"] for f in result["facts"]]
    assert not v.recall(store, request.model_copy(update={"namespace": ns + ":other"}))["facts"]
    # Unquestioned recall remains scoped to the original node.
    assert [
        f["id"] for f in v.recall(store, request.model_copy(update={"question": None}))["facts"]
    ] == [plan]


def test_related_decision_historical_read_does_not_learn_future_deferral(graph):
    store, ns = graph
    plan, decision, before = seed(store, ns)
    request = v.RecallView(namespace=ns, entity=VM["key"], question=QUESTION)
    early = v.recall(store, request.model_copy(update={"at_change": before}))
    assert [f["id"] for f in early["facts"]] == [plan]
    latest = Journal(store).snapshot(ns)["sequence"]
    historical = v.recall(store, request.model_copy(update={"at_change": latest}))
    live = v.recall(store, request)
    assert historical["facts"] == live["facts"]
    assert decision in {f["id"] for f in historical["facts"]}


def test_expansion_ignores_alias_collisions_and_partial_names_and_is_bounded():
    root = {**VM, "id": "root"}
    unrelated = {
        "id": "wrong",
        "key": "topic:other",
        "name": "Other",
        "kind": "topic",
        "aliases": ["Cedar"],
    }
    partial = {**unrelated, "id": "partial", "name": "Cedars VM"}
    candidates = [
        {"id": str(i), "key": f"topic:cedar-{i:02}", "name": f"Cedar topic {i}", "kind": "topic"}
        for i in range(30)
    ]
    chosen = related.select([root], [unrelated, partial, *candidates], QUESTION)
    assert len(chosen) == related.LIMIT
    assert not {"wrong", "partial"} & {e["id"] for e in chosen}
    assert related.select([root], list(reversed(candidates)), QUESTION) == chosen
    assert not related.select([root, {**root, "id": "ambiguous"}], candidates, QUESTION)
    assert not related.select(
        [{**root, "name": "Backup", "key": "service:backup"}], candidates, QUESTION
    )
    assert not related.select([root], candidates, "Cedar")
    assert not related.select([root], candidates, "How much RAM does Cedar use?")
    # Joining distinct canonical fields must not manufacture a phrase match.
    multiword = {**root, "name": "Cedar Server", "key": "service:vm"}
    split_name = {**unrelated, "name": "Cedar", "key": "topic:server-maintenance"}
    assert not related.select([multiword], [split_name], "What did we decide for Cedar Server?")


def test_topic_links_only_expand_decisions_not_every_person_or_project():
    seeds = [
        {
            "subject_id": "person",
            "subject_kind": "person",
            "target_id": "decision",
            "target_kind": "decision",
            "relation": "decided",
            "summary": "Defer the rebuild.",
        },
        {
            "subject_id": "unrelated-project",
            "subject_kind": "project",
            "target_id": "topic",
            "target_kind": "topic",
            "relation": "about",
            "summary": "Cedar rebuild plan and every matching word.",
        },
        {
            "subject_id": "direct-decision",
            "subject_kind": "decision",
            "target_id": "topic",
            "target_kind": "topic",
            "relation": "about",
            "summary": "Recreate Cedar later.",
        },
    ]
    assert set(related.subjects(seeds, QUESTION)) == {"decision", "direct-decision"}
    crowded = [{**seeds[2], "subject_id": str(i)} for i in range(100)]
    assert len(related.subjects(crowded, QUESTION)) == related.SUBJECT_LIMIT


def test_related_person_decision_does_not_load_unrelated_personal_facts(graph):
    store, ns = graph
    person = {"key": "person:owner", "name": "Owner", "kind": "person"}
    decision = {"key": "decision:cedar-upgrade", "name": "Cedar upgrade", "kind": "decision"}
    unrelated = {"key": "topic:other-system", "name": "Other system", "kind": "topic"}
    seed(store, ns)
    extra, _, _ = ingest(
        store,
        ns,
        "owner",
        "The owner decided to defer Cedar's upgrade.",
        [person, decision],
        [
            {
                "subject": person["key"],
                "relation": "decided",
                "target": decision["key"],
                "status": "planned",
                "valid_at": "2026-09-14T10:00:00Z",
            }
        ],
    )
    noise, _, _ = ingest(
        store,
        ns,
        "noise",
        "The owner decided to rebuild an unrelated system.",
        [person, unrelated],
        [{"subject": person["key"], "relation": "about", "target": unrelated["key"]}],
    )
    r = v.RecallView(namespace=ns, entity=VM["key"], question=QUESTION)
    live = v.recall(store, r)
    assert extra["fact_ids"][0] in {f["id"] for f in live["facts"]}
    assert noise["fact_ids"][0] not in {f["id"] for f in live["facts"]}
    snapshot = Journal(store).snapshot(ns)
    assert (
        v.recall(store, r.model_copy(update={"at_change": snapshot["sequence"]}))["facts"]
        == live["facts"]
    )


@pytest.mark.parametrize(
    ("root_name", "decision_name"),
    [
        ("Straße", "Straße upgrade"),
        ("Cedar Server", "Cedar  Server upgrade"),
        ("Cedar Server", "Cedar__Server upgrade"),
    ],
)
def test_canonical_unicode_names_match_in_live_and_historical_expansion(
    graph, root_name, decision_name
):
    store, ns = graph
    service = {"key": "service:street", "name": root_name, "kind": "service"}
    topic = {"key": "topic:maintenance", "name": "Maintenance", "kind": "topic"}
    decision = {"key": "decision:move", "name": decision_name, "kind": "decision"}
    person = {"key": "person:owner", "name": "Owner", "kind": "person"}
    ingest(
        store,
        ns,
        "root",
        f"{root_name} maintenance is planned.",
        [service, topic],
        [{"subject": service["key"], "relation": "about", "target": topic["key"]}],
    )
    chosen, _, _ = ingest(
        store,
        ns,
        "choice",
        f"We decided to defer the {decision_name}.",
        [person, decision],
        [
            {
                "subject": person["key"],
                "relation": "decided",
                "target": decision["key"],
                "status": "planned",
                "valid_at": "2026-09-14T10:00:00Z",
            }
        ],
    )
    r = v.RecallView(
        namespace=ns,
        entity=service["key"],
        question=f"Should we proceed with the {root_name} upgrade?",
    )
    live = v.recall(store, r)
    assert chosen["fact_ids"][0] in {f["id"] for f in live["facts"]}
    sequence = Journal(store).snapshot(ns)["sequence"]
    assert v.recall(store, r.model_copy(update={"at_change": sequence}))["facts"] == live["facts"]


def test_related_decision_keeps_replacement_and_conflicting_role_sides(graph):
    store, ns = graph
    seed(store, ns)
    decision = {"key": "decision:cedar-policy", "name": "Cedar policy", "kind": "decision"}
    people = [
        {"key": f"person:{name.lower()}", "name": name, "kind": "person"}
        for name in ["Ada", "Bea", "Cal"]
    ]
    receipts = []
    for i, person in enumerate(people):
        result, _, _ = ingest(
            store,
            ns,
            f"owner-{i}",
            f"{person['name']} owns the Cedar policy.",
            [decision, person],
            [
                {
                    "subject": decision["key"],
                    "relation": "owned_by",
                    "target": person["key"],
                    "slot": "owner",
                    "valid_at": "2026-09-01T00:00:00Z" if i == 0 else "2026-09-02T00:00:00Z",
                }
            ],
        )
        receipts.append(result["fact_ids"][0])
    request = v.RecallView(
        namespace=ns,
        entity=VM["key"],
        question="What did we decide about Ada owning the Cedar policy?",
    )
    live = v.recall(store, request)
    assert live["status"] == "conflict"
    ids = {f["id"] for f in live["facts"]}
    assert set(receipts[1:]) <= ids
    assert receipts[0] not in ids
    assert set(receipts[1:]) <= set(live["conflict_fact_ids"])
    sequence = Journal(store).snapshot(ns)["sequence"]
    assert (
        v.recall(store, request.model_copy(update={"at_change": sequence}))["facts"]
        == live["facts"]
    )
