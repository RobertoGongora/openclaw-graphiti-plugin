from graph_memory.models import Entity, Extraction, Latest

from .helpers import PROJECT, ingest


def test_latest_project_resolution_ignores_newer_unrelated_state(graph):
    store, ns = graph
    issue = {"key": "issue:cache", "name": "Cache invalidation", "kind": "issue"}
    decision = {"key": "decision:workers", "name": "Use workers", "kind": "decision"}
    ingest(
        store,
        ns,
        "resolved",
        "Atlas resolved cache invalidation.",
        [PROJECT, issue],
        [
            {
                "subject": PROJECT["key"],
                "target": issue["key"],
                "relation": "resolved",
                "valid_at": "2026-09-14T10:00:00Z",
            }
        ],
    )
    ingest(
        store,
        ns,
        "decision",
        "Atlas decided to use workers.",
        [PROJECT, decision],
        [
            {
                "subject": PROJECT["key"],
                "target": decision["key"],
                "relation": "decided",
                "valid_at": "2026-09-15T10:00:00Z",
            }
        ],
    )
    assert store.latest(ns, "Atlas")["latest"]["relation"] == "decided"
    assert store.latest(ns, "Atlas", relation="resolved")["latest"]["target"] == issue["key"]
    assert Latest(namespace=ns, entity="Atlas", relation="decided").entity == "Atlas"
    assert Latest.model_validate({"namespace": ns, "habit": "Pushups"}).relation == "occurred"


def test_occurrences_apply_to_services_and_coincident_facts_are_not_arbitrarily_selected(graph):
    store, ns = graph
    service = {"key": "service:api", "name": "API", "kind": "service"}
    for name in ("restart", "deploy"):
        event = {"key": f"event:{name}", "name": name, "kind": "event"}
        ingest(
            store,
            ns,
            name,
            f"API {name} occurred.",
            [service, event],
            [
                {
                    "subject": service["key"],
                    "target": event["key"],
                    "relation": "occurred",
                    "valid_at": "2026-09-15T10:00:00Z",
                }
            ],
        )
    result = store.latest(ns, "API", relation="occurred")
    assert result["status"] == "multiple"
    assert result["latest"] is None
    assert result["latest_count"] == 2
    assert {f["target_name"] for f in result["latest_facts"]} == {"restart", "deploy"}


def test_preferences_preserve_scope_and_provide_existing_role_context(graph):
    store, ns = graph
    person = {"key": "person:morgan", "name": "Morgan", "kind": "person"}
    english = {"key": "language:en", "name": "English", "kind": "language"}
    spanish = {"key": "language:es", "name": "Spanish", "kind": "language"}
    base = {"subject": person["key"], "relation": "prefers"}
    ingest(
        store,
        ns,
        "new",
        "Morgan prefers Spanish in chat, English in reports.",
        [person, english, spanish],
        [
            {**base, "target": spanish["key"], "slot": "chat", "valid_at": "2026-09-15T10:00:00Z"},
            {
                **base,
                "target": english["key"],
                "slot": "reports",
                "valid_at": "2026-09-15T10:00:00Z",
            },
        ],
    )
    ingest(
        store,
        ns,
        "old",
        "Morgan prefers English in chat.",
        [person, english],
        [
            {**base, "target": english["key"], "slot": "chat", "valid_at": "2026-09-12T10:00:00Z"},
        ],
    )
    current = store.recall(ns, "Morgan")["current"]
    assert {(f["slot"], f["target_name"]) for f in current} == {
        ("chat", "Spanish"),
        ("reports", "English"),
    }
    context = store.relationship_context(ns, [person])
    assert {f["slot"] for f in context} == {"chat", "reports"}
    # The ontology is generic: no habit type is necessary for project/service/person memory.
    assert Entity(**person).kind == "person"
    assert Extraction(entities=[], facts=[]).facts == []
