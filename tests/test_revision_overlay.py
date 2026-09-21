import json
import uuid
from datetime import UTC, datetime

import pytest
from neo4j import ManagedTransaction

from graph_memory import journal
from graph_memory.journal import Journal
from graph_memory.models import Evidence, Extraction, Transcript
from graph_memory.revisions import Revisions
from graph_memory.service import MemoryService

from .helpers import MYSQL, PG, PROJECT, ingest
from .test_revisions import report

pytestmark = pytest.mark.integration

SQLITE = {"key": "database:sqlite", "name": "SQLite", "kind": "database"}
TEXT = "Atlas uses MySQL. PostgreSQL is planned. SQLite is for tests."
CHECKS = [
    {
        "tool": "memory_recall",
        "arguments": {"query": "Atlas"},
        "path": "entities",
        "contains": {"key": PROJECT["key"]},
    }
]


def fact(target, **props):
    return {
        "subject": PROJECT["key"],
        "relation": "uses_database",
        "target": target["key"],
        "valid_at": "2026-09-14T09:00:00Z",
        **props,
    }


class Model:
    """Answers with whatever `answer` makes of the payload; counts its calls."""

    model, effort = "test", "xhigh"

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def generate(self, instructions, payload, output):
        self.calls += 1
        return self.answer(payload)


def with_planned(original):
    result = original.model_copy(deep=True)
    result.facts.append(
        result.facts[0].model_copy(update={"target": PG["key"], "status": "planned"})
    )
    return result


def live_nodes(store, ns):
    return store.read(
        lambda tx: tx.run(
            "MATCH (n) WHERE n.namespace=$ns AND NOT n:MemoryRevision "
            "AND NOT n:MemoryRevisionCandidate AND NOT n:MemoryRevisionDream "
            "OPTIONAL MATCH (n)-[r]-() "
            "WITH count(DISTINCT n) AS nodes,count(DISTINCT r) AS edges "
            "MATCH (s:MemorySpace) RETURN nodes,edges,count(s) AS spaces",
            ns=ns,
        ).single()
    ).data()


def facts(store, ns):
    return {
        r["f"]["id"]: r["f"]
        for r in store.read(
            lambda tx: tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) RETURN properties(f) AS f", ns=ns
            ).data()
        )
    }


def promoted(revisions, ns, episode_ids):
    rid = revisions.create(ns, episode_ids, reason="test")["revision_id"]
    diff = revisions.build(ns, rid)["diff"]
    assert revisions.validate(ns, rid, report(), CHECKS)["passed"]
    assert revisions.promote(ns, rid, diff["digest"])["status"] == "promoted"
    return rid, diff


def test_build_writes_nothing_live_and_promotion_is_one_scoped_change(graph):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    ingest(store, ns, "other", "Atlas uses SQLite.", [PROJECT, SQLITE], [fact(SQLITE)])
    model = Model(lambda payload: with_planned(original))
    revisions = Revisions(MemoryService(store, model))
    created = revisions.create(ns, [receipt["episode_id"]], reason="planned facts")
    rid = created["revision_id"]
    assert created["candidate"] is None and created["status"] == "created"
    before, sequence = live_nodes(store, ns), Journal(store).snapshot(ns)["sequence"]
    diff = revisions.build(ns, rid)["diff"]
    assert live_nodes(store, ns) == before
    assert Journal(store).snapshot(ns)["sequence"] == sequence
    assert model.calls == 1
    assert [f["target"] for f in diff["added"]] == [PG["key"]]
    assert [f["target"] for f in diff["unchanged"]] == [MYSQL["key"]]
    assert diff["removed"] == diff["changed"] == diff["dropped_decisions"] == []
    stored = revisions.get(ns, rid)
    assert stored["status"] == "built" and stored["reason"] == "planned facts"
    assert stored["built_episodes"] == [receipt["episode_id"]] and not stored["legacy"]
    assert revisions.diff(ns, rid)["diff"]["digest"] == diff["digest"]
    checks = [
        {
            "tool": "memory_recall",
            "arguments": {"query": "Atlas"},
            "path": "planned",
            "contains": {"relation": "uses_database", "target_contains": "postgre"},
        }
    ]
    assert revisions.validate(ns, rid, report(), checks)["passed"]
    # The checks saw the promoted graph; the live graph still has not moved.
    assert live_nodes(store, ns) == before
    assert store.recall(ns, "Atlas")["planned"] == []
    revision = store.recall(ns, "Atlas")["revision"]
    assert revisions.promote(ns, rid, diff["digest"])["status"] == "promoted"
    assert Journal(store).snapshot(ns)["sequence"] == sequence + 1
    assert Journal(store).events(ns)[-1]["kind"] == "revision_promoted"
    after = store.recall(ns, "Atlas")
    assert after["revision"] == revision + 1
    assert after["planned"][0]["target"] == PG["key"]
    assert {f["target"] for f in after["current"]} == {MYSQL["key"], SQLITE["key"]}
    assert store.recall(ns, "Atlas", at_change=sequence)["planned"] == []
    assert Journal(store).verify(ns)["verified"]
    assert Journal(store).verify_live(ns)["verified"]
    assert revisions.promote(ns, rid)["replayed"]
    assert revisions.diff(ns, rid)["diff"]["digest"] == diff["digest"]


