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
STOP.add("there")
SCHEMA_CONCEPTS = {"database", "framework", "language"}


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
        description="Nonempty short entity name or alias (at least one character), e.g. Ketch or T3 Code. Empty listing is not supported. Every word must match that entity's names. For a topic or full question, use memory_search instead."
    )
    kind: m.Kind | None = None
    limit: Annotated[int, Field(ge=1, le=20, description="Number of entities, 1–20.")] = 5
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0


class QuestionSearch(m.HistoricalScope):
    question: Annotated[str, Field(min_length=1, max_length=2000)] = Field(
        description="A topic or natural-language question to search across remembered facts, without choosing an entity."
    )
    as_of: AwareDatetime | None = None
    limit: Annotated[int, Field(ge=1, le=20, description="Number of facts, 1–20.")] = 5
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0
    include_history: bool = False


class LatestView(m.Latest):
    detail: Literal["compact", "full"] = "compact"
    limit: Annotated[int, Field(ge=1, le=20)] = 5


class EvidenceRequest(m.HistoricalScope):
    fact_ids: Annotated[list[m.Key], Field(min_length=1, max_length=10)] = Field(
        description="Batch of 1–10 fact IDs from recall/search."
    )
    detail: Literal["index", "compact", "full"] = Field(
        default="full",
        description="index: short claim/source inventory; compact: bounded quote excerpts and provenance; full: exact quotes and all stored metadata. Use full before sourced writes or when excerpts omit needed context.",
    )


class EvidenceView(EvidenceRequest):
    # Internal callers retain the original full-evidence contract.
    detail: Literal["index", "compact", "full"] = Field(
        default="compact", description=EvidenceRequest.model_fields["detail"].description
    )


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
        "uploads": "upload",
        "uploaded": "upload",
        "uploading": "upload",
        "downloads": "download",
        "downloaded": "download",
        "downloading": "download",
        "released": "release",
        "releases": "release",
        "releasing": "release",
        "created": "create",
        "creates": "create",
        "creating": "create",
        "creation": "create",
        "threads": "thread",
        "endpoints": "endpoint",
        "tokens": "token",
        "files": "file",
        "lists": "list",
        "cycles": "cycle",
        "batches": "batch",
        "batching": "batch",
        "rebuilds": "rebuild",
        "rebuilt": "rebuild",
        "rebuilding": "rebuild",
        "recreated": "recreate",
        "recreating": "recreate",
        "recreation": "recreate",
        "deferred": "defer",
        "deferring": "defer",
        "decided": "decide",
    }
    words = {
        forms[w] if w in forms else w
        for w in re.findall(r"[^\W_]+", normalized(text))
        if w not in STOP
    }
    # A small action equivalence used only for retrieval, never entity identity
    # or factual inference. Keep original tokens so exact wording still matches.
    if {"rebuild", "recreate"} & words:
        words.update({"rebuild", "recreate"})
    return words


def reported_time(f):
    # A report's timestamp orders reports; it never supplies a missing event date.
    stamp = f.get("reported_at")
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else 0


def recency(f):
    """Newest event first; undated records follow, newest report first. One
    record's event time is never compared with another record's report time."""
    ts = f.get("valid_ts")
    return (0, -ts, -reported_time(f)) if ts is not None else (1, 0, -reported_time(f))


ANCHOR_KINDS = {"project", "person", "organization", "service", "database", "framework", "language"}
GENERIC_NAMES = (
    STOP
    | SCHEMA_CONCEPTS
    | {
        "api",
        "code",
        "server",
        "service",
        "app",
        "agent",
        "tool",
        "thread",
        "token",
        "session",
        "user",
        "project",
        "production",
        "sandbox",
        "endpoint",
        "memory",
        "backup",
    }
)


def named_subjects(facts, question):
    """Canonical names only: noisy aliases must not narrow a question's scope."""
    query = normalized(question)
    found = {}
    for f in facts:
        for side in ("subject", "target"):
            key = f.get(side, "")
            kind = f.get(side + "_kind") or key.partition(":")[0]
            if kind not in ANCHOR_KINDS:
                continue
            name = normalized(f.get(side + "_name") or key.partition(":")[2].replace("-", " "))
            if (
                not name
                or name in GENERIC_NAMES
                or (len(name) < 3 and not any(c.isdigit() for c in name))
            ):
                continue
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", query):
                found[key] = name
    # Prefer the specific name over a nested fragment (e.g. T3 Code over Code).
    return {
        key: name
        for key, name in found.items()
        if not any(
            name != other and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", other)
            for other in found.values()
        )
    }


