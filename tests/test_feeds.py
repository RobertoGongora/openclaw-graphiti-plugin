import json
from datetime import UTC, datetime

import pytest

from graph_memory.feeds import feed, hook, worker_tick
from graph_memory.importers import transcripts
from graph_memory.models import Extraction
from graph_memory.service import MemoryService
from graph_memory.temporal import project
from tests.helpers import MYSQL, PROJECT, ingest


def message(text, stamp="2026-09-15T10:00:00Z"):
    return (
        json.dumps(
            {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": text}}
        )
        + "\n"
    )


def test_feed_atomic_append_replay_partial_and_prefix_rewrite(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    path = tmp_path / "session.jsonl"
    path.write_text(message("Atlas uses MySQL.") + '{"unfinished"')
    first = feed(service, ns, path, "session-one")
    assert first["message_count"] == 1
    assert len(first["receipts"]) == 1
    assert feed(service, ns, path, "session-one")["receipts"] == []
    path.write_text(
        message("Atlas uses MySQL.") + message("Migration to Postgres is only planned.")
    )
    second = feed(service, ns, path, "session-one")
    assert len(second["receipts"]) == 1
    episode = store.episode(ns, second["receipts"][0]["episode_id"])
    payload = json.loads(episode["payload"])
    assert len(payload["messages"]) == 2
    assert payload["focus_message_ids"] == ["line-2-0"]
    assert (
        "memory_recall"
        in hook(service, ns, {"hook_event_name": "UserPromptSubmit"})["hookSpecificOutput"][
            "additionalContext"
        ]
    )
    path.write_text(message("Changed history."))
    with pytest.raises(ValueError, match="prefix changed"):
        feed(service, ns, path, "session-one")
    assert len(store.pending(ns)) == 2


def test_worker_failure_backoff_and_successful_drain(graph, tmp_path):
    store, ns = graph
    path = tmp_path / "session.jsonl"
    path.write_text(message("Atlas uses MySQL."))

    class Failing:
        def generate(self, *args):
            raise ValueError("bad model output")

    service = MemoryService(store, Failing())
    feed(service, ns, path, "s")
    result = worker_tick(service, ns)
    assert result["receipts"][0]["status"] == "failed"
    assert worker_tick(service, ns)["receipts"] == []
    eid = result["receipts"][0]["episode_id"]
    store.transaction(
        lambda tx: tx.run("MATCH (e:MemoryEpisode {id:$id}) SET e.retry_after=0", id=eid).consume()
    )

    class Valid:
        def generate(self, instructions, payload, output):
            return Extraction(
                entities=[PROJECT, MYSQL],
                facts=[
                    {
                        "subject": PROJECT["key"],
                        "target": MYSQL["key"],
                        "relation": "uses_database",
                        "summary": "Atlas uses MySQL.",
                        "valid_at": "2026-09-15T10:00:00Z",
                        "evidence": [{"message_id": "line-1-0", "quote": "Atlas uses MySQL."}],
                    }
                ],
            )

    service.llm = Valid()
    assert worker_tick(service, ns)["receipts"][0]["status"] == "complete"
    assert len(store.recall(ns, "Atlas")["current"]) == 1


def test_file_dates_are_provenance_never_event_time(graph, tmp_path):
    store, ns = graph
    path = tmp_path / "old.md"
    path.write_text("Atlas uses MySQL.")
    transcript = list(transcripts(path, ns))[0]
    assert transcript.source_updated_at is not None
    receipt = store.stage(transcript)
    extraction = Extraction(
        entities=[PROJECT, MYSQL],
        facts=[
            {
                "subject": PROJECT["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "summary": "Atlas uses MySQL.",
                "evidence": [{"message_id": "note-0", "quote": "Atlas uses MySQL."}],
            }
        ],
    )
    store.commit(ns, receipt["episode_id"], extraction)
    context = store.recall(ns, "Atlas")
    assert not context["current"]
    documented = context["documented"][0]
    assert documented.get("valid_ts") is None
    assert documented["time_basis"] == "document_updated"
    assert documented["documented_at"] == transcript.source_updated_at.isoformat()
    actual = {**documented, "id": "actual", "valid_ts": 1, "target": "postgres", "slot": "primary"}
    stale = {**documented, "slot": "primary"}
    projected = project([actual, stale], datetime.now(UTC))
    assert projected["current"][0]["target"] == "postgres"
    assert projected["documented"][0]["target"] == "database:mysql"


def test_recall_repairs_edges_and_excludes_lost_evidence(graph):
    store, ns = graph
    receipt, _, _ = ingest(
        store,
        ns,
        "one",
        "Atlas uses MySQL.",
        [PROJECT, MYSQL],
        [
            {
                "subject": PROJECT["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "valid_at": "2026-09-15T10:00:00Z",
            }
        ],
    )
    store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFact {namespace:$ns})-[r:SUPPORTED_BY]->() DELETE r", ns=ns
        ).consume()
    )
    assert store.recall(ns, "Atlas")["current"]
    assert store.repair(ns)["orphaned_facts"] == 0
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {id:$id}) DETACH DELETE e", id=receipt["episode_id"]
        ).consume()
    )
    result = store.recall(ns, "Atlas")
    assert not result["current"]
    assert result["freshness"]["excluded_ungrounded_facts"] == 1


def test_watcher_restarts_resume_durable_cursor(graph, tmp_path):
    from graph_memory.follow import follow_once

    store, ns = graph
    service = MemoryService(store)
    path = tmp_path / "session.jsonl"
    path.write_text(message("Atlas uses MySQL."))
    assert len(follow_once(service, ns, [tmp_path], {})) == 1
    assert follow_once(service, ns, [tmp_path], {}) == []
    path.write_text(message("Atlas uses MySQL.") + message("Postgres is planned."))
    assert len(follow_once(service, ns, [tmp_path], {})) == 1
    assert len(store.pending(ns)) == 2
