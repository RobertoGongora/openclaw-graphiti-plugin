import json

import pytest

from graph_memory.claim_support import SupportReview, SupportVerifier
from graph_memory.llm import ModelUnavailable
from graph_memory.models import EpisodeRequest, Extraction
from graph_memory.service import MemoryService
from tests.test_recall_provenance import message, transcript
from tests.test_session_sources import extraction


class SupportModel:
    def __init__(self, verdict="supported"):
        self.verdict = verdict
        self.payloads = []

    def generate(self, instructions, payload, schema):
        assert schema is SupportReview
        self.payloads.append(payload)
        return SupportReview(
            decisions=[
                {
                    "index": c["index"],
                    "verdict": self.verdict,
                    "evidence_ids": [e["id"] for e in c["fresh_evidence"]]
                    if self.verdict == "supported"
                    else [],
                    "reason": "Test decision based on the supplied source.",
                }
                for c in payload["claims"]
            ]
        )


def pair(tool="Atlas uses MySQL."):
    t = transcript(
        [
            message("read", "memory_read", "OLD MEMORY DO NOT SEND TO CHECKER"),
            message("fresh", "tool_result", tool, tool_name="database_status"),
            message("report", "assistant_report", "Atlas uses MySQL."),
        ],
        focus_message_ids=["report"],
    )
    x = extraction(
        "report",
        status="active",
        valid_at="2026-09-01T00:00:00Z",
        validation_evidence=[{"message_id": "fresh", "quote": tool}],
    )
    return t, x


def test_unrelated_evidence_is_checked_in_isolation_and_rejected_even_if_uncertain():
    t, x = pair("Disk usage is 20%.")
    model = SupportModel("unsupported")
    checker = SupportVerifier(model)
    for status in ("active", "uncertain"):
        x.facts[0].status = status
        with pytest.raises(ValueError, match="Independent support check"):
            x.validate_evidence(t, support=checker)
    encoded = json.dumps(model.payloads)
    assert "OLD MEMORY" not in encoded
    assert "Disk usage is 20%" in encoded
    assert model.payloads[0]["claims"][0]["claim"]["relation"] == "uses_database"
    with pytest.raises(ValueError, match="independent support checker"):
        x.validate_evidence(t)


def test_approval_binds_entire_candidate_and_source_and_never_accepts_json_attestations():
    t, x = pair()
    model = SupportModel()
    checker = SupportVerifier(model)
    x.validate_evidence(t, support=checker)
    x.validate_evidence(t, support=checker, allow_support_model=False)
    assert checker.calls == 1
    x.facts[0].summary = "Atlas uses MySQL in production."
    with pytest.raises(ValueError, match="current independent support check"):
        x.validate_evidence(t, support=checker, allow_support_model=False)
    x.facts[0].summary = "Atlas uses MySQL."
    t.messages[1].content += " This is a mock, not production."
    with pytest.raises(ValueError, match="current independent support check"):
        x.validate_evidence(t, support=checker, allow_support_model=False)
    with pytest.raises(ModelUnavailable):
        x.validate_evidence(t, support=SupportVerifier())
    raw = x.model_dump(mode="json")
    raw["support_approved"] = True
    with pytest.raises(ValueError):
        Extraction.model_validate(raw)


@pytest.mark.parametrize("failure", ["missing", "duplicate", "foreign_id", "unclear"])
def test_malformed_or_unclear_support_never_approves(failure):
    class Model(SupportModel):
        def generate(self, instructions, payload, schema):
            result = super().generate(instructions, payload, schema)
            if failure == "missing":
                result.decisions = []
            elif failure == "duplicate":
                result.decisions *= 2
            elif failure == "foreign_id":
                result.decisions[0].evidence_ids = ["read"]
            else:
                result.decisions[0].verdict = "unclear"
            return result

    t, x = pair()
    with pytest.raises(ValueError):
        x.validate_evidence(t, support=SupportVerifier(Model()))


def test_user_correction_does_not_need_a_model_check():
    t, x = pair()
    t.messages[-1].role = "user"
    t.messages[-1].source_type = "user_assertion"
    t.memory_origins = {}
    x.validate_evidence(t)


