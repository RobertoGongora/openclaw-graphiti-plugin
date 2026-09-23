"""Bounded MCP views over the unchanged temporal projection; no model or graph writes."""

import json
import math
import re
from collections import Counter
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from . import aliases
from . import models as m
from .store import digest, normalized
from .temporal import role, shared_slots

LANES = ("current", "planned", "events", "uncertain", "documented", "conflicts", "history")
STOP = set(
    "a an the what which when where who why how is are was were do does did of for to in on at about me my our it its and or with has have tell please currently now we you i us use uses using used show give know".split()
)
ENTITY_STOP = frozenset(STOP)
STOP.update(
    "exact latest last current evidence confirm confirms confirmed support supported".split()
)
STOP.update(
    "reports report conflicting status changed change finish finished before after between date".split()
)


class RecallView(m.HistoricalScope):
    entity: m.Text = Field(
        description="Entity name or key, such as Atlas. Use memory_search_entities when the identity is unclear."
    )
    as_of: AwareDatetime | None = None
    question: m.Text | None = Field(
        default=None,
        description="Optional question about this entity, used to rank relevant facts.",
    )
    detail: Literal["compact", "full"] = "compact"
    limit: Annotated[int, Field(ge=1, le=100)] = 5
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0
    include_history: bool = False

    @model_validator(mode="before")
    @classmethod
    def legacy_query(cls, value):
        if isinstance(value, dict) and "query" in value:
            if "entity" in value and value["entity"] != value["query"]:
                raise ValueError("Use entity; legacy query must not select a different entity")
            value = dict(value)
            value.setdefault("entity", value.pop("query"))
        return value


class EntitySearch(m.HistoricalScope):
    query: m.Text = Field(
        description="Name, alias, or words identifying a person, project, or other entity."
    )
    kind: m.Kind | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 5
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0


class LatestView(m.Latest):
    detail: Literal["compact", "full"] = "compact"
    limit: Annotated[int, Field(ge=1, le=20)] = 5


class EvidenceRequest(m.HistoricalScope):
    fact_ids: Annotated[list[m.Key], Field(min_length=1, max_length=10)]


def tokens(text: str) -> set[str]:
    # Small morphological normalization, not a domain synonym dictionary.
    forms: dict[str, str] = {
        "deployed": "deploy",
        "deployment": "deploy",
        "deploying": "deploy",
        "evaluations": "evaluation",
        "eval": "evaluation",
        "evals": "evaluation",
        "results": "result",
        "benchmarks": "benchmark",
    }
    return {
        forms[w] if w in forms else w
        for w in re.findall(r"[^\W_]+", normalized(text))
        if w not in STOP
    }


def reported_time(f):
    # A report's timestamp orders reports; it never supplies a missing event date.
    stamp = f.get("reported_at")
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else 0


def recency(f):
    """Newest event first; undated records follow, newest report first. One
    record's event time is never compared with another record's report time."""
    ts = f.get("valid_ts")
    return (0, -ts, -reported_time(f)) if ts is not None else (1, 0, -reported_time(f))


def relevance(raw, terms, shared):
    """Rank individual records. Only exclusive state roles inherit old-value matches."""
    facts = [f for lane in LANES for f in raw[lane]]
    words = {
        f["id"]: tokens(
            " ".join(
                str(f.get(k) or "")
                for k in (
                    "summary",
                    "subject",
                    "target",
                    "relation",
                    "slot",
                    "subject_name",
                    "target_name",
                )
            )
        )
        for f in facts
    }
    # Evaluation questions often name the study, while its result states the
    # measured validation/benchmark numbers instead of repeating the question.
    measured = set()
    for f in facts:
        ws = words[f["id"]]
        if {"validation", "benchmark"} & ws:
            ws.add("evaluation")
        if re.search(r"\d(?:\.\d+)?\s*%|\b\d+\s+(?:passed|failed)\b", f["summary"]):
            ws.add("result")
            measured.add(f["id"])
    frequency = Counter(w for ws in words.values() for w in ws)
    scores = {
        fid: sum(1 + math.log(1 + len(words) / frequency[t]) for t in sorted(terms & ws))
        for fid, ws in words.items()
    }
    for f in facts:
        concepts = tokens(f.get("relation", "")) | {f.get("target_kind", "")}
        scores[f["id"]] += 6 * len(terms & concepts & {"database", "framework", "language"})
        if "result" in terms and f["id"] in measured:
            scores[f["id"]] += 4
    roles = {}
    for f in facts:
        if (f["subject"], f["relation"], f.get("slot")) in shared and f[
            "relation"
        ] in m.SLOT_RELATIONS:
            key = role(f, shared)
            roles[key] = max(roles.get(key, 0), scores[f["id"]])
    return {f["id"]: max(scores[f["id"]], roles.get(role(f, shared), 0)) for f in facts}


