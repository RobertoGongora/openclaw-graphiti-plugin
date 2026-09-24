import pytest

from graph_memory import dream_review as review
from graph_memory.models import ClaimReview, DreamOutput
from graph_memory.service import MemoryService


def source_packet():
    graph = {
        "uncertain": [
            {
                "id": "f",
                "summary": "Atlas uses MySQL.",
                "subject": "project:atlas",
                "relation": "uses_database",
                "target": "database:mysql",
                "status": "uncertain",
                "valid_at": None,
            }
        ]
    }
    messages = [
        {
            "id": "claim",
            "role": "assistant",
            "source_type": "assistant_report",
            "content": "Atlas uses MySQL.",
        },
        {
            "id": "user",
            "role": "user",
            "source_type": "user_assertion",
            "content": "Atlas uses MySQL.",
            "timestamp": "2026-09-01T00:00:00Z",
        },
        {
            "id": "call",
            "role": "assistant",
            "source_type": "tool_call",
            "tool_name": "Bash",
            "call_id": "c",
            "content": "inspect Atlas database",
        },
        {
            "id": "tool",
            "role": "tool",
            "source_type": "tool_result",
            "tool_name": "Bash",
            "call_id": "c",
            "content": "Atlas database: MySQL",
        },
        {
            "id": "memory",
            "role": "tool",
            "source_type": "memory_read",
            "content": "Atlas uses MySQL.",
        },
        {
            "id": "failed",
            "role": "tool",
            "source_type": "tool_result",
            "tool_failed": True,
            "content": "Atlas uses MySQL.",
        },
        {"id": "note", "role": "note", "source_type": "context", "content": "Atlas uses MySQL."},
    ]
    transcript = {
        "namespace": "test",
        "session_id": "session",
        "source_id": "source",
        "source_format": "session-records-v1",
        "messages": messages,
    }
    return graph, transcript


def test_review_sources_exclude_reports_memory_reads_failed_tools_and_context():
    graph, transcript = source_packet()
    claims = review.packet(graph, [transcript])
    assert len(claims) == 1
    assert {s["source_type"] for s in claims[0]["sources"]} == {"user_assertion", "tool_result"}
    assert len(claims[0]["sources"]) == 2
    assert all(s["id"] for s in claims[0]["sources"])
    assert (
        next(s for s in claims[0]["sources"] if s["role"] == "tool")["tool_call"]
        == "inspect Atlas database"
    )


def test_split_tool_call_keeps_first_fragment_and_records_missing_context():
    graph, transcript = source_packet()
    call = next(m for m in transcript["messages"] if m["id"] == "call")
    call["gaps"] = ["record_split_into_chunks"]
    transcript["messages"].append(
        {**call, "id": "call-tail", "content": "end of argument", "gaps": []}
    )
    sources = review.packet(graph, [transcript])[0]["sources"]
    tool = next(s for s in sources if s["role"] == "tool")
    assert tool["tool_call"] == "inspect Atlas database"
    assert tool["tool_call_truncated"]
    assert tool["tool_call_gaps"] == ["record_split_into_chunks"]


def test_review_requires_all_claims_and_eligible_citations_without_promoting_them():
    graph, transcript = source_packet()
    claims = review.packet(graph, [transcript])
    good = ClaimReview(
        fact_id="f",
        verdict="supported",
        evidence_ids=[claims[0]["sources"][0]["id"]],
        reason="Source asserts this",
    )
    review.validate(claims, [good])
    assert graph["uncertain"][0]["status"] == "uncertain"
    for bad in (
        [],
        [good, good],
        [good.model_copy(update={"fact_id": "other"})],
        [good.model_copy(update={"evidence_ids": ["invented"]})],
        [good.model_copy(update={"evidence_ids": []})],
    ):
        with pytest.raises(ValueError):
            review.validate(claims, bad)
    review.validate(
        claims, [good.model_copy(update={"verdict": "insufficient", "evidence_ids": []})]
    )


def test_dream_rejects_uncertain_review_as_insight_support_and_retries_bad_reviews():
    graph, transcript = source_packet()
    graph.update(current=[], events=[])
    snapshot = {
        "graph": graph,
        "transcripts": [transcript],
        "claim_review_inputs": review.packet(graph, [transcript]),
    }

    class Model:
        calls = 0

        def generate(self, instructions, payload, schema):
            self.calls += 1
            return DreamOutput(
                insights=[],
                observations=[],
                claim_reviews=[
                    ClaimReview(
                        fact_id="f", verdict="supported", evidence_ids=["invented"], reason="bad"
                    )
                ],
            )

    model = Model()
    with pytest.raises(ValueError, match="eligible"):
        MemoryService(None, model).dream_generate(snapshot)
    assert model.calls == 2


def test_review_abstains_when_only_unsourced_context_is_available():
    graph, transcript = source_packet()
    transcript["source_format"] = "direct-mcp-v1"
    claims = review.packet(graph, [transcript])
    assert not claims[0]["sources"]
    review.validate(
        claims,
        [
            ClaimReview(
                fact_id="f", verdict="insufficient", evidence_ids=[], reason="No original evidence"
            )
        ],
    )


@pytest.mark.integration
def test_completed_dream_reviews_never_promote_or_confirm_facts(graph):
    from graph_memory.models import DreamCreate, DreamRequest

    from .test_reference_journal import extraction, transcript

    store, ns = graph
    t = transcript(ns, "review")
    receipt = store.stage(t)
    candidate = extraction()
    candidate.facts[0].status = "uncertain"
    candidate.facts[0].valid_at = None
    store.commit(ns, receipt["episode_id"], candidate)

    class Model:
        def generate(self, instructions, payload, schema):
            item = payload["claim_review_inputs"][0]
            return DreamOutput(
                insights=[],
                observations=[],
                claim_reviews=[
                    ClaimReview(
                        fact_id=item["fact"]["id"],
                        verdict="supported",
                        evidence_ids=[item["sources"][0]["id"]],
                        reason="Original user asserted this",
                    )
                ],
            )

    service = MemoryService(store, Model())
    before = store.recall(ns, "Atlas")
    created = service.dream_create(
        DreamCreate(
            namespace=ns, query="Atlas", episode_ids=[receipt["episode_id"]], review_uncertain=True
        )
    )
    request = DreamRequest(namespace=ns, dream_id=created["dream_id"])
    assert service.dream_run(request)["output"]["claim_reviews"][0]["verdict"] == "supported"
    applied = service.dream_apply(request)
    assert applied["claim_reviews"] == 1 and applied["reviewed_facts_unchanged"]
    after = store.recall(ns, "Atlas")
    assert after["uncertain"] == before["uncertain"]
    assert not after["current"] and not after["insights"]
    assert not after["uncertain"][0].get("confirmed_at")
