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
    data = raw(
        current=[fact("answer", summary="Database is MySQL.")],
        uncertain=[fact("noise", summary="Release banner changed.", target="banner", slot=None)],
    )
    compact = retrieve(data, question="database", limit=1)
    full = retrieve(data, question="database", detail="full", limit=1)
    assert [f["id"] for f in full["facts"]] == [f["id"] for f in compact["facts"]] == ["answer"]
    assert full["facts"][0]["summary"] == "Database is MySQL."
    assert full["counts"] == compact["counts"]
    assert retrieve(data, detail="full") == data


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
    old = fact(
        "old",
        summary="Deployment is waiting.",
        reported_at="2026-09-01T00:00:00Z",
        recorded_at="2026-09-22T00:00:00Z",
        **common,
    )
    new = fact(
        "new",
        summary="Deployment completed; validation is 16%.",
        reported_at="2026-09-02T00:00:00Z",
        recorded_at="2026-09-03T00:00:00Z",
        **common,
    )
    out = retrieve(raw(uncertain=[old, new]), question="deployment")
    assert [f["id"] for f in out["facts"]] == ["new", "old"]
    assert out["facts"][0]["at"] is None
    assert out["facts"][0]["reported_at"] == new["reported_at"]
    assert retrieve(raw(uncertain=[old, new]), question="validation")["facts"][0]["id"] == "new"


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
                "progress",
                relation="occurred",
                slot=None,
                target="event:vocabulary-evaluation",
                summary="Extraction vocabulary evaluation results are still pending.",
            ),
            fact(
                "outcome",
                relation="occurred",
                slot=None,
                target="event:vocabulary-rollout",
                summary="Validation rate rose from 14% to 16%; deployment completed.",
            ),
        ]
    )
    out = retrieve(data, question="What were the vocabulary evaluation results?", limit=2)
    assert "outcome" in [f["id"] for f in out["facts"]]


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
