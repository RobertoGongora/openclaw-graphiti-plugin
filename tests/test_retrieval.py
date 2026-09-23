import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from graph_memory import retrieval as v
from graph_memory.journal import Journal
from graph_memory.mcp import Protocol
from graph_memory.service import MemoryService

from .helpers import MYSQL, PG, PROJECT, ingest
from .test_contracts import rpc


def fact(fid, **changes):
    return {
        "id": fid,
        "subject": "project:atlas",
        "relation": "uses_database",
        "target": "database:mysql",
        "slot": "primary",
        "status": "active",
        "valid_at": "2026-09-01T00:00:00Z",
        "valid_ts": 1,
        "summary": "Atlas uses MySQL.",
        "episode_id": "episode",
        **changes,
    }


def raw(**lanes):
    data = {lane: [] for lane in v.LANES}
    data.update(lanes)
    return {
        **data,
        "query": "Atlas",
        "entities": [PROJECT],
        "ambiguous": False,
        "entity_matches_truncated": False,
        "inferred": [],
        "insights": [],
        "totals": {k: len(data[k]) for k in v.LANES},
        "revision": 12,
        "as_of": "2026-09-17T00:00:00Z",
        "freshness": {"pending_episodes": 8},
    }


def retrieve(data, **params):
    return v.recall(
        SimpleNamespace(recall=lambda *a, **k: deepcopy(data)),
        v.RecallView(namespace="test", query="Atlas", **params),
    )


def test_compact_dedup_bounds_and_page_without_mutating_raw():
    data = raw(
        documented=[fact(str(i), valid_at=None) for i in range(100)],
        current=[fact("current")],
        history=[fact("old")],
    )
    original = deepcopy(data)
    result = retrieve(data, limit=1)
    assert result["facts"][0]["id"] == "current"
    assert result["next_offset"] == 1
    assert result["counts"]["matching_unique"] == 2
    second = retrieve(data, limit=1, offset=1)
    assert second["facts"][0]["support_count"] == 100
    assert second["facts"][0]["lane"] == "documented"
    assert second["next_offset"] is None
    assert result["freshness"]["pending_episodes"] == 8
    assert data == original
    assert retrieve(data, detail="full") == data


def test_question_about_old_value_surfaces_replacement_and_plan():
    data = raw(
        history=[fact("mysql")],
        current=[fact("pg", summary="Postgres is deployed.", target=PG["key"])],
        planned=[
            fact("plan", summary="Postgres upgrade is planned.", target=PG["key"], status="planned")
        ],
    )
    result = retrieve(data, question="Are we still using MySQL?")
    assert {f["id"] for f in result["facts"]} == {"pg", "plan"}
    assert {f["lane"] for f in result["facts"]} == {"current", "planned"}
    assert "mysql" in {f["id"] for f in retrieve(data, include_history=True)["facts"]}
    assert retrieve(data, question="Redis cache?")["status"] == "no_matching_facts"


def test_conflicts_never_hidden_as_single_confirmed_winner():
    data = raw(conflicts=[fact("a"), fact("b", target=PG["key"], summary="Postgres is primary.")])
    result = retrieve(data, question="MySQL?", limit=1)
    assert result["status"] == "conflict"
    assert result["facts"][0]["lane"] == "conflicts"
    assert result["counts"]["stored_by_lane"]["conflicts"] == 2
    assert result["next_offset"] == 1


def test_long_summary_marked_excerpt_and_no_evidence_dump():
    result = retrieve(raw(current=[fact("x", summary="x" * 2000, evidence="secret payload")]))
    assert len(result["facts"][0]["text"]) == 500
    assert result["facts"][0]["text_truncated"]
    assert "evidence" not in result["facts"][0]