def relevance(raw, terms, shared, question=""):
    """Bounded lexical, identity and provenance signals; none changes truth lanes."""
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
    measured = set()
    for f in facts:
        ws = words[f["id"]]
        if {"validation", "benchmark"} & ws:
            ws.add("evaluation")
        if re.search(r"\d(?:\.\d+)?\s*%|\b\d+\s+(?:passed|failed)\b", f["summary"]):
            ws.add("result")
            measured.add(f["id"])
    frequency = Counter(w for ws in words.values() for w in ws)
    schema_question = not ((terms & frequency.keys()) - SCHEMA_CONCEPTS)
    anchors = named_subjects(facts, question) if terms else {}
    identity_terms = set().union(*(tokens(name) for name in anchors.values())) if anchors else set()
    topical_terms = terms - identity_terms
    lengths = {f["id"]: max(1, len(tokens(f["summary"]))) for f in facts}
    average = sum(lengths.values()) / max(1, len(lengths))
    scores = {}
    for f in facts:
        fid = f["id"]
        matches = terms & words[fid]
        score = sum(1 + math.log(1 + len(words) / frequency[t]) for t in sorted(matches))
        # Moderate, bounded length normalization. A long answer can still win
        # through additional relevant terms; protect short factual statements from
        # a corpus dominated by even shorter records.
        score /= min(2.5, 1 + 0.5 * max(0, lengths[fid] - max(50, average)) / max(50, average))
        topical = not topical_terms or bool(topical_terms & words[fid])
        if score and topical:
            identity_hits = len({f["subject"], f["target"]} & anchors.keys())
            score *= 1 + 0.5 * min(identity_hits, 2)
            # This is recorded validation, not a new verification of the source.
            # Neither the current lane nor repeated reports earn this preference.
            if not schema_question and (f.get("validation_message_refs") or f.get("confirmed")):
                score *= 1.35
        concepts = tokens(f.get("relation", ""))
        if schema_question:
            score += 6 * len(terms & concepts & SCHEMA_CONCEPTS)
        if "result" in terms and fid in measured:
            score += 4
        scores[fid] = score
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
    from .related import decision_question

    raw = store.recall(
        r.namespace,
        r.entity,
        r.as_of,
        r.limit,
        _complete=r.detail == "compact" or bool(r.question),
        known_at=r.known_at,
        at_change=r.at_change,
        _related_question=r.question if decision_question(r.question) else None,
    )
    if r.detail == "full" and not r.question:
        return raw
    terms = tokens(r.question or "") - tokens(r.entity)
    return ranked_view(raw, r, terms)


def ranked_view(raw, r, terms):
    shared = shared_slots(f for lane in LANES for f in raw[lane])
    # Match across all evidence BEFORE hiding history. A question mentioning the old
    # database must also retrieve the replacement in the same exclusive role.
    scores = relevance(raw, terms, shared, r.question or "")
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
    related_ids = set(raw.get("related_fact_ids", []))
    for output, (_, source, copies, _) in zip(facts, selected, strict=True):
        if any(f["id"] in related_ids for f in copies):
            output["subject"] = source["subject"]
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
    # A disagreement is the answer only when it matches as strongly as the best
    # result; conflicts sort first among equals, so they then lead page one. A
    # weaker partial match is still reported, with every side's ID to verify.
    disputed = [x for x in ranked if x[0] == "conflicts"]
    strongest = bool(disputed) and max(x[3] for x in disputed) == ranked[0][3]
    sides = []
    for _, f, copies, _ in disputed:
        dispute = role(f, shared)
        sides += [c["id"] for c in copies]
        sides += [c["id"] for c in raw["conflicts"] if role(c, shared) == dispute]
    sides = list(dict.fromkeys(sides))
    result = {
        "entity": getattr(r, "entity", None),
        # The words that ranked. Empty means the question held only stop words or
        # the entity's own name, and the facts are unranked as if it were omitted.
        **({"question": r.question, "question_terms": sorted(terms)} if r.question else {}),
        "status": "ambiguous"
        if raw["ambiguous"]
        else "not_found"
        if not raw["entities"]
        else "conflict"
        if strongest
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
            "conflicts_matching": sum(len(copies) for _, _, copies, _ in disputed),
        },
        # Up to ten, the most memory_evidence accepts in one call: the matching
        # conflicting records and the other sides of each disagreement.
        **(
            {
                "conflict_fact_ids": sides[:10],
                "conflict_fact_ids_truncated": len(sides) > 10,
            }
            if sides
            else {}
        ),
        "next_offset": next_offset if next_offset < len(ranked) else None,
        "evidence_tool": "memory_evidence",
        **metadata(raw),
    }
    if getattr(r, "detail", "compact") == "full":
        result["facts"] = [
            {**f, "lane": lane, **group_fields(f, lane, len(copies), sessions(copies))}
            for lane, f, copies, _ in selected
        ]
    return result


