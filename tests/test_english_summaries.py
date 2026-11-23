"""English summaries over Spanish sources, through the real engine.

The model here is a fixture: it returns what the instructions ask for, so these
tests show what the engine keeps and serves, not what a model writes. The model's
own behaviour is measured by evals/english_summaries.py."""

import pytest

from evals.english_summaries import check_case, load_cases, search, transcript
from graph_memory.llm import EXTRACTION_INSTRUCTIONS
from graph_memory.models import EpisodeRequest, Extraction
from graph_memory.service import MemoryService

CASES = {case["id"]: case for case in load_cases()}
FARO = {"key": "project:faro", "name": "Faro", "kind": "project"}
# A role name with none of the question's words: only the summary can answer it.
BROKER = "event-bus"


def fact(subject, relation, target, summary, quotes, status="active", **extra):
    return {
        "subject": subject,
        "relation": relation,
        "target": target,
        "status": status,
        "summary": summary,
        "evidence": [{"message_id": mid, "quote": quote} for mid, quote in quotes],
        **extra,
    }


ENGLISH = {
    "es-later-correction": {
        "entities": [
            FARO,
            {"key": "service:rabbitmq", "name": "RabbitMQ", "kind": "service"},
            {"key": "service:kafka", "name": "Kafka", "kind": "service"},
            {"key": "service:nats-jetstream", "name": "NATS JetStream", "kind": "service"},
        ],
        "facts": [
            fact(
                "project:faro",
                "uses_service",
                "service:rabbitmq",
                "Proposal, not decided: use RabbitMQ as the message broker of the Faro payments service.",
                [("u1", "usar RabbitMQ como broker de mensajes del servicio de pagos")],
                "planned",
                slot=BROKER,
                valid_at="2026-10-01T09:00:00Z",
            ),
            fact(
                "project:faro",
                "uses_service",
                "service:kafka",
                "The team decided to use Kafka as the message broker of the Faro payments service.",
                [("u2", "Decidimos usar Kafka como broker de mensajes")],
                "planned",
                slot=BROKER,
                valid_at="2026-10-02T10:00:00Z",
            ),
            fact(
                "project:faro",
                "uses_service",
                "service:nats-jetstream",
                "Correction: Faro will not use Kafka. The final decision is NATS JetStream as the "
                "message broker of the payments service; it is not yet deployed to production.",
                [
                    ("u3", "al final no vamos a usar Kafka"),
                    ("u3", "La decisión final para Faro es NATS JetStream"),
                ],
                "planned",
                slot=BROKER,
                valid_at="2026-10-03T11:00:00Z",
            ),
        ],
    },
    "es-technical-ids": {
        "entities": [
            {"key": "service:pg-prod-02", "name": "pg-prod-02", "kind": "service"},
            {"key": "issue:inc-4721", "name": "INC-4721", "kind": "issue"},
        ],
        "facts": [
            fact(
                "service:pg-prod-02",
                "resolved",
                "issue:inc-4721",
                "Incident INC-4721 on pg-prod-02 was resolved by raising max_connections from 200 "
                "to 500 with the faro-api chart v3.14.2; the cluster was not restarted.",
                [
                    ("u1", "se resolvió subiendo max_connections de 200 a 500"),
                    ("u1", "No se reinició el clúster."),
                ],
                valid_at="2026-10-05T14:30:00Z",
            )
        ],
    },
    "es-en-mixed": {
        "entities": [FARO, {"key": "topic:checkout-v2", "name": "checkout_v2", "kind": "topic"}],
        "facts": [
            fact(
                "project:faro",
                "about",
                "topic:checkout-v2",
                "Proposed, not yet approved: enable the checkout_v2 feature flag in production on "
                "November 15 for the Faro rollout.",
                [("u1", "propuse activar el feature flag checkout_v2 en producción")],
                "planned",
                valid_at="2026-10-08T08:00:00Z",
            ),
            fact(
                "project:faro",
                "about",
                "topic:checkout-v2",
                "For Faro, the checkout_v2 feature flag is enabled only in staging.",
                [("u1", "For now we only enabled it in staging.")],
                valid_at="2026-10-08T08:00:00Z",
            ),
        ],
    },
}


