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
    assert t.messages[0].source_type == "context"
    assert "status=uncertain" in extraction_instructions(t)
    assert not t.can_yield_facts()
    for state in ("active", "uncertain"):
        with pytest.raises(ValueError, match="verified source"):
            extraction("m", status=state).validate_evidence(t)
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
    assert receipt["context_only_message_ids"] == ["m"]
    assert receipt["available_for_recall"] is False
    assert receipt["guidance"]
    for state in ("active", "uncertain"):
        with pytest.raises(ValueError, match="verified source"):
            store.commit(ns, receipt["episode_id"], extraction("m", status=state))
    from graph_memory.models import EpisodeRequest

    done = MemoryService(store).extract(
        EpisodeRequest(namespace=ns, episode_id=receipt["episode_id"])
    )
    assert done["model_calls"] == 0
    assert store.episode(ns, receipt["episode_id"])["fact_count"] == 0
    completed = handler(r)
    assert completed["status"] == "complete"
    assert completed["available_for_recall"] is False


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
    assert "context_only_message_ids" not in receipt
    assert "guidance" not in receipt
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


def test_legacy_pending_unsourced_direct_claim_cannot_bypass_new_policy(graph):
    from graph_memory.models import EpisodeRequest
    from graph_memory.service import MemoryService

    store, ns = graph
    # A durable pending payload staged by the previous engine still says assistant_report.
    old = request().transcript.model_copy(
        update={"namespace": ns, "source_format": "direct-mcp-v1", "verified_source_refs": {}}
    )
    old.messages[0].role = "assistant"
    old.messages[0].source_type = "assistant_report"
    assert not old.can_yield_facts()
    receipt = store.stage(old)
    with pytest.raises(ValueError, match="verified source"):
        store.commit(ns, receipt["episode_id"], extraction("m", status="uncertain"))
    MemoryService(store).extract(EpisodeRequest(namespace=ns, episode_id=receipt["episode_id"]))
    assert store.episode(ns, receipt["episode_id"])["fact_count"] == 0


def test_mixed_direct_write_cannot_launder_an_unsourced_claim():
    source = dict(
        id="stored",
        namespace="test",
        content="Atlas uses MySQL.",
        role="user",
        source_type="user_assertion",
    )
    r = request(sources={"m": "stored"})
    r.transcript.messages.append(r.transcript.messages[0].model_copy(update={"id": "echo"}))
    t = prepare(Store([source]), r)
    assert t.can_yield_facts()
    extraction("m").validate_evidence(t)
    with pytest.raises(ValueError, match="verified source"):
        extraction("echo", status="uncertain").validate_evidence(t)


@pytest.mark.parametrize("kind,role", [("tool_result", "tool"), ("user_assertion", "user")])
def test_verified_excerpt_preserves_original_context_for_support_check(kind, role):
    from graph_memory.claim_support import SupportVerifier
    from graph_memory.models import Evidence
    from tests.test_claim_support import SupportModel

    source = dict(
        id="stored",
        namespace="test",
        content="This is mock documentation, not actual state. Example: Atlas uses MySQL.",
        role=role,
        source_type=kind,
    )
    report = dict(
        id="report-source",
        namespace="test",
        content="Atlas uses MySQL.",
        role="assistant",
        source_type="assistant_report",
        memory_read_refs=["r" * 64],
    )
    r = request(sources={"m": "stored", "report": "report-source"})
    r.transcript.messages.append(r.transcript.messages[0].model_copy(update={"id": "report"}))
    t = prepare(Store([source, report]), r)
    assert t.messages[0].content == source["content"]
    x = extraction("report", status="uncertain")
    if kind == "tool_result":
        x.facts[0].validation_evidence = [Evidence(message_id="m", quote="Atlas uses MySQL.")]
    else:
        x.facts[0].evidence.append(Evidence(message_id="m", quote="Atlas uses MySQL."))
    model = SupportModel("unsupported")
    with pytest.raises(ValueError, match="Independent support check"):
        x.validate_evidence(t, support=SupportVerifier(model))
    evidence = model.payloads[0]["claims"][0]["fresh_evidence"][0]
    assert evidence["content"] == source["content"]
    assert evidence["truncated"] is False


def test_verified_source_expansion_enforces_transcript_size_limit():
    sources = [
        dict(
            id=str(i),
            namespace="test",
            content="x" * 110000 + "Atlas uses MySQL.",
            role="user",
            source_type="user_assertion",
        )
        for i in range(5)
    ]
    r = request(sources={"m": "0", **{f"m{i}": str(i) for i in range(1, 5)}})
    r.transcript.messages.extend(
        r.transcript.messages[0].model_copy(update={"id": f"m{i}"}) for i in range(1, 5)
    )
    with pytest.raises(ValueError, match="500000"):
        prepare(Store(sources), r)
