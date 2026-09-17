"""Bounded MCP views over the unchanged temporal projection; no model or graph writes."""

import json
import re
from typing import Annotated, Literal

from pydantic import Field

from . import models as m
from .store import normalized

LANES = ("current", "planned", "events", "uncertain", "documented", "conflicts", "history")
STOP = set(
    "a an the what which when where who why how is are was were do does did of for to in on at about me my our it its and or with has have tell please currently now we you i us use uses using used show give know".split()
)


class RecallView(m.Recall):
    query: m.Text = Field(description="Entity name or key, such as Atlas.")
    question: m.Text | None = Field(
        default=None,
        description="Optional question about this entity, used to rank relevant facts.",
    )
    detail: Literal["compact", "full"] = "compact"
    limit: Annotated[int, Field(ge=1, le=100)] = 5
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0
    include_history: bool = False


class LatestView(m.Latest):
    detail: Literal["compact", "full"] = "compact"
    limit: Annotated[int, Field(ge=1, le=20)] = 5


class EvidenceRequest(m.HistoricalScope):
    fact_ids: Annotated[list[m.Key], Field(min_length=1, max_length=10)]


def tokens(text):
    return {w for w in re.findall(r"[^\W_]+", normalized(text)) if w not in STOP}


def role(f):
    return f["subject"], f["relation"], f.get("slot") or "target:" + f["target"]


def compact_fact(f, lane, copies=1):
    result = {
        "id": f["id"],
        "text": f["summary"][:500],
        "lane": lane,
        "at": f.get("valid_at"),
        "source": f["episode_id"],
    }
    if len(f["summary"]) > 500:
        result["text_truncated"] = True
    if f.get("documented_at") and not f.get("valid_at"):
        result["documented_at"] = f["documented_at"]
    count = max(copies, len(f.get("corroborating_fact_ids", [])))
    if count > 1:
        result["support_count"] = count
    return result


def entities(items):
    return [{k: e[k] for k in ("key", "name", "kind")} for e in items]


def metadata(raw):
    return {k: raw[k] for k in ("as_of", "revision", "freshness", "knowledge_history") if k in raw}


def recall(store, r):
    raw = store.recall(
        r.namespace,
        r.query,
        r.as_of,
        r.limit,
        _complete=r.detail == "compact",
        known_at=r.known_at,
        at_change=r.at_change,
    )
    if r.detail == "full":
        return raw
    terms = tokens(r.question or "") - tokens(r.query)
    # Match across all evidence BEFORE hiding history. A question mentioning the old
    # database must also retrieve the replacement in the same exclusive role.
    scores = {}
    for lane in LANES:
        for f in raw[lane]:
            text = " ".join(
                str(f.get(k) or "") for k in ("summary", "subject", "target", "relation")
            )
            scores[role(f)] = max(scores.get(role(f), 0), len(terms & tokens(text)))
    best_score = max(scores.values(), default=0)
    groups = {}
    for lane in LANES:
        if lane == "history" and not r.include_history:
            continue
        for f in raw[lane]:
            key = (
                lane,
                *role(f),
                f["target"],
                f["status"],
                f.get("valid_at"),
                normalized(f["summary"]),
            )
            groups.setdefault(key, []).append(f)
    ranked = []
    for key, copies in groups.items():
        f = copies[0]
        score = scores.get(role(f), 0)
        if terms and (score == 0 or score < best_score):
            continue
        ranked.append((key[0], f, len(copies), score))
    priority = {
        "conflicts": 0,
        "current": 1,
        "planned": 2,
        "events": 3,
        "uncertain": 4,
        "documented": 5,
        "history": 6,
    }
    ranked.sort(key=lambda x: (-x[3], priority[x[0]], -(x[1].get("valid_ts") or 0), x[1]["id"]))
    selected = ranked[r.offset : r.offset + r.limit]
    facts = [compact_fact(f, lane, copies) for lane, f, copies, _ in selected]
    next_offset = r.offset + len(selected)
    # Derived conclusions remain explicitly separate and bounded; they never replace facts.
    derived = []
    for lane in ("inferred", "insights"):
        for item in raw[lane]:
            text = item.get("summary") or f"{item['subject']} {item['relation']} {item['target']}"
            if terms and not terms.intersection(tokens(text)):
                continue
            derived.append(
                {
                    "text": text[:500],
                    "text_truncated": len(text) > 500,
                    "lane": lane,
                    "supporting_fact_ids": item["supporting_fact_ids"],
                }
            )
    room = max(0, r.limit - len(facts))
    return {
        "query": r.query,
        **({"question": r.question} if r.question else {}),
        "status": "ambiguous"
        if raw["ambiguous"]
        else "not_found"
        if not raw["entities"]
        else "conflict"
        if any(x[0] == "conflicts" for x in ranked)
        else "found"
        if ranked or derived
        else "no_matching_facts",
        "entities": entities(raw["entities"]),
        "entity_matches_truncated": raw["entity_matches_truncated"],
        "facts": facts,
        **({"derived": derived[:room]} if derived else {}),
        "counts": {
            "stored_by_lane": raw["totals"],
            "matching_unique": len(ranked),
            "returned": len(facts),
            "derived_returned": min(room, len(derived)),
            "derived_available": len(derived),
        },
        "next_offset": next_offset if next_offset < len(ranked) else None,
        "evidence_tool": "memory_evidence",
        **metadata(raw),
    }


