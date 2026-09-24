import pytest

from graph_memory.journal import Journal

from .helpers import MYSQL, PG, PROJECT, ingest


@pytest.mark.integration
def test_explicit_compatible_alternative_correction_preserves_evidence_and_history(graph):
    store, ns = graph
    receipt, _, _ = ingest(
        store,
        ns,
        "choice",
        "Atlas can use MySQL or Postgres.",
        [PROJECT, MYSQL, PG],
        [
            {
                "subject": PROJECT["key"],
                "relation": "uses_database",
                "target": target["key"],
                "slot": "acceptable",
                "status": "planned",
                "valid_at": "2026-09-01T00:00:00Z",
            }
            for target in (MYSQL, PG)
        ],
    )
    journal = Journal(store)
    before = journal.snapshot(ns)
    assert len(store.recall(ns, "Atlas")["conflicts"]) == 2
    ids = receipt["fact_ids"]
    store.allow_alternatives(ns, ids, "Original user statement allows either database")
    current = store.recall(ns, "Atlas")
    assert not current["conflicts"] and len(current["planned"]) == 2
    after = journal.snapshot(ns)
    for fid in ids:
        original = before["state"]["MemoryFact"][fid]
        corrected = after["state"]["MemoryFact"][fid]
        assert corrected["evidence"] == original["evidence"]
        assert corrected["summary"] == original["summary"]
        assert corrected["original_slot"] == "acceptable"
        assert not corrected.get("slot")
    assert len(store.recall(ns, "Atlas", at_change=before["sequence"])["conflicts"]) == 2
    assert store.allow_alternatives(ns, ids, "retry")["replayed"]
    with pytest.raises(ValueError, match="namespace"):
        store.allow_alternatives("other", ids, "not permitted")
    with pytest.raises(ValueError, match="distinct"):
        store.allow_alternatives(ns, [ids[0], ids[0]], "duplicate")
    assert journal.verify_live(ns)["verified"]


@pytest.mark.integration
def test_partial_correction_cannot_resurrect_ended_role_or_separate_duplicate_reports(graph):
    store, ns = graph
    facts = [
        {
            "subject": PROJECT["key"],
            "relation": "uses_database",
            "target": target["key"],
            "slot": "choice",
            "valid_at": "2026-09-01T00:00:00Z",
        }
        for target in (MYSQL, PG)
    ]
    first, _, _ = ingest(
        store, ns, "first", "MySQL or Postgres are acceptable.", [PROJECT, MYSQL, PG], facts
    )
    duplicate, _, _ = ingest(
        store, ns, "duplicate", "MySQL or Postgres are acceptable.", [PROJECT, MYSQL, PG], facts
    )
    ended, _, _ = ingest(
        store,
        ns,
        "ended",
        "Both database options have been discontinued.",
        [PROJECT, MYSQL, PG],
        [{**f, "status": "ended", "valid_at": "2026-09-02T00:00:00Z"} for f in facts],
    )
    assert not store.recall(ns, "Atlas")["current"]
    with pytest.raises(ValueError, match="complete exclusive-role history"):
        store.allow_alternatives(ns, first["fact_ids"], "These are alternatives")
    assert not store.recall(ns, "Atlas")["current"]
    store.allow_alternatives(
        ns,
        first["fact_ids"] + duplicate["fact_ids"] + ended["fact_ids"],
        "Reviewed full history: compatible choices, later both ended",
    )
    assert not store.recall(ns, "Atlas")["current"]
