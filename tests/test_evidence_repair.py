"""Loosely copied quotes resolve to exact source text; real inventions are all reported."""

import json

import pytest

from graph_memory.diagnostics import diagnostic
from graph_memory.feeds import Breaker, worker_tick
from graph_memory.models import Extraction, Transcript, source_span
from graph_memory.service import MemoryService
from graph_memory.session_sources import feed_records

from .helpers import MYSQL, PG, PROJECT
from .test_session_sources import claude

SOURCE = "Intro.\n   12\tAtlas **uses** `MySQL`   in\n   13\tproduction since café day."


def transcript(content=SOURCE, other="Atlas will move to Postgres next quarter."):
    return Transcript.model_validate(
        {
            "namespace": "eval:unit",
            "source_id": "s",
            "session_id": "s",
            "messages": [
                {"id": "m1", "role": "user", "content": content},
                {"id": "m2", "role": "user", "content": other},
            ],
        }
    )


def extraction(*quotes):
    return Extraction.model_validate(
        {
            "entities": [PROJECT, MYSQL, PG],
            "facts": [
                {
                    "subject": PROJECT["key"],
                    "target": MYSQL["key"],
                    "relation": "uses_database",
                    "summary": "Atlas uses MySQL.",
                    "evidence": [{"message_id": mid, "quote": quote}],
                }
                for mid, quote in quotes
            ],
        }
    )


def test_loose_quote_is_replaced_by_the_exact_source_span():
    candidate = extraction(("m1", "Atlas uses MySQL in production since café day"))
    candidate.validate_evidence(transcript())
    stored = candidate.facts[0].evidence[0].quote
    assert stored in SOURCE and stored.startswith("Atlas **uses**") and "13\t" in stored


def test_right_text_under_the_wrong_message_id_is_repaired():
    candidate = extraction(("m1", "Atlas will move to Postgres next quarter."))
    candidate.validate_evidence(transcript())
    assert candidate.facts[0].evidence[0].message_id == "m2"


def test_short_ambiguous_and_invented_quotes_are_still_rejected():
    assert source_span("uses MySQL", "Atlas *uses* MySQL") is None  # too short to repair
    twice = "Atlas **uses** MySQL in production. Later: Atlas *uses* MySQL in production."
    assert source_span("Atlas uses MySQL in production", twice) is None
    with pytest.raises(ValueError, match="exact substring"):
        extraction(("m1", "Atlas uses Oracle in production since forever")).validate_evidence(
            transcript()
        )


def test_every_bad_quote_is_reported_with_its_own_field():
    candidate = extraction(
        ("m1", "Atlas uses Oracle in production today"),
        ("m2", "Atlas will move to Postgres next quarter."),
        ("m2", "Atlas will move to DynamoDB next quarter"),
    )
    candidate.facts[2].validation_evidence = candidate.facts[0].evidence[:1]
    with pytest.raises(ValueError) as caught:
        candidate.validate_evidence(transcript())
    issue = diagnostic(caught.value, "evidence_validation")
    assert issue["code"] == "evidence_quote_mismatch"
    assert issue["locations"] == [
        ["facts", 0, "evidence", 0, "quote"],
        ["facts", 2, "evidence", 0, "quote"],
        ["facts", 2, "validation_evidence", 0, "quote"],
    ]


class Counting:
    timeout = 600

    def __init__(self):
        self.calls = 0

    def generate(self, instructions, payload, output):
        self.calls += 1
        return Extraction(entities=[], facts=[])


def test_batches_without_a_new_claim_never_reach_the_model(graph, tmp_path):
    store, ns = graph
    call = [{"type": "tool_use", "id": "r1", "name": "Bash", "input": {"command": "ls"}}]
    path = tmp_path / "s.jsonl"
    # Eight tool calls fill the first batch; the claim lands in the second.
    path.write_text(
        "".join(claude("assistant", [{**call[0], "id": f"r{i}"}]) for i in range(8))
        + claude("user", "Atlas uses MySQL.")
    )
    service = MemoryService(store, Counting())
    fed = feed_records(service, ns, path, "s")
    assert len(fed["receipts"]) == 2
    receipts = []
    while store.pending(ns):
        receipts += worker_tick(service, ns)["receipts"]
    skipped = [r for r in receipts if r.get("skipped")]
    assert len(skipped) == 1 and skipped[0]["model_calls"] == 0
    assert service.llm.calls == 1  # only the batch that carries the claim
    info = store.episode(ns, skipped[0]["episode_id"])
    assert info["status"] == "complete" and json.loads(info["model_info"])["skipped"]


def test_namespace_fault_charges_no_episode_and_pauses_the_queue(graph, tmp_path):
    store, ns = graph
    for i in range(2):
        (tmp_path / f"{i}.md").write_text(f"Note {i}: Atlas uses MySQL.")
    from graph_memory.daemon import scan_bank

    service = MemoryService(store, Counting())
    service.breaker = Breaker()
    scan_bank(service, ns, [tmp_path], {})
    store.transaction(
        lambda tx: tx.run(
            'CREATE (:MemoryEntity {id:$id,namespace:$ns,key:"x",aliases:[]})', id=ns + ":x", ns=ns
        ).consume()
    )
    # An empty extraction changes the episode only, so force a full-capture check.
    store.transaction(
        lambda tx: tx.run(
            "MATCH (s:MemorySpace {id:$ns}) SET s.journal_set_hash='0'", ns=ns
        ).consume()
    )
    receipts = worker_tick(service, ns)["receipts"]
    assert len(receipts) == 1 and receipts[0]["systemic"]
    assert receipts[0]["diagnostic"]["code"] == "journal_state_mismatch"
    assert service.breaker.open and worker_tick(service, ns)["receipts"] == []
    rows = store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN coalesce(e.attempts,0) AS attempts,"
            "e.quarantine_engine AS quarantine",
            ns=ns,
        ).data()
    )
    assert all(r["attempts"] == 0 and r["quarantine"] is None for r in rows)