def group_fields(f, lane, copies=1, sessions=1):
    """Repeat counts shared by compact and full detail: repetition, never proof."""
    result = {}
    count = max(copies, len(f.get("corroborating_fact_ids", [])))
    if count > 1:
        result["report_count" if lane == "uncertain" else "support_count"] = count
    if sessions > 1:
        # Said again in another conversation: a reason to check the claim, not proof.
        result["source_sessions"] = sessions
    return result


def compact_fact(f, lane, copies=1, sessions=1):
    result = {
        "id": f["id"],
        "text": f["summary"][:500],
        "lane": lane,
        "at": f.get("valid_at"),
        "source": f["episode_id"],
    }
    if len(f["summary"]) > 500:
        result["text_truncated"] = True
    if f.get("confirmed"):
        # Current because a person said so, not because the engine verified it.
        result["confirmed_by_user"] = True
    if f.get("documented_at") and not f.get("valid_at"):
        result["documented_at"] = f["documented_at"]
    if f.get("reported_at"):
        result["reported_at"] = f["reported_at"]
    result.update(group_fields(f, lane, copies, sessions))
    return result


def entities(items):
    return [{k: e[k] for k in ("key", "name", "kind")} for e in items]


def metadata(raw):
    return {k: raw[k] for k in ("as_of", "revision", "freshness", "knowledge_history") if k in raw}


