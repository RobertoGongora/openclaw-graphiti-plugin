import json
import uuid
from datetime import UTC, datetime

import pytest
from neo4j import ManagedTransaction

from graph_memory import journal
from graph_memory.journal import Journal
from graph_memory.models import (
    DreamCreate,
    DreamOutput,
    DreamRequest,
    Evidence,
    Extraction,
    Transcript,
)
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
    # Unchanged claims are counted and named, never stored again with their evidence.
    assert diff["unchanged"] == receipt["fact_ids"] and diff["unchanged_count"] == 1
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
    assert diff["unchanged_count"] == 2 and not diff["added"] and not diff["removed"]
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


def dreaming(extraction):
    """A model that extracts `extraction` and reflects on the first current fact."""

    def answer(payload):
        if "eligible_fact_ids" not in payload:
            return extraction
        return DreamOutput(
            insights=[
                {
                    "summary": "Atlas runs MySQL.",
                    "entity_keys": [PROJECT["key"]],
                    "supporting_fact_ids": [payload["graph"]["current"][0]["id"]],
                    "confidence": 0.9,
                }
            ],
            observations=[],
        )

    return answer


def applied_dream(service, ns, episode_id):
    created = service.dream_create(
        DreamCreate(namespace=ns, query="Atlas", episode_ids=[episode_id])
    )
    request = DreamRequest(namespace=ns, dream_id=created["dream_id"])
    service.dream_run(request)
    service.dream_apply(request)
    return created["dream_id"]


def dream(store, ns, dream_id):
    return store.read(
        lambda tx: tx.run(
            "MATCH (d:MemoryDream {id:$id,namespace:$ns}) RETURN properties(d) AS d",
            id=dream_id,
            ns=ns,
        ).single()
    )["d"]


def test_promotion_runs_the_validated_checks_again(graph):
    store, ns = graph
    receipt, _, original = ingest(
        store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL, slot="primary")]
    )
    revisions = Revisions(MemoryService(store, Model(lambda payload: original)))
    rid = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    revisions.build(ns, rid)
    checks = [
        {
            "tool": "memory_recall",
            "arguments": {"query": "Atlas"},
            "path": "conflicts",
            "equals": [],
        }
    ]
    assert revisions.validate(ns, rid, report(), checks)["passed"]
    assert revisions.get(ns, rid)["checks"] == checks
    # Another episode now claims a different holder of the same slot at the same time.
    ingest(store, ns, "other", "Atlas uses PostgreSQL.", [PROJECT, PG], [fact(PG, slot="primary")])
    assert store.recall(ns, "Atlas")["conflicts"]
    with pytest.raises(ValueError, match="no longer passes"):
        revisions.promote(ns, rid)
    assert revisions.get(ns, rid)["status"] == "validated"


def test_a_dream_that_appears_after_the_build_blocks_promotion_until_rebuilt(graph):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    service = MemoryService(store, Model(dreaming(original)))
    revisions = Revisions(service)
    created = revisions.create(ns, [receipt["episode_id"]])
    rid = created["revision_id"]
    assert created["affected_dreams"] == []
    revisions.build(ns, rid)
    assert revisions.validate(ns, rid, report(), CHECKS)["passed"]
    old = applied_dream(service, ns, receipt["episode_id"])
    with pytest.raises(ValueError, match="build the revision again"):
        revisions.promote(ns, rid)
    assert "superseded_by" not in dream(store, ns, old)
    diff = revisions.build(ns, rid)["diff"]
    assert diff["affected_dreams"] == [old]
    rebuilt = revisions.get(ns, rid)
    assert rebuilt["status"] == "built" and "validation" not in rebuilt
    assert revisions.validate(ns, rid, report(), CHECKS)["passed"]
    assert revisions.promote(ns, rid, diff["digest"])["revalidated_dreams"] == 1
    assert dream(store, ns, old)["superseded_by"] == diff["revalidated_dreams"][old]
    assert Journal(store).verify(ns)["verified"]