def spanish(extraction):
    """What an extractor that copies the source language produces."""
    copy = Extraction.model_validate(extraction).model_dump(mode="json")
    for f in copy["facts"]:
        f["summary"] = " ".join(e["quote"] for e in f["evidence"])
    return copy


class Fixture:
    def __init__(self, extraction):
        self.extraction, self.instructions = extraction, None

    def generate(self, instructions, payload, output):
        self.instructions = instructions
        return output.model_validate(self.extraction)


def extract(store, ns, case, extraction):
    source = transcript(case, ns)
    eid = store.stage(source)["episode_id"]
    model = Fixture(extraction)
    service = MemoryService(store, model)
    assert service.extract(EpisodeRequest(namespace=ns, episode_id=eid))["status"] == "complete"
    facts = Extraction.model_validate_json(store.episode(ns, eid)["extraction_payload"])
    found = search(service, ns, case)["facts"]
    return source, facts.model_dump(mode="json")["facts"], found, model


@pytest.mark.parametrize("name", sorted(ENGLISH))
def test_english_summary_keeps_spanish_quotes_and_answers_english_question(graph, name):
    store, ns = graph
    case = CASES[name]
    source, facts, found, model = extract(store, ns, case, ENGLISH[name])
    # The rule reaches the model on the session-records path the Claude feed uses.
    assert "fact summary in English" in model.instructions
    checks = check_case(case, source, facts, found)
    assert [c for c in checks if not c["passed"]] == []
    content = {m.id: m.content for m in source.messages}
    for f in facts:
        for e in f["evidence"]:
            assert e["quote"] in content[e["message_id"]]


def test_eventual_decision_is_the_answer_and_its_predecessors_are_history(graph):
    store, ns = graph
    case = CASES["es-later-correction"]
    _, _, found, _ = extract(store, ns, case, ENGLISH["es-later-correction"])
    assert found[0]["lane"] == "planned" and "NATS JetStream" in found[0]["text"]
    assert not [f for f in found if f["lane"] in {"current", "planned"} and "RabbitMQ" in f["text"]]
    assert all(
        "final decision is NATS" in f["text"]
        for f in found
        if f["lane"] in {"current", "planned"} and "Kafka" in f["text"]
    )


def test_spanish_summaries_fail_the_same_checks(graph):
    """Old behaviour: the summary repeats the source language. Quotes are still
    valid, but when keys and slot carry none of the question's words, the English
    question no longer reaches the decision."""
    store, ns = graph
    case = CASES["es-later-correction"]
    source, facts, found, _ = extract(store, ns, case, spanish(ENGLISH["es-later-correction"]))
    failed = {c["check"] for c in check_case(case, source, facts, found) if not c["passed"]}
    assert {
        "every summary is English",
        "English question finds the eventual answer first",
    } <= failed
    assert "every quote is verbatim source text" not in failed


@pytest.mark.parametrize(
    "quote",
    [
        "the final decision for Faro is NATS JetStream",  # translated
        "La decisión final para Faro es NATS Streaming",  # changed name
        "al final vamos a usar Kafka",  # negation dropped
    ],
)
def test_translated_or_altered_quotes_are_rejected(quote):
    source = transcript(CASES["es-later-correction"], "english")
    bad = Extraction.model_validate(ENGLISH["es-later-correction"])
    bad.facts[2].evidence[1].quote = quote
    with pytest.raises(ValueError, match="exact substring"):
        bad.validate_evidence(source)


def test_changed_number_in_a_quote_is_rejected():
    bad = Extraction.model_validate(ENGLISH["es-technical-ids"])
    bad.facts[0].evidence[0].quote = "se resolvió subiendo max_connections de 200 a 600"
    with pytest.raises(ValueError, match="exact substring"):
        bad.validate_evidence(transcript(CASES["es-technical-ids"], "english"))


def test_rule_is_in_every_extraction_prompt():
    assert "fact summary in English" in EXTRACTION_INSTRUCTIONS