def search(store, r):
    # Reuse recall's temporal projection, conflict handling, deduplication and
    # evidence lanes. A topical match never promotes an uncertain report.
    terms = tokens(r.question)
    if not terms:
        return {
            "question": r.question,
            "status": "needs_search_terms",
            "facts": [],
            "next_offset": None,
            "guidance": "Include a subject or topic, such as Ketch uploads or database disk size.",
        }
    raw = store.recall(
        r.namespace,
        r.question,
        r.as_of,
        r.limit,
        _complete=True,
        known_at=r.known_at,
        at_change=r.at_change,
        _search=True,
    )
    result = ranked_view(raw, r, terms)
    result.pop("entity")
    result.pop("entities")
    result.pop("entity_matches_truncated")
    by_id = {f["id"]: f for lane in LANES for f in raw[lane]}
    for fact in result["facts"]:
        original = by_id[fact["id"]]
        fact.update(subject=original["subject"], target=original["target"])
    result["scope"] = (
        "Keyword search across stored facts; a miss is not proof that no relevant memory exists."
    )
    if not result["facts"]:
        result["guidance"] = (
            "Try fewer distinctive words or an alternative name. Use memory_search_entities for a short name, then memory_recall with the returned key and your question."
        )
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


def source_context(message, call, origin):
    """Describe recorded collection methods, never the truth of a cited claim.

    An opaque shell result stays opaque: expose its recorded command rather
    than guessing which file (or remote system) produced a particular quote.
    """
    source_type = message.get("source_type")
    tool = message.get("tool_name") or ""
    role = message.get("role")
    if not message:
        kind = "unavailable"
    elif origin:
        kind = "memory_derived_report"
    elif source_type in {"context", "memory_read", "memory_write", "tool_call"}:
        kind = source_type
    elif role == "user":
        kind = "user_assertion"
    elif role == "assistant":
        kind = "assistant_report"
    elif role == "tool":
        if tool in {"mcp__context7__query-docs", "mcp__context7__get-library-docs"}:
            kind = "documentation_lookup"
        elif tool in {"Bash", "exec_command", "functions.exec_command", "shell_command"}:
            kind = "shell_output"
        elif tool in {"Read", "read_file"}:
            kind = "file_read"
        else:
            kind = "tool_output"
    else:
        kind = "recorded_context"
    result = {"kind": kind}
    if call:
        content = call.get("content", "")
        result["tool_call"] = {
            "message_id": call["id"],
            "tool_name": call.get("tool_name"),
            "arguments": content[:1200],
            "arguments_truncated": len(content) > 1200
            or "record_split_into_chunks" in call.get("gaps", []),
        }
    if message.get("touches"):
        result["artifacts"] = [
            {k: t[k] for k in ("path", "operation", "captured", "gap") if k in t}
            for t in message["touches"]
        ]
    if message.get("gaps"):
        result["gaps"] = message["gaps"]
    return result