def test_promotion_never_captures_the_whole_namespace(graph, monkeypatch):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    ingest(store, ns, "other", "Atlas uses SQLite.", [PROJECT, SQLITE], [fact(SQLITE)])
    monkeypatch.delenv("MEMORY_JOURNAL_AUDIT")
    captures, real = [], journal.elements

    def elements(tx, namespace, ids=None):
        captures.append(ids)
        return real(tx, namespace, ids)

    monkeypatch.setattr(journal, "elements", elements)
    revisions = Revisions(MemoryService(store, Model(lambda payload: with_planned(original))))
    promoted(revisions, ns, [receipt["episode_id"]])
    assert captures and all(ids is not None for ids in captures)
    touched = {k for ids in captures for label in ids for k in ids[label]}
    other = [f["id"] for f in facts(store, ns).values() if f["target"] == SQLITE["key"]]
    assert other and not touched & set(other)
    monkeypatch.undo()
    assert Journal(store).verify(ns)["verified"]
    assert Journal(store).verify_live(ns)["verified"]


def test_cost_does_not_follow_the_size_of_the_namespace(graph, monkeypatch):
    store, small = graph
    large = "eval:" + str(uuid.uuid4())

    def measure(ns, unrelated):
        receipt, _, original = ingest(
            store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)]
        )
        for i in range(unrelated):
            thing = {"key": f"project:unrelated-{i}", "name": f"Unrelated {i}", "kind": "project"}
            base = {"key": f"database:store-{i}", "name": f"Store {i}", "kind": "database"}
            ingest(
                store,
                ns,
                f"unrelated-{i}",
                f"Unrelated {i} uses Store {i}.",
                [thing, base],
                [{**fact(base), "subject": thing["key"]}],
            )
        model, statements = Model(lambda payload: with_planned(original)), []
        run = ManagedTransaction.run

        def counted(self, query, *args, **kwargs):
            statements.append(query)
            return run(self, query, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(ManagedTransaction, "run", counted)
            promoted(Revisions(MemoryService(store, model)), ns, [receipt["episode_id"]])
        return model.calls, len(statements)

    try:
        assert measure(small, 2) == measure(large, 20)
    finally:
        store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR (n:MemoryChange AND n.scope=$ns) "
                "OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                ns=large,
            ).consume()
        )


