from datetime import datetime

import pytest

from graph_memory.journal import Journal

from .test_reference_journal import mixed_chain, query

pytestmark = pytest.mark.integration


def test_selected_snapshots_match_full_across_legacy_and_reference_checkpoints(graph, monkeypatch):
    store, ns = graph
    seen, _, _ = mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    for sequence in seen:
        full = journal.snapshot(ns, sequence=sequence)
        for cutoff in (
            {"sequence": sequence},
            {"known_at": datetime.fromisoformat(full["known_at"])},
        ):
            selected = journal.snapshot(
                ns, **cutoff, select=lambda label, node: label == "MemoryEntity"
            )
            assert selected["state"]["MemoryEntity"] == full["state"]["MemoryEntity"]
            assert all(
                not nodes for label, nodes in selected["state"].items() if label != "MemoryEntity"
            )
            assert {k: v for k, v in selected.items() if k != "state"} == {
                k: v for k, v in full.items() if k != "state"
            }


def test_omitted_checkpoint_data_and_changed_nodes_are_still_verified(graph, monkeypatch):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    original = journal._events

    def corrupt(*args):
        for event, tip in original(*args):
            if event["changes"]:
                event["changes"][0]["hash"] = "0" * 64
            yield event, tip

    monkeypatch.setattr(journal, "_events", corrupt)
    with pytest.raises(ValueError, match="integrity"):
        journal.snapshot(ns, select=lambda label, node: False)
    monkeypatch.setattr(journal, "_events", original)
    query(
        store,
        "MATCH (p:MemorySnapshotPart {namespace:$ns}) SET p.data=$bad",
        ns="audit:" + ns,
        bad=b"bad",
    )
    # Parts are journaled in the audit namespace; if schema changes, corrupt by
    # their relationship as well so this assertion cannot pass vacuously.
    query(
        store, "MATCH (:MemoryChange {scope:$ns})-[:PART]->(p) SET p.data=$bad", ns=ns, bad=b"bad"
    )
    with pytest.raises(ValueError, match="checkpoint integrity"):
        journal.snapshot(ns, select=lambda label, node: False)


def test_restore_does_not_retain_unchanged_unselected_nodes(graph, monkeypatch):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    journal.checkpoint(ns)
    seen = []
    original = journal._restore

    def inspect(*args, **kwargs):
        state, sealed, total = original(*args, **kwargs)
        seen.append((sum(map(len, state.values())), len(sealed)))
        return state, sealed, total

    monkeypatch.setattr(journal, "_restore", inspect)
    journal.snapshot(ns, select=lambda label, node: label == "MemoryEntity")
    assert seen[-1] == (3, 0)
    journal.snapshot(ns)
    assert seen[-1][0] > 3 and seen[-1][1] > 0