def evidence_excerpt(item, detail):
    """A bounded navigation view; exact quotes remain available by fact ID."""
    source = item["fact"]
    summary = source.get("summary", "")
    fact = {
        k: source[k]
        for k in (
            "id",
            "subject",
            "relation",
            "target",
            "status",
            "retracted",
            "valid_at",
            "confirmed_at",
            "confirmed_by",
        )
        if k in source
    }
    fact.update(summary=summary[:240], summary_truncated=len(summary) > 240)
    out = {
        "fact": fact,
        "source": {"id": item["source"]["id"]} if item["source"] else None,
        "source_available": item["source_available"],
        "counts": {k: len(item[k]) for k in ("claims", "validation")},
    }
    if detail == "index":
        sources = {}
        for quote in item["claims"] + item["validation"]:
            key = quote.get("source_message_id") or quote["message_id"]
            sources[key] = {
                "id": key,
                "role": quote.get("role"),
                "kind": quote["source_context"]["kind"],
            }
        out["sources"] = list(sources.values())[:6]
        out["sources_truncated"] = len(sources) > 6
        return out
    for lane in ("claims", "validation"):
        out[lane] = []
        for quote in item[lane][:3]:
            text = quote["quote"]
            q = {
                k: quote[k]
                for k in (
                    "message_id",
                    "source_message_id",
                    "role",
                    "timestamp",
                    "source_type",
                    "tool_name",
                    "tool_failed",
                    "message_available",
                )
                if k in quote
            }
            q.update(
                source_context={"kind": quote["source_context"]["kind"]},
                quote_truncated=len(text) > 400,
            )
            # A partial quote must never masquerade as the exact source text
            # required by a sourced write or a validation judgment.
            q["quote_excerpt" if len(text) > 400 else "quote"] = text[:400]
            if quote["source_context"].get("gaps"):
                q["has_source_gaps"] = True
            if quote.get("memory_origin"):
                q["memory_derived"] = True
            out[lane].append(q)
        out[lane + "_truncated"] = len(item[lane]) > 3
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

        journal = Journal(store)
        wanted_set = set(wanted)
        snap = journal.snapshot(
            r.namespace,
            known_at=r.known_at,
            sequence=r.at_change,
            select=lambda label, node: label == "MemoryFact" and node["id"] in wanted_set,
        )
        state = snap["state"]
        records = [state["MemoryFact"][fid] for fid in wanted if fid in state["MemoryFact"]]
        episode_ids = {f["episode_id"] for f in records}
        episodes = (
            journal.snapshot(
                r.namespace,
                sequence=snap["sequence"],
                select=lambda label, node: label == "MemoryEpisode" and node["id"] in episode_ids,
            )["state"]["MemoryEpisode"]
            if episode_ids
            else {}
        )
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
        calls = {}
        for msg in messages.values():
            if msg.get("call_id") and msg.get("source_type") == "tool_call":
                # Keep the earliest available fragment, not the tail of a long
                # call. Split source records are explicitly marked incomplete.
                calls.setdefault(msg["call_id"], msg)

        def quotes(field, f=f, messages=messages, payload=payload, calls=calls):
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
                        "source_context": source_context(
                            message, calls.get(message.get("call_id")), origin
                        ),
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
    if r.detail != "full":
        items = [evidence_excerpt(item, r.detail) for item in items]
    return {
        "facts": items,
        "missing_fact_ids": [fid for fid in wanted if fid not in found],
        **(
            {
                "detail": r.detail,
                "expand": {
                    "tool": "memory_evidence",
                    "arguments": {
                        "fact_ids": wanted,
                        "detail": "full",
                        **({"at_change": history["sequence"]} if history else {}),
                    },
                },
            }
            if r.detail != "full"
            else {}
        ),
        "scope": "stored evidence; source_context describes recorded sources, not live verification; use recall/latest for current state",
        **({"knowledge_history": history} if history is not None else {}),
    }


def search_entities(store, r):
    needle = normalized(r.query)
    terms = sorted(set(re.findall(r"[^\W_]+", needle)) - ENTITY_STOP) or [needle]
    history = None
    if r.known_at is not None or r.at_change is not None:
        from .journal import Journal

        snapshot = Journal(store).snapshot(
            r.namespace,
            known_at=r.known_at,
            sequence=r.at_change,
            select=lambda label, node: label == "MemoryEntity",
        )
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
        **(
            {
                "guidance": "No entity name matched. This does not mean no relevant facts exist. Use memory_search for this topic/question, or try one short entity name and pass the question to memory_recall.",
                "suggested_call": {
                    "tool": "memory_search",
                    "arguments": {
                        "question": r.query,
                        **({"known_at": r.known_at.isoformat()} if r.known_at is not None else {}),
                        **({"at_change": r.at_change} if r.at_change is not None else {}),
                    },
                },
            }
            if total == 0
            else {}
        ),
        **({"knowledge_history": history} if history else {}),
    }
