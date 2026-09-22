from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from graph_memory import retrieval as v
from graph_memory.models import Extraction, Fact
from graph_memory.temporal import project, role, shared_slots

from .test_retrieval import fact, raw


def claim(relation, target, slot=None):
    return {
        "subject": "project:atlas",
        "relation": relation,
        "target": target,
        "slot": slot,
        "summary": "x",
        "valid_at": "2026-09-01T00:00:00Z",
        "evidence": [{"message_id": "m1", "quote": "x"}],
    }


def test_slot_survives_only_on_role_bearing_relations():
    assert (
        Fact.model_validate(claim("uses_database", "database:mysql", "primary")).slot == "primary"
    )
    for relation, target in (
        ("related_to", "issue:x"),
        ("has_issue", "issue:x"),
        ("about", "topic:x"),
        ("occurred", "event:x"),
    ):
        assert Fact.model_validate(claim(relation, target, "invented-label")).slot is None
    with pytest.raises(ValidationError):
        Fact.model_validate(claim("no_such_relation", "issue:x", "label"))


def test_new_relations_are_kind_constrained():
    def extraction(relation, kind):
        return {
            "entities": [
                {"key": "project:atlas", "name": "Atlas", "kind": "project"},
                {"key": f"{kind}:x", "name": "X", "kind": kind},
            ],
            "facts": [claim(relation, f"{kind}:x")],
        }

    for relation, kind in (("has_issue", "issue"), ("about", "topic"), ("depends_on", "service")):
        Extraction.model_validate(extraction(relation, kind))
        with pytest.raises(ValidationError, match="Invalid target kind"):
            Extraction.model_validate(extraction(relation, "event"))


def test_single_use_slot_groups_by_target_and_shared_slot_by_role():
    mysql = fact("m", target="database:mysql", slot="mysql-observation-1", valid_ts=1)
    mysql_again = fact("m2", target="database:mysql", slot="mysql-observation-2", valid_ts=2)
    postgres = fact("p", target="database:postgres", slot="pg-observation", valid_ts=3)
    assert shared_slots([mysql, mysql_again, postgres]) == set()
    assert role(mysql) == ("project:atlas", "uses_database", "target:database:mysql")
    out = project([mysql, mysql_again, postgres], SimpleNamespace(timestamp=lambda: 10))
    assert [f["id"] for f in out["current"]] == ["p", "m2"]  # newer observation wins
    assert [f["id"] for f in out["history"]] == ["m"]
    shared = [
        fact("old", target="database:mysql", slot="primary", valid_ts=1),
        fact("new", target="database:postgres", slot="primary", valid_ts=2),
    ]
    assert shared_slots(shared) == {("project:atlas", "uses_database", "primary")}
    out = project(shared, SimpleNamespace(timestamp=lambda: 10))
    assert [f["id"] for f in out["current"]] == ["new"] and [f["id"] for f in out["history"]] == [
        "old"
    ]


def test_recall_question_treats_single_use_slots_as_targets():
    old = fact("old", slot="db-note-1", valid_ts=1, summary="Atlas used MySQL.")
    new = fact(
        "new",
        target="database:postgres",
        slot="db-note-2",
        valid_ts=2,
        summary="Atlas uses Postgres.",
    )
    # Different targets, single-use slots: different roles, so a question about MySQL
    # does not drag Postgres in through a shared role.
    result = v.recall(
        SimpleNamespace(recall=lambda *a, **k: raw(current=[new], history=[old])),
        v.RecallView(namespace="test", entity="Atlas", question="MySQL", include_history=True),
    )
    assert [f["id"] for f in result["facts"]] == ["old"]
    # The same two facts on a shared role: the replacement comes along.
    old["slot"] = new["slot"] = "primary"
    result = v.recall(
        SimpleNamespace(recall=lambda *a, **k: raw(current=[new], history=[old])),
        v.RecallView(namespace="test", entity="Atlas", question="MySQL", include_history=True),
    )
    assert {f["id"] for f in result["facts"]} == {"old", "new"}
