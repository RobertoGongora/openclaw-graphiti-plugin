import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from graph_memory.feeds import worker_tick
from graph_memory.journal import Journal
from graph_memory.llm import CodexLLM
from graph_memory.models import Extraction, Transcript
from graph_memory.retry import MAX_FEEDBACK_BYTES, feedback, restored_feedback
from graph_memory.service import MemoryService
from graph_memory.store import GraphStore
from tests.helpers import MYSQL, PROJECT


def candidate(quote="Atlas uses MySQL."):
    return Extraction(
        entities=[PROJECT, MYSQL],
        facts=[
            {
                "subject": PROJECT["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "summary": "Atlas uses MySQL.",
                "valid_at": "2026-09-15T10:00:00Z",
                "evidence": [{"message_id": "m1", "quote": quote}],
            }
        ],
    )


@pytest.mark.parametrize("failure", ["evidence", "schema"])
def test_feedback_survives_worker_restart_and_clears_only_on_success(graph, failure):
    store, ns = graph
    transcript = Transcript(
        namespace=ns,
        source_id="source",
        session_id="session",
        messages=[
            {
                "id": "m1",
                "role": "user",
                "content": "Atlas uses MySQL.",
                "timestamp": "2026-09-15T10:00:00Z",
            }
        ],
    )
    eid = store.stage(transcript)["episode_id"]
    invalid = candidate("Invented private quote")
    rejected = invalid.model_dump(mode="json")
    if failure == "schema":
        rejected["entities"] = []

    class Invalid:
        def generate(self, instructions, payload, output):
            if failure == "schema":
                try:
                    return Extraction.model_validate(rejected)
                except ValidationError as exc:
                    exc.memory_rejected_candidate = json.dumps(rejected)
                    raise
            return invalid

    first = worker_tick(MemoryService(store, Invalid()), ns)
    assert first["receipts"][0]["status"] == "failed"
    saved = store.episode(ns, eid)["retry_feedback"]
    detail = json.loads(saved)
    if failure == "evidence":
        assert detail["diagnostic"]["code"] == "evidence_quote_mismatch"
        assert detail["diagnostic"]["location"] == ["facts", 0, "evidence", 0, "quote"]
    else:
        assert detail["diagnostic"]["issues"][0]["code"] == "undeclared_endpoint"
    assert detail["rejected_candidate"] == rejected
    assert "Invented private quote" not in json.dumps(first)
    assert store.recall(ns, "Atlas")["current"] == []
    assert [e["kind"] for e in Journal(store).events(ns)] == ["baseline", "source_saved"]
    assert Journal(store).verify(ns)["verified"]
    assert "retry_feedback" not in json.dumps(Journal(store).events(ns))
    changed = transcript.model_copy(update={"source_id": "different-source"})
    changed_id = store.stage(changed)["episode_id"]
    assert store.episode(ns, changed_id).get("retry_feedback") is None
    # Leave the unrelated source out of this retry-only worker test.
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {id:$id}) SET e.retry_after=9999999999", id=changed_id
        ).consume()
    )

    # A different connection/service stands in for a restarted Docker worker.
    import os

    restarted = GraphStore(
        os.environ["MEMORY_TEST_NEO4J_URI"], password=os.environ.get("MEMORY_TEST_NEO4J_PASSWORD")
    )
    calls = []

    class Repair:
        def generate(self, instructions, payload, output):
            calls.append(payload)
            assert payload["previous_rejection"] == detail
            assert payload["transcript"] == transcript.model_dump(mode="json")
            return candidate()

    def due():
        store.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {id:$id}) SET e.retry_after=0", id=eid
            ).consume()
        )

    class Unavailable:
        def generate(self, *args):
            raise RuntimeError("Codex extraction timed out; durable input can be retried")

    try:
        due()
        worker_tick(MemoryService(restarted, Unavailable()), ns)
        assert store.episode(ns, eid)["retry_feedback"] == saved
        due()
        assert (
            worker_tick(MemoryService(restarted, Repair()), ns)["receipts"][0]["status"]
            == "complete"
        )
    finally:
        restarted.close()
    assert len(calls) == 1
    assert store.episode(ns, eid).get("retry_feedback") is None
    assert len(store.recall(ns, "Atlas")["current"]) == 1
    assert Journal(store).verify(ns)["verified"]
    assert "retry_feedback" not in json.dumps(Journal(store).events(ns))


@pytest.mark.parametrize("max_attempts", [1, 2])
def test_schema_rejection_retains_final_candidate_and_error(monkeypatch, max_attempts):
    raw = candidate().model_dump(mode="json")
    raw["entities"] = []  # Both relationship endpoints are undeclared.
    seen = []

    def run(command, **kwargs):
        seen.append(kwargs["input"])
        from pathlib import Path

        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(raw))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("graph_memory.llm.subprocess.run", run)
    with pytest.raises(ValidationError) as caught:
        CodexLLM(max_attempts=max_attempts).generate(
            "Extract", {"transcript": "source"}, Extraction
        )
    assert len(seen) == max_attempts
    result = feedback(caught.value, "model_output", "engine")
    assert result["rejected_candidate"] == raw
    assert result["diagnostic"]["issues"][0]["code"] == "undeclared_endpoint"
    assert restored_feedback(json.dumps(result), "engine") == result


def test_feedback_bounds_and_engine_isolation():
    error = ValueError("Evidence must quote an exact substring of its source message")
    result = feedback(error, "evidence_validation", "old", {"quote": "x" * 100_000})
    assert result["candidate_omitted"] == "size_limit"
    assert "rejected_candidate" not in result
    assert len(json.dumps(result).encode()) <= MAX_FEEDBACK_BYTES
    assert restored_feedback(json.dumps(result), "new") is None
    for raw in (None, "{", "[]", "x" * (MAX_FEEDBACK_BYTES + 1)):
        assert restored_feedback(raw, "old") is None
    # The serialization size is bounded even if non-ASCII JSON expands on storage.
    error.memory_rejected_candidate = json.dumps({"quote": "é" * 25_000}, ensure_ascii=False)
    result = feedback(error, "evidence_validation", "old")
    assert len(json.dumps(result).encode()) <= MAX_FEEDBACK_BYTES
    assert feedback(RuntimeError("secret-token"), "model_output", "old") is None
    assert feedback(error, "commit", "old") is None