def latest(store, r):
    raw = store.latest(
        r.namespace, r.entity, r.as_of, r.relation, known_at=r.known_at, at_change=r.at_change
    )
    if r.detail == "full":
        return raw
    out = {"status": raw["status"], **metadata(raw)}
    if "candidates" in raw:
        out["candidates"] = entities(raw["candidates"])
        return out
    out.update(
        entity=entities([raw["entity"]])[0], relation=raw["relation"], certainty=raw["certainty"]
    )
    # Resolve ties/uncertainty first; only then bound output. Never imply a single
    # winner because a response budget hid the other candidates.
    rows = [("conflicts", f) for f in raw["conflicts"]]
    rows += [("unresolved", f) for f in raw["unresolved"]]
    rows += [("latest", f) for f in raw["latest_facts"]]
    out["facts"] = [compact_fact(f, lane) for lane, f in rows[: r.limit]]
    out["counts"] = {
        "latest": raw["latest_count"],
        "unresolved": raw["unresolved_count"],
        "conflicts_returned_by_engine": len(raw["conflicts"]),
    }
    out["more"] = raw["latest_count"] + raw["unresolved_count"] + len(raw["conflicts"]) > len(
        out["facts"]
    )
    out["evidence_tool"] = "memory_evidence"
    return out


def evidence(store, r):
    """Fetch exact evidence by namespace-bound IDs, including retracted claims.

    This deliberately reports stored claims, not a new judgment of current truth.
    Historical IDs are resolved against the same journal snapshot as historical recall.
    """
    wanted = list(dict.fromkeys(r.fact_ids))
    history = None
    if r.known_at is not None or r.at_change is not None:
        from .journal import Journal

        snap = Journal(store).snapshot(r.namespace, known_at=r.known_at, sequence=r.at_change)
        state = snap["state"]
        records = [state["MemoryFact"][fid] for fid in wanted if fid in state["MemoryFact"]]
        episodes = state["MemoryEpisode"]
        history = {k: v for k, v in snap.items() if k != "state"}
    else:

        def read(tx):
            rows = tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) WHERE f.id IN $ids "
                "OPTIONAL MATCH (e:MemoryEpisode {namespace:$ns}) WHERE e.id=f.episode_id "
                "RETURN properties(f) AS fact,properties(e) AS episode",
                ns=r.namespace,
                ids=wanted,
            ).data()
            return [x["fact"] for x in rows], {
                x["episode"]["id"]: x["episode"] for x in rows if x["episode"]
            }

        records, episodes = store.transaction(read)
    found = {f["id"]: f for f in records}
    items = []
    for fid in wanted:
        if fid not in found:
            continue
        f = found[fid]
        ep = episodes.get(f["episode_id"])
        source = (
            {k: ep[k] for k in ("id", "session_id", "source_uri", "source_id", "status") if k in ep}
            if ep
            else None
        )
        payload = json.loads(ep.get("payload") or "{}") if ep else {}
        if source is not None:
            source["source_uri"] = payload.get("source_uri")
        messages = {msg["id"]: msg for msg in payload.get("messages", [])}

        def quotes(field, f=f, messages=messages):
            output = []
            for quote in json.loads(f.get(field) or "[]"):
                message = messages.get(quote["message_id"], {})
                output.append(
                    {
                        **quote,
                        **{
                            k: message[k]
                            for k in (
                                "role",
                                "timestamp",
                                "source_type",
                                "call_id",
                                "tool_name",
                                "tool_failed",
                            )
                            if k in message
                        },
                        "message_available": bool(message),
                    }
                )
            return output

        items.append(
            {
                "fact": {
                    k: v for k, v in f.items() if k not in ("evidence", "validation_evidence")
                },
                "claims": quotes("evidence"),
                "validation": quotes("validation_evidence"),
                "source": source,
                "source_available": ep is not None,
            }
        )
    return {
        "facts": items,
        "missing_fact_ids": [fid for fid in wanted if fid not in found],
        "scope": "stored evidence; use recall/latest for current state",
        **({"knowledge_history": history} if history is not None else {}),
    }
