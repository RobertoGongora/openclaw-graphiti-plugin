import pytest

from graph_memory.direct_ingest import prepare
from graph_memory.llm import extraction_instructions
from graph_memory.models import Remember
from tests.test_session_sources import extraction


class Store:
    def __init__(self, sources=()):
        self.sources = sources

    def transaction(self, run):
        return run(self)

    def run(self, query, **params):
        self.params = params
        return self

    def data(self):
        return [
            {"source": s}
            for s in self.sources
            if s["namespace"] == self.params["ns"] and s["id"] in self.params["ids"]
        ]


def request(**updates):
    value = dict(
        transcript=dict(
            namespace="test",
            source_id="direct",
            session_id="s",
            source_format="session-records-v1",
            verified_source_refs={"m": "forged"},
            messages=[
                dict(
                    id="m",
                    role="user",
                    source_type="user_assertion",
                    content="Atlas uses MySQL.",
                    timestamp="2026-09-16T10:00:00Z",
                )
            ],
        )
    )
    value.update(updates)
    return Remember.model_validate(value)


def test_unsourced_direct_write_cannot_self_certify():
    t = prepare(Store(), request())
    assert t.source_format == "direct-mcp-v1"
    assert t.verified_source_refs == {}
    assert t.messages[0].source_type == "assistant_report"
    assert "status=uncertain" in extraction_instructions(t)
    with pytest.raises(ValueError, match="unvalidated assistant"):
        extraction("m").validate_evidence(t)
    extraction("m", status="uncertain").validate_evidence(t)
    assert prepare(Store(), request()).model_dump() == t.model_dump()


def test_verified_source_inherits_authority_not_callers_role():
    source = dict(
        id="stored",
        namespace="test",
        content="Atlas uses MySQL.",
        role="assistant",
        source_type="assistant_report",
        timestamp="2026-09-15T10:00:00Z",
    )
    store = Store([source])
    t = prepare(store, request(sources={"m": "stored"}))
    assert t.messages[0].role == "assistant"
    assert t.messages[0].timestamp.day == 15
    with pytest.raises(ValueError, match="unvalidated assistant"):
        extraction("m").validate_evidence(t)
    source.update(role="user", source_type="user_assertion")
    t = prepare(store, request(sources={"m": "stored"}))
    extraction("m").validate_evidence(t)
    assert t.verified_source_refs == {"m": "stored"}


@pytest.mark.parametrize(
    "change", [{"namespace": "other"}, {"content": "Unrelated text"}, {"id": "missing"}]
)
def test_wrong_or_cross_namespace_source_rejected(change):
    source = dict(
        id="stored",
        namespace="test",
        content="Atlas uses MySQL.",
        role="user",
        source_type="user_assertion",
    )
    source.update(change)
    with pytest.raises(ValueError, match="Source reference"):
        prepare(Store([source]), request(sources={"m": "stored"}))


def test_unknown_submitted_message_reference_rejected():
    with pytest.raises(ValueError, match="submitted message"):
        prepare(Store(), request(sources={"unknown": "stored"}))


def test_public_ingest_stages_normalized_claim_and_enforces_it_at_commit(graph):
    from graph_memory.service import MemoryService

    store, ns = graph
    r = request()
    r.transcript.namespace = ns
    schema, handler, _ = MemoryService(store).session_tools()["memory_ingest"]
    assert "sources" in schema.model_fields
    receipt = handler(r)
    assert receipt["processing"] == "queued_for_worker"
    assert handler(r)["episode_id"] == receipt["episode_id"]
    with pytest.raises(ValueError, match="unvalidated assistant"):
        store.commit(ns, receipt["episode_id"], extraction("m"))
    store.commit(ns, receipt["episode_id"], extraction("m", status="uncertain"))
    assert handler(r)["status"] == "complete"


@pytest.mark.parametrize("kind", ["memory_read", "memory_write", "tool_result"])
def test_context_and_tool_sources_cannot_originate_claims(kind):
    source = dict(
        id="stored", namespace="test", content="Atlas uses MySQL.", role="tool", source_type=kind
    )
    t = prepare(Store([source]), request(sources={"m": "stored"}))
    with pytest.raises(ValueError, match="conversational claim"):
        extraction("m").validate_evidence(t)


def test_stored_source_roundtrip_through_evidence_and_direct_write(graph):
    from graph_memory.models import Transcript
    from graph_memory.retrieval import EvidenceRequest, evidence
    from graph_memory.service import MemoryService

    store, ns = graph
    source = Transcript.model_validate(request().transcript.model_dump())
    source.namespace = ns
    source.verified_source_refs = {}
    receipt = store.stage(source)
    committed = store.commit(ns, receipt["episode_id"], extraction("m"))
    result = evidence(store, EvidenceRequest(namespace=ns, fact_ids=committed["fact_ids"]))
    ref = result["facts"][0]["claims"][0]["source_message_id"]
    assert ref
    r = request(sources={"m": ref})
    r.transcript.namespace = ns
    r.transcript.source_id = "sourced-direct"
    receipt = MemoryService(store).remember(r)
    committed = store.commit(ns, receipt["episode_id"], extraction("m"))
    result = evidence(store, EvidenceRequest(namespace=ns, fact_ids=committed["fact_ids"]))
    assert result["facts"][0]["claims"][0]["source_message_id"] == ref
    # The verified source's time, inherited from the stored message, orders the report.
    assert result["facts"][0]["fact"]["reported_at"] == "2026-09-16T10:00:00+00:00"


def test_sourced_report_keeps_its_memory_origin_and_cannot_repeat_the_memory():
    source = dict(
        id="stored",
        namespace="test",
        content="Atlas uses MySQL.",
        role="assistant",
        source_type="assistant_report",
        timestamp="2026-09-15T10:00:00Z",
        memory_read_refs=["b" * 64],
        recalled_fact_ids=["c" * 64],
    )
    t = prepare(Store([source]), request(sources={"m": "stored"}))
    assert t.memory_origins["m"].result_ids == ["b" * 64]
    assert t.memory_origins["m"].fact_ids == ["c" * 64]
    assert not t.can_yield_facts()
    with pytest.raises(ValueError, match="memory-derived report"):
        extraction("m", status="uncertain").validate_evidence(t)
    assert t.model_dump(mode="json")["memory_origins"]["m"]["result_ids"] == ["b" * 64]


def test_unsourced_direct_message_time_does_not_order_reports(graph):
    from datetime import UTC, datetime

    from graph_memory.retrieval import EvidenceRequest, evidence
    from graph_memory.service import MemoryService

    store, ns = graph
    r = request()
    r.transcript.namespace = ns
    r.transcript.messages[0].timestamp = datetime(2099, 1, 1, tzinfo=UTC)
    receipt = MemoryService(store).remember(r)
    committed = store.commit(ns, receipt["episode_id"], extraction("m", status="uncertain"))
    result = evidence(store, EvidenceRequest(namespace=ns, fact_ids=committed["fact_ids"]))
    # A caller may date its own unverified claim; that date cannot rank it first.
    assert result["facts"][0]["fact"].get("reported_at") is None