def test_derived_keeps_supports_and_does_not_become_fact():
    data = raw()
    data["inferred"] = [
        {
            "subject": "project:atlas",
            "relation": "uses_language",
            "target": "language:php",
            "supporting_fact_ids": ["framework", "language"],
        }
    ]
    out = retrieve(data, question="PHP?")
    assert not out["facts"]
    assert out["derived"][0]["lane"] == "inferred"
    assert out["derived"][0]["supporting_fact_ids"] == ["framework", "language"]


def test_latest_preserves_ties_uncertainty_despite_small_budget():
    data = {
        "status": "uncertain",
        "entity": PROJECT,
        "relation": None,
        "certainty": "unresolved claims may be newer",
        "latest_facts": [fact("a"), fact("b")],
        "latest_count": 2,
        "conflicts": [],
        "unresolved": [fact("unknown", valid_at=None)],
        "unresolved_count": 1,
        "revision": 3,
    }
    store = SimpleNamespace(latest=lambda *a, **k: data)
    out = v.latest(store, v.LatestView(namespace="test", entity="Atlas", limit=1))
    assert out["status"] == "uncertain" and out["more"]
    assert out["counts"]["latest"] == 2 and out["counts"]["unresolved"] == 1
    assert len(out["facts"]) == 1


@pytest.mark.integration
def test_mcp_compact_and_exact_evidence_historical_namespace_retraction(graph):
    store, ns = graph
    receipt, _, _ = ingest(
        store,
        ns,
        "source",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [
            {
                "subject": PROJECT["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "slot": "primary",
                "valid_at": "2026-09-01T00:00:00Z",
            }
        ],
    )
    fid = receipt["fact_ids"][0]
    checkpoint = Journal(store).verify(ns)["sequence"]
    protocol = Protocol(MemoryService(store), ns, read_only=True)

    def call(name, **args):
        out = protocol.dispatch(rpc("tools/call", name=name, arguments={"namespace": ns, **args}))[
            1
        ]["result"]
        assert not out["isError"], out
        return out["structuredContent"]

    result = call("memory_recall", query="Atlas", question="Which database?")
    assert result["facts"][0]["id"] == fid
    assert "evidence" not in result["facts"][0]
    full = call("memory_recall", query="Atlas", detail="full")
    assert full["current"][0]["id"] == fid
    detail = call("memory_evidence", fact_ids=[fid])["facts"][0]
    assert detail["claims"][0]["quote"] == "Atlas uses MySQL."
    assert detail["claims"][0]["role"] == "user"
    assert detail["claims"][0]["message_available"]
    assert detail["validation"] == []
    assert detail["source"]["id"] == receipt["episode_id"]
    assert v.evidence(store, v.EvidenceRequest(namespace="other", fact_ids=[fid]))[
        "missing_fact_ids"
    ] == [fid]
    store.retract(ns, fid, "incorrect")
    assert call("memory_evidence", fact_ids=[fid])["facts"][0]["fact"]["retracted"]
    past = call("memory_evidence", fact_ids=[fid], at_change=checkpoint)
    assert not past["facts"][0]["fact"]["retracted"]
    assert call("memory_recall", query="Atlas")["facts"] == []
    assert call("memory_recall", query="Atlas", at_change=checkpoint)["facts"][0]["id"] == fid
    assert len(json.dumps(result)) < len(json.dumps(full))


def test_specific_question_ranks_strong_match_without_discarding_partial_match():
    data = raw(
        documented=[
            fact(
                "exact",
                summary="Report crossed the statement timeout.",
                target="report",
                slot=None,
                valid_at=None,
            )
        ],
        current=[
            fact("loose", summary="Callback timeout is 15 seconds.", target="callback", slot=None)
        ],
    )
    result = retrieve(data, question="statement timeout", limit=1)
    assert [f["id"] for f in result["facts"]] == ["exact"]
    assert result["next_offset"] == 1
    assert retrieve(data, question="statement timeout", offset=1)["facts"][0]["id"] == "loose"


@pytest.mark.integration
def test_entity_search_alias_partial_scope_and_fixed_history(graph):
    store, ns = graph
    canonical = {**PROJECT, "name": "Atlas Command Center", "aliases": ["Atlas", "AtlasCC"]}
    base = {"target": MYSQL["key"], "relation": "uses_database", "valid_at": "2026-09-01T00:00:00Z"}
    ingest(
        store,
        ns,
        "atlas",
        "Atlas uses MySQL.",
        [canonical, MYSQL],
        [{**base, "subject": canonical["key"]}],
    )
    checkpoint = Journal(store).verify(ns)["sequence"]
    other = {**PROJECT, "key": "project:other-atlas", "name": "Atlas Client"}
    ingest(
        store,
        ns,
        "client",
        "Atlas Client uses MySQL.",
        [other, MYSQL],
        [{**base, "subject": other["key"]}],
    )

    def search(query, **kw):
        return v.search_entities(store, v.EntitySearch(namespace=ns, query=query, **kw))

    assert search("ATLAScc")["matches"][0]["key"] == canonical["key"]
    assert search("command atlas")["matches"][0]["match"] == "words"
    assert search("atla")["total"] == 2
    first, second = search("Atlas", limit=1), search("Atlas", limit=1, offset=1)
    assert first["next_offset"] == 1 and second["next_offset"] is None
    assert first["matches"][0]["key"] != second["matches"][0]["key"]
    assert search("Atlas", at_change=checkpoint)["total"] == 1
    assert search("Atlas", kind="person")["total"] == 0
    assert (
        v.search_entities(store, v.EntitySearch(namespace="unrelated", query="Atlas"))["total"] == 0
    )
    assert search("postgres")["matches"] == []
    protocol = Protocol(MemoryService(store), ns, read_only=True)
    message = rpc(
        "tools/call",
        name="memory_recall",
        arguments={"entity": canonical["key"], "question": "database"},
    )
    before = deepcopy(message)
    output = protocol.dispatch(message)[1]["result"]
    assert not output["isError"] and output["structuredContent"]["facts"]
    assert message == before
    for name, args in [
        ("memory_search_entities", {"query": "Atlas"}),
        ("memory_evidence", {"fact_ids": ["missing"]}),
    ]:
        bad = rpc("tools/call", name=name, arguments={"namespace": "unrelated", **args})
        assert protocol.dispatch(bad)[0] == 403


@pytest.mark.integration
def test_bound_remember_injects_nested_namespace_without_mutating_input(graph):
    store, ns = graph
    protocol = Protocol(MemoryService(store), ns)
    arguments = {
        "transcript": {
            "session_id": "chat",
            "source_id": "remember-scoped",
            "messages": [{"id": "m1", "role": "user", "content": "Remember this preference."}],
        }
    }
    message = rpc("tools/call", name="memory_ingest", arguments=arguments)
    before = deepcopy(message)
    result = protocol.dispatch(message)[1]["result"]
    assert not result["isError"], result
    assert result["structuredContent"]["namespace"] == ns
    assert message == before
    arguments["transcript"]["namespace"] = "wrong"
    assert protocol.dispatch(message)[0] == 403


def test_uncertain_different_wordings_survive_and_identical_reports_are_not_support():
    said = [
        fact(
            f"u{i}",
            status="uncertain",
            valid_at=None,
            valid_ts=None,
            summary=f"Atlas reportedly uses MySQL, wording {i}.",
            session_id=f"session-{i % 2}",
            recorded_at=f"2026-09-1{i}T00:00:00Z",
        )
        for i in range(4)
    ]
    dated = [fact(f"d{i}", summary=f"Atlas uses MySQL, wording {i}.") for i in range(2)]
    result = retrieve(raw(uncertain=said, current=dated), limit=10)
    uncertain = [f for f in result["facts"] if f["lane"] == "uncertain"]
    assert len(uncertain) == 4
    # Dated claims keep their exact-wording grouping: different words, different entries.
    assert len([f for f in result["facts"] if f["lane"] == "current"]) == 2
    assert result["counts"]["matching_unique"] == 6
    duplicates = retrieve(
        raw(uncertain=[said[0], {**said[0], "id": "copy", "session_id": "other"}])
    )
    assert len(duplicates["facts"]) == 1
    assert duplicates["facts"][0]["report_count"] == 2
    assert duplicates["facts"][0]["source_sessions"] == 2
    assert "support_count" not in duplicates["facts"][0]


def test_question_full_expands_same_ranked_page_and_retains_raw_view_without_question():
    backup = dict(summary="Database backup is nightly.", target="backup", slot=None)
    data = raw(
        current=[fact("answer", summary="Database is MySQL.")],
        uncertain=[
            fact("noise", summary="Release banner changed.", target="banner", slot=None),
            fact("twice", session_id="s1", **backup),
            fact("copy", session_id="s2", **backup),
        ],
        history=[fact("old", summary="Database was Postgres.", target=PG["key"])],
    )
    calls = []

    def recall_all(*args, **kwargs):
        calls.append(kwargs)
        return deepcopy(data)

    store = SimpleNamespace(recall=recall_all)
    pages = [{}, {"offset": 1}, {"include_history": True}, {"include_history": True, "offset": 2}]
    for params in [*pages, {"offset": 50}]:
        view = dict(namespace="test", entity="Atlas", question="database", limit=1, **params)
        compact = v.recall(store, v.RecallView(**view))
        full = v.recall(store, v.RecallView(**view, detail="full"))
        assert [f["id"] for f in full["facts"]] == [f["id"] for f in compact["facts"]]
        assert full["counts"] == compact["counts"]
        assert full["next_offset"] == compact["next_offset"]
        assert full["question_terms"] == compact["question_terms"] == ["database"]
    # Ranking needs every lane complete; a per-lane cut before ranking could hide the answer.
    assert all(call["_complete"] for call in calls)
    compact = retrieve(data, question="database", limit=1)
    full = retrieve(data, question="database", detail="full", limit=1)
    assert [f["id"] for f in full["facts"]] == [f["id"] for f in compact["facts"]] == ["answer"]
    assert full["facts"][0]["summary"] == "Database is MySQL."
    grouped = next(f for f in retrieve(data, question="backup", detail="full")["facts"])
    assert grouped["summary"] == backup["summary"] and grouped["lane"] == "uncertain"
    assert grouped["report_count"] == 2 and grouped["source_sessions"] == 2
    assert retrieve(data, detail="full") == data
    v.recall(store, v.RecallView(namespace="test", entity="Atlas", detail="full"))
    assert calls[-1]["_complete"] is False


@pytest.mark.integration
def test_recall_word_normalization_does_not_change_entity_name_search(graph):
    store, ns = graph
    named = {
        **PROJECT,
        "key": "project:eval-tracker",
        "name": "Eval Tracker",
        "aliases": ["Eval Tracker"],
    }
    ingest(
        store,
        ns,
        "tracker",
        "Eval Tracker uses MySQL.",
        [named, MYSQL],
        [
            {
                "subject": named["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "valid_at": "2026-09-01T00:00:00Z",
            }
        ],
    )
    assert (
        v.search_entities(store, v.EntitySearch(namespace=ns, query="Tracker Eval"))["matches"][0][
            "key"
        ]
        == named["key"]
    )


def test_report_order_uses_source_time_not_reingestion_and_preserves_detail():
    common = {"status": "uncertain", "valid_at": None, "valid_ts": None, "slot": None}
    # IDs sort against the expected order, so ID order cannot stand in for report time.
    old = fact(
        "a-old",
        summary="Deployment is waiting.",
        reported_at="2026-09-01T00:00:00Z",
        recorded_at="2026-09-22T00:00:00Z",
        **common,
    )
    new = fact(
        "z-new",
        summary="Deployment completed; validation is 16%.",
        reported_at="2026-09-02T00:00:00Z",
        recorded_at="2026-09-03T00:00:00Z",
        **common,
    )
    out = retrieve(raw(uncertain=[old, new]), question="deployment")
    assert [f["id"] for f in out["facts"]] == ["z-new", "a-old"]
    assert out["facts"][0]["at"] is None
    assert out["facts"][0]["reported_at"] == new["reported_at"]
    assert retrieve(raw(uncertain=[old, new]), question="validation")["facts"][0]["id"] == "z-new"


def test_event_role_does_not_transfer_question_score_to_unrelated_summary():
    data = raw(
        uncertain=[
            fact(
                "relevant",
                relation="occurred",
                target="event:release",
                slot="status",
                summary="Vocabulary validation is 16%.",
            ),
            fact(
                "unrelated",
                relation="occurred",
                target="event:release",
                slot="status",
                summary="Release paused while CI runs.",
            ),
        ]
    )
    assert [f["id"] for f in retrieve(data, question="validation")["facts"]] == ["relevant"]


def test_result_question_retains_measured_outcome_alongside_progress_reports():
    data = raw(
        uncertain=[
            fact(
                "a-progress",
                relation="occurred",
                slot=None,
                target="event:vocabulary-evaluation",
                summary="Extraction vocabulary evaluation results are still pending.",
            ),
            fact(
                "z-outcome",
                relation="occurred",
                slot=None,
                target="event:vocabulary-rollout",
                summary="Validation rate rose from 14% to 16%; deployment completed.",
            ),
        ]
    )
    out = retrieve(data, question="What were the vocabulary evaluation results?", limit=2)
    # The measured outcome outranks the progress note; ID order would say otherwise.
    assert [f["id"] for f in out["facts"]] == ["z-outcome", "a-progress"]


@pytest.mark.integration
def test_report_time_live_and_historical_does_not_become_event_time(graph):
    from graph_memory.models import Transcript
    from tests.test_session_sources import extraction

    store, ns = graph
    t = Transcript(
        namespace=ns,
        source_id="report-time",
        session_id="report-time",
        source_format="session-records-v1",
        messages=[
            {
                "id": "report",
                "role": "assistant",
                "source_type": "assistant_report",
                "content": "Atlas uses MySQL.",
                "timestamp": "2026-09-22T12:00:00Z",
            }
        ],
    )
    receipt = store.stage(t)
    store.commit(ns, receipt["episode_id"], extraction("report", status="uncertain"))
    checkpoint = Journal(store).verify(ns)["sequence"]
    live = v.recall(store, v.RecallView(namespace=ns, entity="Atlas"))
    historical = v.recall(store, v.RecallView(namespace=ns, entity="Atlas", at_change=checkpoint))
    assert live["facts"] == historical["facts"]
    assert live["facts"][0]["reported_at"] == "2026-09-22T12:00:00+00:00"
    assert live["facts"][0]["at"] is None


def test_question_terms_expose_unranked_stop_word_questions_and_normalized_forms():
    data = raw(
        current=[fact("db", summary="Database is MySQL.")],
        uncertain=[
            fact(
                "deploy",
                relation="occurred",
                target="event:deploy",
                slot=None,
                summary="Deployed to production yesterday.",
            )
        ],
    )
    unranked = retrieve(data, question="What is the current status?")
    assert unranked["question_terms"] == []
    assert unranked["status"] == "found"
    assert [f["id"] for f in unranked["facts"]] == [f["id"] for f in retrieve(data)["facts"]]
    assert "question_terms" not in retrieve(data)
    ranked = retrieve(data, question="When was the deployment?")
    assert ranked["question_terms"] == ["deploy"]
    assert [f["id"] for f in ranked["facts"]] == ["deploy"]
    study = raw(
        uncertain=[
            fact(
                "ev",
                relation="occurred",
                target="event:study",
                slot=None,
                summary="Vocabulary evaluation finished.",
            )
        ]
    )
    assert retrieve(study, question="eval results?")["question_terms"] == ["evaluation", "result"]
    assert [f["id"] for f in retrieve(study, question="evals")["facts"]] == ["ev"]
    # The entity's own name ranks nothing: every fact of the entity would match it.
    assert retrieve(data, question="Atlas")["question_terms"] == []
    measured = raw(
        uncertain=[
            fact(
                "rate",
                relation="occurred",
                target="event:rollout",
                slot=None,
                summary="Validation rate is 16%.",
            )
        ]
    )
    # A measured validation figure answers an evaluation question without the word.
    assert [f["id"] for f in retrieve(measured, question="evaluation")["facts"]] == ["rate"]


def test_dated_records_precede_undated_and_report_time_orders_only_undated():
    common = dict(relation="occurred", slot=None, status="uncertain")
    dated = fact(
        "dated",
        target="event:release",
        summary="Release shipped.",
        valid_at="2025-01-01T00:00:00Z",
        valid_ts=1735689600.0,
        reported_at="2026-09-22T00:00:00Z",
        **common,
    )
    newer = fact(
        "a-report",
        target="event:migration",
        summary="Migration may be blocked.",
        valid_at=None,
        valid_ts=None,
        reported_at="2026-09-10T00:00:00Z",
        **common,
    )
    older = fact(
        "z-report",
        target="event:backup",
        summary="Backup may be late.",
        valid_at=None,
        valid_ts=None,
        reported_at="2026-09-01T00:00:00Z",
        **common,
    )
    out = retrieve(raw(uncertain=[older, newer, dated]), limit=10)
    # An old event reported yesterday is not newer than an undated report from last week.
    assert [f["id"] for f in out["facts"]] == ["dated", "a-report", "z-report"]


def test_identical_legacy_copies_show_newest_ingestion_when_no_report_time():
    shared = dict(
        relation="occurred",
        target="event:deploy",
        slot=None,
        status="uncertain",
        valid_at=None,
        valid_ts=None,
        summary="Deploy pending.",
    )
    copies = [
        fact("a-old", recorded_at="2026-09-01T00:00:00Z", episode_id="ep-old", **shared),
        fact("z-new", recorded_at="2026-09-20T00:00:00Z", episode_id="ep-new", **shared),
    ]
    out = retrieve(raw(uncertain=copies))
    assert [f["id"] for f in out["facts"]] == ["z-new"]
    assert out["facts"][0]["source"] == "ep-new"
    assert out["facts"][0]["report_count"] == 2
    # A report time, when present, still wins over ingestion time.
    reported = [{**copies[0], "reported_at": "2026-09-21T00:00:00Z"}, copies[1]]
    assert retrieve(raw(uncertain=reported))["facts"][0]["id"] == "a-old"


@pytest.mark.integration
def test_legacy_fact_report_time_matches_between_live_and_historical(graph, monkeypatch):
    from graph_memory import store as store_module
    from graph_memory.models import Evidence, Transcript
    from tests.test_session_sources import extraction

    store, ns = graph
    t = Transcript(
        namespace=ns,
        source_id="legacy",
        session_id="legacy",
        source_format="session-records-v1",
        messages=[
            {
                "id": "early",
                "role": "assistant",
                "source_type": "assistant_report",
                "content": "Atlas uses MySQL.",
                "timestamp": "2026-09-22T08:00:00.250+00:00",
            },
            {
                "id": "late",
                "role": "assistant",
                "source_type": "assistant_report",
                "content": "Atlas uses MySQL.",
                "timestamp": "2026-09-22T12:00:00+03:00",
            },
        ],
    )
    receipt = store.stage(t)
    candidate = extraction("early", status="uncertain")
    candidate.facts[0].evidence.append(Evidence(message_id="late", quote="Atlas uses MySQL."))
    with monkeypatch.context() as previous_engine:
        # A fact committed before reported_at existed stored no such property.
        previous_engine.setattr(store_module, "latest_report_time", lambda stamps: None)
        store.commit(ns, receipt["episode_id"], candidate)
    stored = store.read(
        lambda tx: tx.run(
            "MATCH (f:MemoryFact {namespace:$ns}) RETURN f.reported_at AS reported", ns=ns
        ).single()["reported"]
    )
    assert stored is None
    checkpoint = Journal(store).verify(ns)["sequence"]
    live = v.recall(store, v.RecallView(namespace=ns, entity="Atlas"))
    historical = v.recall(store, v.RecallView(namespace=ns, entity="Atlas", at_change=checkpoint))
    assert live["facts"] == historical["facts"]
    assert live["facts"][0]["reported_at"] == "2026-09-22T09:00:00+00:00"
    assert live["facts"][0]["at"] is None
    full = v.recall(store, v.RecallView(namespace=ns, entity="Atlas", detail="full"))
    assert full["uncertain"][0]["reported_at"] == "2026-09-22T09:00:00+00:00"


def test_weak_conflict_match_is_reported_without_claiming_the_answer_is_disputed():
    data = raw(
        conflicts=[
            fact("c1", summary="Atlas uses MySQL for release sessions."),
            fact("c2", target=PG["key"], summary="Atlas uses Postgres."),
        ],
        events=[
            fact(
                "ev",
                relation="occurred",
                target="event:release",
                slot=None,
                summary="Release 2.3 shipped to production after QA signoff.",
            )
        ],
    )
    out = retrieve(data, question="When did release 2.3 ship to production?", limit=1)
    # Page one is the release event; the database dispute only shares the word release.
    assert [f["id"] for f in out["facts"]] == ["ev"]
    assert out["status"] == "found"
    assert out["counts"]["conflicts_matching"] == 2
    assert set(out["conflict_fact_ids"]) == {"c1", "c2"}
    assert out["conflict_fact_ids_truncated"] is False
    # A question aimed at the disagreement itself still reports it, first on the page.
    direct = retrieve(data, question="Which database, MySQL or Postgres?", limit=1)
    assert direct["status"] == "conflict"
    assert direct["facts"][0]["lane"] == "conflicts"
    # Without a question every record ties, so any disagreement is the answer.
    assert retrieve(data)["status"] == "conflict"
    assert "conflict_fact_ids" not in retrieve(raw(current=[fact("x")]))


def test_conflict_ids_include_the_unmatched_side_and_stay_within_one_evidence_call():
    shared = dict(relation="about", slot="owner", status="active")
    data = raw(
        conflicts=[
            fact("topic-a", target="topic:billing", summary="Billing owns invoices.", **shared),
            fact("topic-b", target="topic:finance", summary="Finance owns it.", **shared),
        ],
        current=[fact("db", summary="Atlas uses MySQL.")],
    )
    out = retrieve(data, question="invoices")
    # Only one side matches the question; the other side comes along to be checked.
    assert out["conflict_fact_ids"] == ["topic-a", "topic-b"]
    many = raw(conflicts=[fact(f"c{i:02}", target=f"database:{i}") for i in range(12)])
    capped = retrieve(many, limit=1)
    assert len(capped["conflict_fact_ids"]) == 10
    assert capped["conflict_fact_ids_truncated"] is True
    # Identical conflicting copies are separate records: the count matches the IDs.
    copies = raw(conflicts=[fact("x1"), {**fact("x1"), "id": "x2"}, fact("y", target=PG["key"])])
    grouped = retrieve(copies, question="database")
    assert grouped["counts"]["conflicts_matching"] == 3
    assert grouped["conflict_fact_ids"] == ["x1", "x2", "y"]
