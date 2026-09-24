"""Bounded, source-linked research output; never a fact confirmation mechanism."""

from . import models as m
from .recall_provenance import fresh_results
from .retrieval import tokens
from .store import digest


def packet(graph, transcripts):
    candidates = [f for f in graph["uncertain"] if not f.get("retracted")][:10]
    sources = {}
    for payload in transcripts:
        transcript = m.Transcript.model_validate(payload)
        fresh = fresh_results(transcript)
        calls = {}
        for msg in transcript.messages:
            if msg.call_id and msg.source_type == "tool_call":
                calls.setdefault(msg.call_id, msg)
        for msg in transcript.messages:
            if msg.id not in fresh and msg.source_type != "user_assertion":
                continue
            if msg.id in transcript.memory_origins or msg.tool_failed:
                continue
            ref = transcript.verified_source_refs.get(msg.id)
            if transcript.source_format == "session-records-v1":
                ref = digest([transcript.namespace, transcript.session_id, msg.id])
            if not ref:
                # Unsourced notes and legacy/context material cannot validate a claim.
                continue
            call = calls.get(msg.call_id) if msg.call_id else None
            sources[ref] = {
                "id": ref,
                "role": msg.role,
                "source_type": msg.source_type,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                "tool_name": msg.tool_name,
                "tool_call": call.content[:1200] if call else None,
                "tool_call_truncated": bool(
                    call and (len(call.content) > 1200 or "record_split_into_chunks" in call.gaps)
                ),
                "tool_call_gaps": call.gaps if call else [],
                "content": msg.content[:16000],
                "truncated": len(msg.content) > 16000,
                "gaps": msg.gaps,
            }
    claims = []
    for fact in candidates:
        terms = tokens(" ".join(str(fact.get(k) or "") for k in ("summary", "subject", "target")))
        ranked = sorted(
            sources.values(),
            key=lambda source: (-len(terms & tokens(source["content"])), source["id"]),
        )
        # This is candidate selection, not validation. Zero overlap can mean a
        # paraphrase was missed; the model must abstain if these sources don't help.
        selected = [s for s in ranked if terms & tokens(s["content"])][:4]
        claims.append(
            {
                "fact": {
                    k: fact[k]
                    for k in (
                        "id",
                        "subject",
                        "relation",
                        "target",
                        "summary",
                        "status",
                        "valid_at",
                    )
                    if k in fact
                },
                "sources": selected,
            }
        )
    return claims


def validate(claims, reviews):
    wanted = {c["fact"]["id"]: {s["id"] for s in c["sources"]} for c in claims}
    if len(reviews) != len(wanted) or {r.fact_id for r in reviews} != set(wanted):
        raise ValueError("Review each eligible uncertain fact exactly once")
    for review in reviews:
        if not set(review.evidence_ids) <= wanted[review.fact_id]:
            raise ValueError("Claim review cites a source outside its eligible original evidence")
        if review.verdict != "insufficient" and not review.evidence_ids:
            raise ValueError("Supported/contradicted reviews require original evidence IDs")
