"""Pure event-time projection. No cached summary can override graph evidence."""

from collections import defaultdict
from datetime import datetime

from .models import EVENT_RELATIONS


def project(facts: list[dict], as_of: datetime) -> dict:
    cutoff = as_of.timestamp()
    groups = defaultdict(list)
    history, uncertain, events, documented = [], [], [], []
    for fact in facts:
        if fact.get("retracted"):
            continue
        if fact.get("confirmed_at") and fact["status"] == "uncertain":
            # A person vouched for it: established from the date they confirmed.
            fact = {
                **fact,
                "status": "active",
                "valid_at": fact["confirmed_valid_at"],
                "valid_ts": fact["confirmed_valid_ts"],
                "confirmed": True,
            }
        if fact.get("valid_ts") is None:
            if fact.get("documented_ts") is not None:
                if fact["documented_ts"] <= cutoff:
                    documented.append(fact)
            else:
                uncertain.append(fact)
            continue
        if fact["valid_ts"] > cutoff:
            continue
        if fact["relation"] in EVENT_RELATIONS:
            if fact["status"] == "active":
                events.append(fact)
            elif fact["status"] == "uncertain":
                uncertain.append(fact)
            else:
                history.append(fact)
            continue
        # Planned changes have their own lane and cannot replace deployed state.
        lane = "planned" if fact["status"] == "planned" else "actual"
        role = fact.get("slot") or "target:" + fact["target"]
        groups[(fact["subject"], fact["relation"], role, lane)].append(fact)
    current, planned, conflicts = [], [], []
    for key, members in groups.items():
        winners = []
        for timestamp in sorted({f["valid_ts"] for f in members}):
            observed = [f for f in members if f["valid_ts"] == timestamp]
            assertions = [f for f in observed if f["status"] != "ended"]
            ended = {f["target"] for f in observed if f["status"] == "ended"}
            if assertions:
                winners = assertions
                # An assertion and a denial at the same instant conflict.
                winners += [
                    f
                    for f in observed
                    if f["status"] == "ended"
                    and any(a["target"] == f["target"] for a in assertions)
                ]
            else:
                winners = [f for f in winners if f["target"] not in ended]
        winning_ids = {f["id"] for f in winners}
        history.extend(f for f in members if f["id"] not in winning_ids)
        if not winners:
            continue
        # Equal-time incompatible values are unresolved, never arbitrary last-write wins.
        if len({(f["target"], f["status"]) for f in winners}) > 1:
            conflicts.extend(winners)
            continue
        winner = sorted(winners, key=lambda f: f["id"])[0]
        winner = {**winner, "corroborating_fact_ids": [f["id"] for f in winners]}
        if winner["status"] == "ended":
            history.extend(winners)
        elif winner["status"] == "uncertain":
            uncertain.extend(winners)
        elif key[-1] == "planned":
            planned.append(winner)
        else:
            current.append(winner)
    # Fulfilled/ended plans no longer appear as pending, without deleting history.
    actual = {}
    for f in facts:
        if (
            f.get("valid_ts") is not None
            and f["valid_ts"] <= cutoff
            and f["status"] in ("active", "ended")
            and not f.get("retracted")
        ):
            key = (f["subject"], f["relation"], f["target"])
            actual[key] = max(actual.get(key, float("-inf")), f["valid_ts"])
    planned = [
        f
        for f in planned
        if actual.get((f["subject"], f["relation"], f["target"]), float("-inf")) < f["valid_ts"]
    ]

    def order(f):
        return (-(f.get("valid_ts") or 0), f["id"])

    return {
        "current": sorted(current, key=order),
        "planned": sorted(planned, key=order),
        "events": sorted(events, key=order),
        "uncertain": sorted(uncertain, key=order),
        "documented": sorted(documented, key=lambda f: (-f["documented_ts"], f["id"])),
        "conflicts": sorted(conflicts, key=order),
        "history": sorted(history, key=order),
    }