def test_confirmations_carry_over_and_dropped_decisions_are_listed(graph):
    store, ns = graph
    uncertain = [
        fact(MYSQL, status="uncertain"),
        fact(PG, status="uncertain"),
        fact(SQLITE, status="uncertain"),
    ]
    receipt, _, original = ingest(
        store, ns, "source", TEXT, [PROJECT, MYSQL, PG, SQLITE], uncertain
    )
    mysql, pg, sqlite = receipt["fact_ids"]
    store.confirm(ns, mysql, "Checked the deployment", datetime(2026, 9, 15, tzinfo=UTC))
    store.confirm(ns, pg, "Checked the roadmap", datetime(2026, 9, 15, tzinfo=UTC))
    store.retract(ns, sqlite, "Never used")

    def answer(payload):
        # MySQL comes back reworded (a new id for the same claim), PostgreSQL is
        # gone, and the retracted SQLite claim comes back unchanged.
        result = original.model_copy(deep=True)
        result.facts[0] = result.facts[0].model_copy(update={"confidence": 0.8})
        del result.facts[1]
        return result

    revisions = Revisions(MemoryService(store, Model(answer)))
    rid = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    diff = revisions.build(ns, rid)["diff"]
    assert [c["before"]["id"] for c in diff["changed"]] == [mysql]
    assert [f["id"] for f in diff["removed"]] == [pg]
    assert [f["id"] for f in diff["added"]] == [sqlite]
    assert {(d["fact_id"], d["decision"]) for d in diff["dropped_decisions"]} == {
        (pg, "confirmed"),
        (sqlite, "retracted"),
    }
    dropped = {d["fact_id"]: d["detail"] for d in diff["dropped_decisions"]}
    assert dropped[pg]["confirmation_note"] == "Checked the roadmap"
    assert dropped[sqlite]["retraction_reason"] == "Never used"
    assert revisions.validate(ns, rid, report(), CHECKS)["passed"]
    with pytest.raises(ValueError, match="Behavior changed"):
        revisions.promote(ns, rid)
    revisions.promote(ns, rid, diff["digest"])
    after = facts(store, ns)
    reworded = after[diff["changed"][0]["after"]["id"]]
    assert reworded["confirmation_note"] == "Checked the deployment"
    assert reworded["confirmed_at"] == after[mysql]["confirmed_at"]
    assert reworded["confirmation_carried_from"] == mysql
    assert after[mysql]["retracted"] and after[mysql]["replaced_by_revision"] == rid
    assert after[pg]["retracted"] and after[pg]["confirmation_note"] == "Checked the roadmap"
    assert Journal(store).verify(ns)["verified"]


def test_a_recommitted_fact_keeps_nothing_of_its_retraction(graph):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    fid = receipt["fact_ids"][0]
    store.retract(ns, fid, "Wrong")
    answers = [original, with_planned(original), original]
    revisions = Revisions(MemoryService(store, Model(lambda payload: answers.pop(0))))
    residue = ("retraction_reason", "retracted_at", "replaced_by_revision")
    # Retracted by a person, then extracted again unchanged.
    promoted(revisions, ns, [receipt["episode_id"]])
    revived = facts(store, ns)[fid]
    assert revived["retracted"] is False and not any(k in revived for k in residue)
    # Superseded by one revision, restored by the next.
    rid, diff = promoted(revisions, ns, [receipt["episode_id"]])
    planned = diff["added"][0]["id"]
    promoted(revisions, ns, [receipt["episode_id"]])
    after = facts(store, ns)
    assert after[planned]["retracted"] and after[planned]["replaced_by_revision"]
    promoted(
        Revisions(MemoryService(store, Model(lambda payload: with_planned(original)))),
        ns,
        [receipt["episode_id"]],
    )
    restored = facts(store, ns)[planned]
    assert restored["retracted"] is False and not any(k in restored for k in residue)
    assert store.recall(ns, "Atlas")["planned"][0]["id"] == planned
    assert Journal(store).verify(ns)["verified"]
    assert Journal(store).verify_live(ns)["verified"]


def test_a_failed_episode_leaves_the_revision_rebuildable(graph):
    store, ns = graph
    first, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    second, _, other = ingest(
        store, ns, "other", "Atlas uses SQLite.", [PROJECT, SQLITE], [fact(SQLITE)]
    )
    broken = [True]

    def answer(payload):
        if "Atlas uses SQLite." not in json.dumps(payload["transcript"]):
            return original
        if broken[0]:
            raise RuntimeError("model fell over")
        return other

    model = Model(answer)
    revisions = Revisions(MemoryService(store, model))
    rid = revisions.create(ns, [first["episode_id"], second["episode_id"]])["revision_id"]
    before = live_nodes(store, ns)
    with pytest.raises(ValueError, match=f"episode {second['episode_id']}: RuntimeError"):
        revisions.build(ns, rid)
    failed = revisions.get(ns, rid)
    assert failed["status"] == "building"
    assert failed["build_error"]["episode_id"] == second["episode_id"]
    assert failed["built_episodes"] == [first["episode_id"]]
    with pytest.raises(ValueError, match="Build the revision"):
        revisions.diff(ns, rid)
    assert live_nodes(store, ns) == before
    broken[0] = False
    diff = revisions.build(ns, rid)["diff"]
    # The episode that had succeeded is not paid for twice.
    assert model.calls == 3
    assert len(diff["unchanged"]) == 2 and not diff["added"] and not diff["removed"]
    rebuilt = revisions.get(ns, rid)
    assert rebuilt["status"] == "built" and "build_error" not in rebuilt
    assert revisions.validate(ns, rid, report(), CHECKS)["passed"]
    # Nothing changed, so no reviewed digest is needed.
    assert revisions.promote(ns, rid)["status"] == "promoted"
    assert Journal(store).verify(ns)["verified"]