def recall(store, r):
    raw = store.recall(
        r.namespace,
        r.entity,
        r.as_of,
        r.limit,
        _complete=r.detail == "compact" or bool(r.question),
        known_at=r.known_at,
        at_change=r.at_change,
    )
    if r.detail == "full" and not r.question:
        return raw
    terms = tokens(r.question or "") - tokens(r.entity)
    shared = shared_slots(f for lane in LANES for f in raw[lane])
    # Match across all evidence BEFORE hiding history. A question mentioning the old
    # database must also retrieve the replacement in the same exclusive role.
    scores = relevance(raw, terms, shared)
    groups = {}
    for lane in LANES:
        if lane == "history" and not r.include_history:
            continue
        for f in raw[lane]:
            # Preserve distinct wording: shared endpoints do not make a later
            # summary equivalent to an earlier detailed report or correction.
            wording = (f.get("valid_at"), normalized(f["summary"]))
            key = (lane, *role(f, shared), f["target"], f["status"], *wording)
            groups.setdefault(key, []).append(f)
    ranked = []
    for key, copies in groups.items():
        # The copy shown for identical wording: newest report, then newest ingestion
        # when no report time exists. This chooses attribution, never rank.
        copies.sort(key=lambda f: f["id"])
        copies.sort(key=lambda f: (reported_time(f), f.get("recorded_at") or ""), reverse=True)
        f = copies[0]
        score = max(scores[c["id"]] for c in copies)
        if terms and score == 0:
            continue
        ranked.append((key[0], f, copies, score))
    priority = {
        "conflicts": 0,
        "current": 1,
        "planned": 2,
        "events": 3,
        "uncertain": 4,
        "documented": 5,
        "history": 6,
    }
    ranked.sort(key=lambda x: (-x[3], priority[x[0]], *recency(x[1]), x[1]["id"]))
    selected = ranked[r.offset : r.offset + r.limit]

    def sessions(copies):
        return len({c.get("session_id") for c in copies})

    facts = [
        compact_fact(f, lane, len(copies), sessions(copies)) for lane, f, copies, _ in selected
    ]
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
    result = {
        "entity": r.entity,
        # The words that ranked. Empty means the question held only stop words or
        # the entity's own name, and the facts are unranked as if it were omitted.
        **({"question": r.question, "question_terms": sorted(terms)} if r.question else {}),
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
    if r.detail == "full":
        result["facts"] = [
            {**f, "lane": lane, **group_fields(f, lane, len(copies), sessions(copies))}
            for lane, f, copies, _ in selected
        ]
    return result


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

        def quotes(field, f=f, messages=messages, payload=payload):
            output = []
            for quote in json.loads(f.get(field) or "[]"):
                message = messages.get(quote["message_id"], {})
                source_ref = payload.get("verified_source_refs", {}).get(quote["message_id"])
                if payload.get("source_format") == "session-records-v1" and message:
                    source_ref = digest(
                        [payload["namespace"], payload["session_id"], quote["message_id"]]
                    )
                origin = payload.get("memory_origins", {}).get(quote["message_id"])
                if origin and payload.get("source_format") == "session-records-v1":
                    # Stored message IDs, the same form as source_message_id and
                    # as MemoryMessage.memory_read_refs.
                    origin = {
                        **origin,
                        "result_ids": [
                            digest([payload["namespace"], payload["session_id"], rid])
                            for rid in origin.get("result_ids", [])
                        ],
                    }
                output.append(
                    {
                        **quote,
                        "source_message_id": source_ref,
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
                        **({"memory_origin": origin} if origin else {}),
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


def search_entities(store, r):
    needle = normalized(r.query)
    terms = sorted(set(re.findall(r"[^\W_]+", needle)) - ENTITY_STOP) or [needle]
    history = None
    if r.known_at is not None or r.at_change is not None:
        from .journal import Journal

        snapshot = Journal(store).snapshot(r.namespace, known_at=r.known_at, sequence=r.at_change)
        candidates = list(snapshot["state"]["MemoryEntity"].values())
        paged = None
        history = {k: v for k, v in snapshot.items() if k != "state"}
    else:
        kind = r.kind.value if r.kind else None

        def find(tx):
            if aliases.indexed(tx, r.namespace):
                return aliases.search(tx, r.namespace, needle, terms, kind, r.offset, r.limit)
            rows = tx.run(
                "MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.merged_into IS NULL "
                "AND ($kind IS NULL OR e.kind=$kind) "
                "AND (any(a IN e.aliases WHERE a CONTAINS $needle) "
                "OR all(term IN $terms WHERE any(a IN e.aliases WHERE a CONTAINS term))) "
                "RETURN properties(e) AS entity",
                ns=r.namespace,
                kind=kind,
                needle=needle,
                terms=terms,
            ).data()
            return None, [row["entity"] for row in rows]

        # With alias nodes the database ranks and pages; the list scan returns everything.
        paged, candidates = store.read(find)
    ranked = []
    for e in candidates:
        if e.get("merged_into") or (r.kind and e["kind"] != r.kind.value):
            continue
        names = [normalized(a) for a in [e["key"], e["name"], *e.get("aliases", [])]]
        exact = needle in names
        partial = any(needle in a for a in names)
        all_terms = all(any(term in a for a in names) for term in terms)
        if not (exact or partial or all_terms):
            continue
        match = "exact" if exact else "substring" if partial else "words"
        score = 0 if normalized(e["key"]) == needle else 1 if exact else 2 if partial else 3
        ranked.append(
            (
                score,
                e["key"],
                {
                    "key": e["key"],
                    "name": e["name"],
                    "kind": e["kind"],
                    "match": match,
                    "aliases": list(dict.fromkeys(e.get("aliases", [])))[:5],
                },
            )
        )
    ranked.sort(key=lambda x: (x[0], x[1]))
    total = len(ranked) if paged is None else paged
    window = ranked[r.offset : r.offset + r.limit] if paged is None else ranked
    rows = [row for _, _, row in window]
    end = r.offset + len(rows)
    return {
        "query": r.query,
        "matches": rows,
        "total": total,
        "next_offset": end if end < total else None,
        **({"knowledge_history": history} if history else {}),
    }