def test_two_revisions_never_replace_the_same_dream_twice(graph):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    service = MemoryService(store, Model(dreaming(original)))
    old = applied_dream(service, ns, receipt["episode_id"])
    revisions = Revisions(service)
    first = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    second = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    first_diff = revisions.build(ns, first)["diff"]
    assert revisions.build(ns, second)["diff"]["affected_dreams"] == [old]
    assert revisions.validate(ns, first, report(), CHECKS)["passed"]
    assert revisions.validate(ns, second, report(), CHECKS)["passed"]
    revisions.promote(ns, first, first_diff["digest"])
    replacement = first_diff["revalidated_dreams"][old]
    with pytest.raises(ValueError, match="build the revision again"):
        revisions.promote(ns, second, revisions.get(ns, second)["diff"]["digest"])
    # Rebuilt, the second revision replaces the first one's replacement, not the original.
    second_diff = revisions.build(ns, second)["diff"]
    assert second_diff["affected_dreams"] == [replacement]
    assert revisions.validate(ns, second, report(), CHECKS)["passed"]
    revisions.promote(ns, second, second_diff["digest"])
    assert dream(store, ns, old)["superseded_by"] == replacement
    assert (
        dream(store, ns, replacement)["superseded_by"]
        == (second_diff["revalidated_dreams"][replacement])
    )
    live = store.read(
        lambda tx: tx.run(
            "MATCH (i:MemoryInsight {namespace:$ns}) WHERE coalesce(i.retired,false)=false "
            "RETURN count(i) AS count",
            ns=ns,
        ).single()
    )["count"]
    assert live == 1
    assert Journal(store).verify(ns)["verified"]


def test_every_step_refuses_another_engine_or_model(graph, monkeypatch):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    model = Model(lambda payload: original)
    revisions = Revisions(MemoryService(store, model))
    rid = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    revisions.build(ns, rid)
    steps = (
        lambda: revisions.diff(ns, rid),
        lambda: revisions.validate(ns, rid, report(), CHECKS),
        lambda: revisions.promote(ns, rid),
    )
    for step in steps[:2]:
        model.model = "another"
        with pytest.raises(ValueError, match="Model settings changed"):
            step()
        model.model = "test"
    assert revisions.validate(ns, rid, report(), CHECKS)["passed"]
    model.effort = "low"
    with pytest.raises(ValueError, match="Model settings changed"):
        revisions.promote(ns, rid)
    model.effort = "xhigh"
    monkeypatch.setattr("graph_memory.revisions.engine_fingerprint", lambda: "another engine")
    for step in steps:
        with pytest.raises(ValueError, match="Engine changed"):
            step()
    assert revisions.get(ns, rid)["status"] == "validated"
    monkeypatch.undo()
    assert revisions.promote(ns, rid)["status"] == "promoted"
    # What is already promoted stays readable under any engine.
    monkeypatch.setattr("graph_memory.revisions.engine_fingerprint", lambda: "another engine")
    assert revisions.promote(ns, rid)["replayed"]
    assert revisions.diff(ns, rid)["diff"]["unchanged_count"] == 1


def test_checks_may_only_use_tools_that_read_the_preview(graph):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    revisions = Revisions(MemoryService(store, Model(lambda payload: original)))
    rid = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    revisions.build(ns, rid)
    for tool, arguments in (
        ("memory_render", {}),
        ("memory_retract", {"fact_id": receipt["fact_ids"][0], "reason": "no"}),
    ):
        check = {"tool": tool, "arguments": arguments, "path": "nodes", "equals": []}
        with pytest.raises(ValueError, match=f"{tool} cannot be used in a revision check"):
            revisions.validate(ns, rid, report(), [*CHECKS, check])
    assert revisions.get(ns, rid)["status"] == "built"
    assert facts(store, ns)[receipt["fact_ids"][0]]["retracted"] is False


def test_a_confirmation_lands_only_on_an_uncertain_claim_and_losers_are_listed(graph):
    store, ns = graph
    twice = [
        fact(MYSQL, status="uncertain"),
        fact(MYSQL, status="uncertain", confidence=0.9),
        fact(PG, status="uncertain"),
    ]
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], twice)
    older, newer, pg = receipt["fact_ids"]
    store.confirm(ns, older, "First look", datetime(2026, 9, 15, tzinfo=UTC))
    store.confirm(ns, newer, "Second look", datetime(2026, 9, 15, tzinfo=UTC))
    store.confirm(ns, pg, "Roadmap", datetime(2026, 9, 15, tzinfo=UTC))

    def answer(payload):
        # One reworded MySQL claim for two confirmed ones; PostgreSQL comes back as
        # established, which a person's confirmation of a doubt says nothing about.
        result = original.model_copy(deep=True)
        result.facts = [
            result.facts[0].model_copy(update={"confidence": 0.8}),
            result.facts[2].model_copy(update={"status": "active"}),
        ]
        return result

    revisions = Revisions(MemoryService(store, Model(answer)))
    rid = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    diff = revisions.build(ns, rid)["diff"]
    dropped = {d["fact_id"]: d for d in diff["dropped_decisions"]}
    assert set(dropped) == {older, pg}
    assert dropped[older]["detail"]["confirmation_note"] == "First look"
    assert "another confirmation" in dropped[older]["reason"]
    assert "not uncertain" in dropped[pg]["reason"]
    assert revisions.validate(ns, rid, report(), CHECKS)["passed"]
    revisions.promote(ns, rid, diff["digest"])
    after = {f["id"]: f for f in facts(store, ns).values() if not f["retracted"]}
    assert len(after) == 2
    for live in after.values():
        if live["target"] == MYSQL["key"]:
            assert live["confirmation_carried_from"] == newer
            assert live["confirmation_note"] == "Second look"
        else:
            assert live["status"] == "active" and "confirmed_at" not in live
    assert Journal(store).verify(ns)["verified"]


