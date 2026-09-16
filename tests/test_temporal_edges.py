from datetime import datetime

import pytest

from graph_memory.temporal import project

AT = datetime.fromisoformat("2026-09-16T00:00:00+00:00")


def fact(id, target, time, status="active", slot="primary"):
    return {
        "id": id,
        "subject": "atlas",
        "relation": "uses_database",
        "target": target,
        "valid_ts": time,
        "status": status,
        "slot": slot,
    }


@pytest.mark.parametrize(
    "facts,expected",
    [
        (
            [fact("1", "mysql", 10), fact("2", "postgres", 20), fact("3", "mysql", 30, "ended")],
            ["postgres"],
        ),
        (
            [fact("1", "mysql", 10), fact("2", "postgres", 20), fact("3", "postgres", 30, "ended")],
            [],
        ),
        (
            [fact("1", "mysql", 10, slot=None), fact("2", "postgres", 20, slot=None)],
            ["postgres", "mysql"],
        ),
        ([fact("1", "mysql", 10), fact("2", "postgres", 20, "uncertain")], []),
        ([fact("1", "mysql", 10), fact("2", "postgres", 20, "planned")], ["mysql"]),
        (
            [fact("1", "mysql", 10), fact("2", "postgres", AT.timestamp() + 100, "planned")],
            ["mysql"],
        ),
    ],
)
def test_state_transitions(facts, expected):
    for order in (facts, list(reversed(facts))):
        assert [f["target"] for f in project(order, AT)["current"]] == expected


def test_simultaneous_denial_is_a_conflict():
    result = project([fact("1", "mysql", 10), fact("2", "mysql", 10, "ended")], AT)
    assert len(result["conflicts"]) == 2
    assert result["current"] == []


def test_duplicate_supports_are_retained():
    result = project([fact("1", "mysql", 10), fact("2", "mysql", 10)], AT)
    assert len(result["current"]) == 1
    assert set(result["current"][0]["corroborating_fact_ids"]) == {"1", "2"}