def test_a_legacy_revision_can_be_read_but_not_run(graph):
    store, ns = graph
    ingest(store, ns, "source", TEXT, [PROJECT, MYSQL], [fact(MYSQL)])
    rid = str(uuid.uuid4())
    store.transaction(
        lambda tx: tx.run(
            "CREATE (r:MemoryRevision {id:$id,namespace:$ns,candidate:$candidate,status:'built',"
            "base_revision:1,engine:'old',model:'test',episode_ids:['e'],affected_dreams:[],"
            "snapshot:$snapshot,episode_map:$map,diff:$diff})",
            id=rid,
            ns=ns,
            candidate=f"candidate:{rid}",
            snapshot=json.dumps({"MemoryFact": []}),
            map=json.dumps({"e": "c"}),
            diff=json.dumps({"added": [], "removed": [], "digest": "d"}),
        ).consume()
    )
    revisions = Revisions(MemoryService(store, Model(lambda payload: None)))
    legacy = revisions.get(ns, rid)
    assert legacy["legacy"] and "snapshot" not in legacy
    assert legacy["status"] == "built" and legacy["diff"]["digest"] == "d"
    for call in (
        lambda: revisions.build(ns, rid),
        lambda: revisions.diff(ns, rid),
        lambda: revisions.validate(ns, rid, report(), CHECKS),
        lambda: revisions.promote(ns, rid, "d"),
    ):
        with pytest.raises(ValueError, match="predates overlay revisions"):
            call()


def test_promoted_facts_cite_the_live_messages(graph):
    store, ns = graph
    transcript = Transcript(
        namespace=ns,
        source_id="source",
        session_id="session",
        source_format="session-records-v1",
        title="Atlas verification",
        messages=[
            {
                "id": "a",
                "role": "assistant",
                "source_type": "assistant_report",
                "content": "Atlas uses MySQL.",
            },
            {
                "id": "v",
                "role": "tool",
                "source_type": "tool_result",
                "tool_name": "database_status",
                "content": "MySQL online",
            },
        ],
    )
    extraction = Extraction.model_validate(
        {
            "entities": [PROJECT, MYSQL],
            "facts": [
                {
                    **fact(MYSQL, status="uncertain", valid_at=None),
                    "summary": "Atlas uses MySQL.",
                    "evidence": [{"message_id": "a", "quote": "Atlas uses MySQL."}],
                }
            ],
        }
    )
    eid = store.stage(transcript)["episode_id"]
    store.commit(ns, eid, extraction)
    verified = extraction.model_copy(deep=True)
    verified.facts[0] = verified.facts[0].model_copy(
        update={
            "status": "active",
            "valid_at": datetime(2026, 9, 16, 10, tzinfo=UTC),
            "validation_evidence": [Evidence(message_id="v", quote="MySQL online")],
        }
    )
    revisions = Revisions(MemoryService(store, Model(lambda payload: verified)))
    rid, diff = promoted(revisions, ns, [eid])
    rows = store.read(
        lambda tx: tx.run(
            "MATCH (f:MemoryFact {id:$id})-[r:CITES|VALIDATED_BY]->(m:MemoryMessage) "
            "RETURN type(r) AS type,m.namespace AS namespace,m.source_type AS source",
            id=diff["changed"][0]["after"]["id"],
        ).data()
    )
    assert {x["type"]: x["source"] for x in rows} == {
        "CITES": "assistant_report",
        "VALIDATED_BY": "tool_result",
    }
    assert all(x["namespace"] == ns for x in rows)
    assert Journal(store).verify(ns)["verified"]
