"""English summaries over Spanish, mixed and English sources, through the real engine.

The model here is a fixture: it returns what the instructions ask for, so these
tests show what the engine keeps and serves, not what a model writes. The model's
own behaviour is measured by evals/english_summaries.py."""

from copy import deepcopy

import pytest

from evals.english_summaries import check_case, load_cases, search, transcript
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
        "entities": [
            FARO,
            {
                "key": "topic:checkout-v2",
                "name": "checkout_v2",
                "kind": "topic",
                "aliases": ["CHK-V2"],
            },
        ],
        "facts": [
            fact(
                "project:faro",
                "about",
                "topic:checkout-v2",
                "Proposed, not yet approved: enable the checkout_v2 feature flag (CHK-V2) in "
                "production on November 15 for the Faro rollout.",
                [
                    (
                        "u1",
                        "propuse activar el feature flag checkout_v2 (alias CHK-V2) en producción",
                    )
                ],
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
    "en-control": {
        "entities": [
            {"key": "project:atlas", "name": "Atlas", "kind": "project"},
            {"key": "database:mysql", "name": "MySQL", "kind": "database"},
            {"key": "database:postgresql", "name": "PostgreSQL", "kind": "database"},
        ],
        "facts": [
            fact(
                "project:atlas",
                "uses_database",
                "database:mysql",
                "Atlas's reports API currently runs on MySQL 8.0.",
                [("u1", "Atlas will move the reports API from MySQL 8.0")],
                slot="reports-primary",
                valid_at="2026-10-09T10:00:00Z",
            ),
            fact(
                "project:atlas",
                "uses_database",
                "database:postgresql",
                "Atlas plans to move the reports API from MySQL 8.0 to PostgreSQL 16 next quarter; "
                "the migration has not started.",
                [
                    ("u1", "move the reports API from MySQL 8.0 to PostgreSQL 16 next quarter"),
                    ("u1", "The migration has not started"),
                ],
                "planned",
                slot="reports-primary",
                valid_at="2026-10-09T10:00:00Z",
            ),
        ],
    },
    "es-asks-for-spanish": {
        "entities": [
            FARO,
            {"key": "database:postgresql", "name": "PostgreSQL", "kind": "database"},
            {"key": "topic:billing-deployment", "name": "Billing deployment", "kind": "topic"},
        ],
        "facts": [
            fact(
                "project:faro",
                "uses_database",
                "database:postgresql",
                "Faro decided that its billing service will use PostgreSQL 16 instead of MySQL; "
                "the migration has not started yet.",
                [
                    ("u1", "el servicio de facturación usará PostgreSQL 16 en lugar de MySQL"),
                    ("u1", "la migración todavía no ha empezado"),
                ],
                "planned",
                valid_at="2026-10-10T09:00:00Z",
            ),
            fact(
                "project:faro",
                "about",
                "topic:billing-deployment",
                "Deployment of Faro's billing service to the k8s-prod-eu1 cluster was cancelled "
                "until January.",
                [("u2", "quedó cancelado hasta enero")],
                valid_at="2026-10-10T09:05:00Z",
            ),
        ],
    },
    "es-other-incident": {
        "entities": [
            {"key": "service:api-gw-eu", "name": "api-gw-eu", "kind": "service"},
            {"key": "issue:sec-0193", "name": "SEC-0193", "kind": "issue"},
        ],
        "facts": [
            fact(
                "service:api-gw-eu",
                "resolved",
                "issue:sec-0193",
                "Incident SEC-0193 on api-gw-eu was resolved by rotating the TLS certificate of "
                "ingress-nginx 1.11.3; there have been no 502 errors since.",
                [
                    ("u1", "quedó resuelto al rotar el certificado TLS de ingress-nginx 1.11.3"),
                    ("u1", "Desde entonces no hay errores 502."),
                ],
                valid_at="2026-10-07T08:10:00Z",
            ),
            # The incident is over: its has_issue state ends, not its resolution.
            fact(
                "service:api-gw-eu",
                "has_issue",
                "issue:sec-0193",
                "Incident SEC-0193 on api-gw-eu is over.",
                [("u1", "El incidente SEC-0193 en api-gw-eu quedó resuelto")],
                "ended",
                valid_at="2026-10-07T08:10:00Z",
            ),
        ],
    },
    "es-cancelled-plan": {
        "entities": [
            {"key": "service:pg-prod-02", "name": "pg-prod-02", "kind": "service"},
            {"key": "event:pg-prod-02-failover-drill", "name": "Failover drill", "kind": "event"},
            {"key": "event:pg-prod-02-alert-review", "name": "Alert review", "kind": "event"},
        ],
        "facts": [
            fact(
                "service:pg-prod-02",
                "occurred",
                "event:pg-prod-02-failover-drill",
                "The pg-prod-02 failover drill planned for Thursday, October 8 was cancelled and "
                "did not take place.",
                [("u2", "Se canceló el simulacro de failover de pg-prod-02; no se hizo.")],
                "ended",
                valid_at="2026-10-08T16:00:00Z",
            ),
            fact(
                "service:pg-prod-02",
                "occurred",
                "event:pg-prod-02-alert-review",
                "On Thursday, October 8, the pg-prod-02 replication alerts were reviewed.",
                [("u2", "Ese jueves revisamos las alertas de replicación de pg-prod-02.")],
                valid_at="2026-10-08T16:00:00Z",
            ),
        ],
    },
}

# What gpt-5.6-terra committed for es-technical-ids at b1ef049, repeat 1
# (.local/english-b1ef049.json): right summary and quote, but the resolution is
# ended, so it is history and the English question finds nothing.
FROZEN_B1EF049 = {
    "entities": [
        {"key": "service:pg-prod-02", "name": "pg-prod-02", "kind": "service", "aliases": []},
        {"key": "issue:inc-4721", "name": "INC-4721", "kind": "issue", "aliases": []},
    ],
    "facts": [
        fact(
            "service:pg-prod-02",
            "resolved",
            "issue:inc-4721",
            "INC-4721 on pg-prod-02 was resolved by increasing max_connections from 200 to 500 "
            "using the faro-api chart v3.14.2; the cluster was not restarted.",
            [
                (
                    "u1",
                    "El incidente INC-4721 en pg-prod-02 se resolvió subiendo max_connections de "
                    "200 a 500 con el chart faro-api v3.14.2. No se reinició el clúster.",
                )
            ],
            "ended",
            slot=None,
            valid_at="2026-10-05T14:30:00Z",
            confidence=0.98,
        )
    ],
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
    committed = Extraction.model_validate_json(store.episode(ns, eid)["extraction_payload"])
    found = search(service, ns, case)["facts"]
    return source, committed.model_dump(mode="json"), found, model


def failures(store, ns, name, extraction):
    case = CASES[name]
    source, committed, found, _ = extract(store, ns, case, extraction)
    checks = check_case(case, source, committed["facts"], found, committed["entities"])
    return {c["check"] for c in checks if not c["passed"]}


@pytest.mark.parametrize("name", sorted(ENGLISH))
def test_fixture_extraction_passes_every_check_of_its_case(graph, name):
    store, ns = graph
    source, committed, found, model = extract(store, ns, CASES[name], ENGLISH[name])
    # The rule reaches the model on the session-records path the Claude feed uses.
    assert "fact summary in English" in model.instructions
    checks = check_case(CASES[name], source, committed["facts"], found, committed["entities"])
    assert [c for c in checks if not c["passed"]] == []


def mutate(name, change):
    extraction = deepcopy(ENGLISH[name])
    change(extraction["facts"])
    return extraction


def summary(index, old, new):
    def change(facts):
        assert old in facts[index]["summary"]
        facts[index]["summary"] = facts[index]["summary"].replace(old, new)

    return change


def deployed(facts):
    facts[2].update(status="active", summary="Faro uses NATS JetStream in production.")


def kafka_last(facts):
    facts[1]["valid_at"] = "2026-10-04T09:00:00Z"


def status(index, value):
    def change(facts):
        facts[index]["status"] = value

    return change


IDS, FIX, MIX = "es-technical-ids", "es-later-correction", "es-en-mixed"
OTHER, DRILL = "es-other-incident", "es-cancelled-plan"
EXACT, RESTART = "identifiers kept exactly in summaries", "cluster restart stays negated"
ANSWER = "English question finds the eventual answer first"


def test_frozen_b1ef049_output_fails_only_retrieval(graph):
    store, ns = graph
    assert failures(store, ns, IDS, FROZEN_B1EF049) == {ANSWER}


def test_frozen_b1ef049_output_with_an_active_resolution_passes(graph):
    """The same output as the corrected rule asks for it: the resolution happened,
    so it is active; an ended has_issue for the incident stays out of the answer."""
    store, ns = graph
    extraction = deepcopy(FROZEN_B1EF049)
    extraction["facts"][0]["status"] = "active"
    over = deepcopy(extraction["facts"][0])
    over.update(relation="has_issue", status="ended", summary="INC-4721 on pg-prod-02 is over.")
    extraction["facts"].append(over)
    assert failures(store, ns, IDS, extraction) == set()


@pytest.mark.parametrize(
    "name,change,expected",
    [
        (IDS, summary(0, "INC-4721", "inc-4721"), {EXACT}),
        (IDS, summary(0, "v3.14.2", "3.14.2"), {EXACT}),
        (IDS, summary(0, "INC-4721", "INC-47210"), {EXACT}),
        (IDS, summary(0, "was not restarted", "was restarted with no downtime"), {RESTART}),
        (IDS, summary(0, "; the cluster was not restarted", ""), {RESTART}),
        (IDS, summary(0, "was not restarted", "was rebooted"), {RESTART}),
        (
            IDS,
            summary(
                0, "the cluster was not restarted", "the cluster had no downtime. It restarted"
            ),
            {RESTART},
        ),
        # An active NATS fact does not supersede the planned Kafka one either.
        (
            FIX,
            deployed,
            {
                "not claimed: NATS JetStream in use as actual state",
                "only the eventual choice is current",
            },
        ),
        (
            FIX,
            summary(2, "it is not yet deployed", "NATS JetStream is deployed and running"),
            {"not claimed: NATS JetStream already deployed"},
        ),
        (
            FIX,
            kafka_last,
            {
                "only the eventual choice is current",
                "English question finds the eventual answer first",
            },
        ),
        (
            MIX,
            summary(
                1,
                "is enabled only in staging",
                "(CHK-V2) is approved and enabled in production and staging",
            ),
            {
                "not claimed: the production rollout is approved",
                "not claimed: enabled in production",
            },
        ),
        (
            MIX,
            summary(0, "enable the checkout_v2", "activar the checkout_v2"),
            {"no Spanish word of the source in a summary"},
        ),
        (MIX, summary(0, "(CHK-V2)", "(chk-v2)"), {"alias CHK-V2 kept exactly"}),
        (
            MIX,
            summary(1, "is enabled only in staging", "is enabled in staging and production"),
            {"not claimed: enabled in production"},
        ),
        # A second sentence, with punctuation inside its clause.
        (
            MIX,
            summary(
                1,
                "is enabled only in staging.",
                "is enabled in staging. It is enabled (now) in production.",
            ),
            {"not claimed: enabled in production"},
        ),
        (
            MIX,
            summary(
                1,
                "feature flag is enabled only in staging",
                "feature flag in production is enabled",
            ),
            {
                "not claimed: production has it enabled",
                "claimed: enabled in staging as actual state",
            },
        ),
        (
            "en-control",
            summary(1, "Atlas plans to move", "Atlas moved"),
            {"not claimed: the migration already happened"},
        ),
        # A resolution marked ended is history: the incident is over, but the
        # resolution happened, and the question about it then finds nothing.
        (OTHER, status(0, "ended"), {ANSWER}),
        (OTHER, status(1, "active"), {"not claimed: SEC-0193 still open"}),
        (DRILL, status(0, "active"), {"not claimed: the cancelled failover drill as done"}),
        (
            DRILL,
            status(0, "planned"),
            {"not claimed: the cancelled failover drill as still planned"},
        ),
        (
            DRILL,
            summary(0, "was cancelled and did not take place", "was held"),
            {"not claimed: the failover drill said to be held"},
        ),
        (DRILL, status(1, "ended"), {ANSWER}),
    ],
)
def test_each_wrong_output_fails_its_own_check(graph, name, change, expected):
    store, ns = graph
    assert failures(store, ns, name, mutate(name, change)) == expected


@pytest.mark.parametrize(
    "name,change",
    [
        (
            FIX,
            summary(
                2, ENGLISH[FIX]["facts"][2]["summary"], "Faro chose NATS JetStream over Kafka."
            ),
        ),
        (
            MIX,
            summary(
                0,
                "Proposed, not yet approved: enable",
                "Faro has a proposed, not-yet-approved plan to activate",
            ),
        ),
        ("es-asks-for-spanish", summary(0, "instead of MySQL", "in favor of MySQL")),
        # The 20e574f wording: the next clause's "production" is not this one's.
        (
            MIX,
            summary(
                1,
                "For Faro, the checkout_v2 feature flag is enabled only in staging.",
                "Faro has feature flag checkout_v2 enabled only in staging; production "
                "activation has not been approved.",
            ),
        ),
        (DRILL, summary(0, "was cancelled and did not take place", "was not held: cancelled")),
    ],
)
def test_truthful_wording_is_not_scored_red(graph, name, change):
    store, ns = graph
    assert failures(store, ns, name, mutate(name, change)) == set()


def test_spanish_summaries_fail_the_same_checks(graph):
    """Old behaviour: the summary repeats the source language. Quotes are still
    valid, but when keys and slot carry none of the question's words, the English
    question no longer reaches the decision."""
    store, ns = graph
    case = CASES["es-later-correction"]
    failed = failures(store, ns, case["id"], spanish(ENGLISH[case["id"]]))
    assert {
        "no Spanish word of the source in a summary",
        "English question finds the eventual answer first",
    } <= failed
    assert "every committed quote is verbatim source text" not in failed
    assert "no Spanish word of the source in a summary" in failures(
        store, ns, "es-asks-for-spanish", spanish(ENGLISH["es-asks-for-spanish"])
    )


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