def test_a_revived_fact_is_reported_with_whatever_it_brings_back(graph):
    store, ns = graph
    uncertain = [fact(MYSQL, status="uncertain"), fact(PG, status="uncertain")]
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], uncertain)
    mysql, pg = receipt["fact_ids"]
    store.confirm(ns, mysql, "Checked", datetime(2026, 9, 15, tzinfo=UTC))
    store.retract(ns, pg, "Never planned")
    without = original.model_copy(deep=True)
    del without.facts[0]
    answers = [without, original]
    revisions = Revisions(MemoryService(store, Model(lambda payload: answers.pop(0))))
    _, diff = promoted(revisions, ns, [receipt["episode_id"]])
    # The person's retraction is undone; the confirmed claim is superseded.
    assert [(r["fact_id"], r["retracted_by"]) for r in diff["revived"]] == [(pg, "person")]
    assert diff["revived"][0]["retraction_reason"] == "Never planned"
    _, diff = promoted(revisions, ns, [receipt["episode_id"]])
    # An earlier revision's retraction is undone, and the confirmation returns with it.
    assert [(r["fact_id"], r["retracted_by"]) for r in diff["revived"]] == [(mysql, "revision")]
    assert diff["revived"][0]["confirmation"]["confirmation_note"] == "Checked"
    assert facts(store, ns)[mysql]["confirmation_note"] == "Checked"
    assert Journal(store).verify(ns)["verified"]


def test_a_failed_dream_is_recorded_and_a_hopeless_one_ends_the_revision(graph, monkeypatch):
    store, ns = graph
    receipt, _, original = ingest(store, ns, "source", TEXT, [PROJECT, MYSQL, PG], [fact(MYSQL)])
    reflect, broken = dreaming(original), [False]

    def answer(payload):
        if broken[0] and "eligible_fact_ids" in payload:
            raise RuntimeError("model fell over")
        return reflect(payload)

    service = MemoryService(store, Model(answer))
    old = applied_dream(service, ns, receipt["episode_id"])
    revisions = Revisions(service)
    rid = revisions.create(ns, [receipt["episode_id"]])["revision_id"]
    broken[0] = True
    with pytest.raises(ValueError, match=f"dream {old}: RuntimeError"):
        revisions.build(ns, rid)
    failed = revisions.get(ns, rid)
    assert failed["status"] == "building"
    assert failed["build_error"] == {
        "dream_id": old,
        "error": "RuntimeError",
        "message": "model fell over",
        "permanent": False,
    }
    broken[0] = False
    recall = store.recall.__func__

    def too_broad(self, *args, **kwargs):
        return {**recall(self, *args, **kwargs), "entity_matches_truncated": True}

    monkeypatch.setattr("graph_memory.revisions._Pinned.recall", too_broad)
    for _ in range(2):
        with pytest.raises(ValueError, match="too broad.*create a new revision"):
            revisions.build(ns, rid)
    monkeypatch.undo()
    hopeless = revisions.get(ns, rid)
    assert hopeless["status"] == "failed" and hopeless["build_error"]["permanent"]
    with pytest.raises(ValueError, match="too broad.*create a new revision"):
        revisions.build(ns, rid)


def test_a_resumed_build_keeps_the_candidates_it_already_has(graph):
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

    revisions = Revisions(MemoryService(store, Model(answer)))
    rid = revisions.create(ns, [first["episode_id"], second["episode_id"]])["revision_id"]
    with pytest.raises(ValueError, match="RuntimeError"):
        revisions.build(ns, rid)
    moved = store.recall(ns, "Atlas")["revision"]
    ingest(store, ns, "later", "Atlas uses PostgreSQL.", [PROJECT, PG], [fact(PG)])
    broken[0] = False
    revisions.build(ns, rid)
    built = revisions.get(ns, rid)["candidates"]
    assert built[first["episode_id"]]["live_revision"] == moved
    assert built[second["episode_id"]]["live_revision"] == moved + 1
