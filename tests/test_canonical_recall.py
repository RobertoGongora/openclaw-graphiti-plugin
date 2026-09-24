import pytest

from graph_memory.journal import Journal

from .helpers import ingest


@pytest.mark.integration
def test_canonical_key_wins_over_aliases_even_beyond_candidate_page(graph):
    store, ns = graph
    person = {"key": "person:rob", "name": "Rob", "kind": "person"}
    events = [
        {"key": f"event:a{i:02}", "name": f"Event {i}", "kind": "event", "aliases": ["person:rob"]}
        for i in range(25)
    ]
    ingest(
        store,
        ns,
        "aliases",
        "Rob's events were recorded.",
        [person, *events],
        [
            {
                "subject": person["key"],
                "relation": "occurred",
                "target": e["key"],
                "valid_at": "2026-09-01T00:00:00Z",
            }
            for e in events
        ],
    )
    sequence = Journal(store).snapshot(ns)["sequence"]
    for scope in ({}, {"at_change": sequence}):
        result = store.recall(ns, "person:rob", **scope)
        assert not result["ambiguous"]
        assert [e["key"] for e in result["entities"]] == ["person:rob"]


@pytest.mark.integration
def test_merged_person_key_alias_does_not_resolve_to_event_alias(graph):
    store, ns = graph
    person = {"key": "person:rob", "name": "Rob", "kind": "person"}
    user = {"key": "person:user", "name": "User", "kind": "person"}
    event = {
        "key": "event:aliases",
        "name": "Identity aliases requested",
        "kind": "event",
        "aliases": ["person:user"],
    }
    ingest(
        store,
        ns,
        "alias-event",
        "The user requested identity aliases.",
        [person, user, event],
        [
            {
                "subject": user["key"],
                "relation": "occurred",
                "target": event["key"],
                "valid_at": "2026-09-01T00:00:00Z",
            }
        ],
    )
    store.merge(ns, "person:user", "person:rob", "User confirmed equivalence")
    sequence = Journal(store).snapshot(ns)["sequence"]
    for scope in ({}, {"at_change": sequence}):
        result = store.recall(ns, "person:user", **scope)
        assert not result["ambiguous"]
        assert [e["key"] for e in result["entities"]] == ["person:rob"]