def test_commit_requires_support_outside_transaction_and_rechecks_changed_candidate(graph):
    store, ns = graph
    t, x = pair()
    t.namespace = ns
    eid = store.stage(t)["episode_id"]
    with pytest.raises(ModelUnavailable):
        store.commit(ns, eid, x, model_info={"support_approved": True})

    class Model(SupportModel):
        def generate(self, *args):
            from graph_memory.store import _held

            assert not getattr(_held, "locks", set())
            return super().generate(*args)

    model = Model("unsupported")
    MemoryService(store, model)
    with pytest.raises(ValueError, match="Independent support check"):
        store.commit(ns, eid, x)
    assert store.episode(ns, eid)["status"] != "complete"
    model.verdict = "supported"
    receipt = store.commit(ns, eid, x)
    assert receipt["status"] == "complete"
    assert len(model.payloads) == 2  # No model calls from inside the commit transaction.
    assert store.commit(ns, eid, x)["replayed"]


def test_extraction_retries_without_unsupported_echo_and_cached_candidate_is_rechecked(graph):
    store, ns = graph
    t, x = pair("Disk usage is 20%.")
    t.namespace = ns
    eid = store.stage(t)["episode_id"]

    class Model(SupportModel):
        model, effort = "test", "low"

        def __init__(self):
            super().__init__("unsupported")
            self.extractions = 0

        def generate(self, instructions, payload, schema):
            if schema is SupportReview:
                return super().generate(instructions, payload, schema)
            self.extractions += 1
            return x if self.extractions == 1 else Extraction(entities=[], facts=[])

    model = Model()
    service = MemoryService(store, model)
    info = {"provider": type(model).__name__, "model": model.model, "effort": model.effort}
    store.cache_extraction(ns, eid, x, info)
    with pytest.raises(ValueError, match="Independent support check"):
        service.extract(EpisodeRequest(namespace=ns, episode_id=eid))
    assert model.extractions == 0
    assert store.episode(ns, eid)["status"] != "complete"
    # Rejected cached work is cleared; retry gets the normal correction pass.
    assert not store.episode(ns, eid).get("cached_extraction")
    done = service.extract(EpisodeRequest(namespace=ns, episode_id=eid))
    assert done["model_calls"] == 3  # extraction, support check, corrected extraction
    assert store.episode(ns, eid)["fact_count"] == 0


def test_revision_rechecks_support_after_restart_before_opening_promotion(graph):
    from graph_memory.revisions import Revisions
    from graph_memory.store import _held

    store, ns = graph
    t, x = pair()
    t.namespace = ns
    eid = store.stage(t)["episode_id"]

    class Model(SupportModel):
        model, effort = "test", "low"

        def generate(self, instructions, payload, schema):
            assert not getattr(_held, "locks", set())
            if schema is SupportReview:
                return super().generate(instructions, payload, schema)
            return x.model_copy(deep=True)

    model = Model()
    revisions = Revisions(MemoryService(store, model))
    store.commit(ns, eid, x)
    revision = revisions.create(ns, [eid])
    revisions.build(ns, revision["revision_id"])
    before = len(model.payloads)
    store.support_verifier = SupportVerifier(model)  # Simulate a restarted worker.
    revisions.diff(ns, revision["revision_id"])
    assert len(model.payloads) == before + 1
    model.verdict = "unsupported"
    store.support_verifier = SupportVerifier(model)
    with pytest.raises(ValueError, match="Independent support check"):
        revisions.diff(ns, revision["revision_id"])


def test_checker_outage_preserves_episode_and_cached_work(graph):
    store, ns = graph
    t, x = pair()
    t.namespace = ns
    eid = store.stage(t)["episode_id"]

    class Outage(SupportModel):
        model, effort = "test", "low"

        def generate(self, *args):
            raise ModelUnavailable(
                "Model endpoint unreachable; durable input can be retried", "network"
            )

    model = Outage()
    service = MemoryService(store, model)
    info = {"provider": type(model).__name__, "model": model.model, "effort": model.effort}
    store.cache_extraction(ns, eid, x, info)
    before = store.episode(ns, eid)
    with pytest.raises(ModelUnavailable):
        service.extract(EpisodeRequest(namespace=ns, episode_id=eid))
    after = store.episode(ns, eid)
    assert after["status"] == before["status"]
    assert after["cached_extraction"] == before["cached_extraction"]


def test_completed_repaired_retry_is_idempotent_without_approval_after_restart(graph):
    store, ns = graph
    t, x = pair()
    t.namespace = ns
    x.facts[0].evidence[0].quote = "Atlas uses **MySQL**."
    original = x.model_copy(deep=True)
    MemoryService(store, SupportModel())
    eid = store.stage(t)["episode_id"]
    store.commit(ns, eid, x)
    store.support_verifier = SupportVerifier()  # No model or previous process approvals.
    assert store.commit(ns, eid, original)["replayed"]
    changed = original.model_copy(deep=True)
    changed.facts[0].summary = "Different claim."
    with pytest.raises(ValueError, match="different extraction"):
        store.commit(ns, eid, changed)
