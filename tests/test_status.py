from datetime import UTC, datetime
from types import SimpleNamespace

from graph_memory.mcp import Protocol
from graph_memory.service import MemoryService
from graph_memory.status import status
from tests.test_contracts import headers, rpc


def test_status_empty_namespace_is_read_only(graph):
    store, ns = graph
    protocol = Protocol(MemoryService(store), namespace=ns, read_only=True)
    message = rpc("tools/call", name="memory_status", arguments={})
    code, response = protocol.dispatch(message, headers(message))
    assert code == 200
    result = response["result"]["structuredContent"]
    assert result["episodes"] == {
        "total": 0,
        "by_status": {"pending": 0, "complete": 0, "failed": 0},
    }
    assert result["processing"]["is_processing"] is False
    assert result["processing"]["active_episodes"] == []
    assert result["graph"] == {"entities": 0, "facts": 0}
    for key in ("latest_episode", "latest_completed_episode", "oldest_incomplete_episode"):
        assert result[key] is None
    assert (
        store.transaction(
            lambda tx: tx.run(
                "MATCH (s:MemorySpace {id:$ns}) RETURN count(*) AS count", ns=ns
            ).single()["count"]
        )
        == 0
    )
    message["params"]["arguments"] = {"namespace": ns + ":other"}
    assert protocol.dispatch(message, headers(message))[0] == 403


def test_status_leases_retries_latest_and_scope(graph, monkeypatch):
    store, ns = graph
    checked = datetime(2026, 9, 17, tzinfo=UTC)
    monkeypatch.setattr("graph_memory.status.now", lambda: checked)
    at = checked.timestamp()
    episodes = [
        {"id": f"active-{i}", "status": "pending", "lease_until": at + 60} for i in range(6)
    ] + [
        {"id": "queued", "status": "pending"},
        {"id": "retry-due", "status": "failed", "retry_after": at},
        {"id": "delayed", "status": "failed", "retry_after": at + 60, "attempts": 2},
        {"id": "expired", "status": "pending", "lease_until": at},
        # A completed episode's leftover lease must not count as active.
        {
            "id": "done",
            "status": "complete",
            "lease_until": at + 60,
            "completed_at": "2026-09-16T12:00:00+00:00",
            "fact_count": 1,
        },
        {"id": "newest", "status": "pending"},
    ]
    for index, episode in enumerate(episodes):
        episode.update(
            id=ns + episode["id"],
            namespace=ns,
            ingested_at=f"2026-09-16T10:{index:02d}:00+00:00",
            payload="private transcript",
            error="private error",
        )
    store.transaction(
        lambda tx: tx.run(
            "UNWIND $episodes AS props CREATE (e:MemoryEpisode) SET e=props "
            "WITH count(*) AS ignored "
            "CREATE (:MemoryEntity {id:$entity,namespace:$ns}), "
            "(:MemoryEntity {id:$merged,namespace:$ns,merged_into:$entity}), "
            "(:MemoryFact {id:$fact,namespace:$ns}), "
            "(:MemoryFact {id:$retracted,namespace:$ns,retracted:true})",
            episodes=episodes,
            ns=ns,
            entity=ns + "entity",
            merged=ns + "merged",
            fact=ns + "fact",
            retracted=ns + "retracted",
        ).consume()
    )
    result = status(store, SimpleNamespace(namespace=ns))
    assert result["episodes"] == {
        "total": 12,
        "by_status": {"pending": 9, "failed": 2, "complete": 1},
    }
    processing = result["processing"]
    assert {k: processing[k] for k in ("active", "queued", "retry_delayed", "expired_leases")} == {
        "active": 6,
        "queued": 4,
        "retry_delayed": 1,
        "expired_leases": 1,
    }
    assert processing["is_processing"] is True
    assert processing["active_episodes_truncated"] is True
    assert len(processing["active_episodes"]) == 5
    assert result["latest_episode"]["episode_id"] == ns + "newest"
    assert result["latest_completed_episode"]["episode_id"] == ns + "done"
    assert result["oldest_incomplete_episode"]["episode_id"] == ns + "active-0"
    assert result["graph"] == {"entities": 1, "facts": 1}
    assert "private" not in str(result)
    other = status(store, SimpleNamespace(namespace=ns + ":other"))
    assert other["episodes"]["total"] == 0
    assert other["graph"] == {"entities": 0, "facts": 0}
    assert other["latest_episode"] is None


def test_status_lists_uncertain_claims_repeated_across_sessions(graph):
    store, ns = graph
    facts = []
    for triple, sessions in (("thrice", 3), ("twice", 2)):
        for i in range(sessions + 1):  # one session says it twice: still that many sessions
            facts.append(
                {
                    "id": f"{ns}{triple}-{i}",
                    "namespace": ns,
                    "status": "uncertain",
                    "subject_id": f"{ns}s",
                    "relation": "related_to",
                    "target_id": f"{ns}{triple}",
                    "subject": "Atlas",
                    "target": triple,
                    "session_id": f"session-{min(i, sessions - 1)}",
                    "recorded_at": f"2026-09-2{i}T00:00:00+00:00",
                    "summary": f"Atlas {triple} wording {i}",
                }
            )
    # Retracted or confirmed copies and validated claims do not count.
    facts += [
        {**facts[0], "id": f"{ns}retracted", "session_id": "session-9", "retracted": True},
        {**facts[0], "id": f"{ns}confirmed", "session_id": "session-8", "confirmed_at": "x"},
        {**facts[0], "id": f"{ns}active", "session_id": "session-7", "status": "active"},
    ]
    store.transaction(
        lambda tx: tx.run("UNWIND $facts AS props CREATE (f:MemoryFact) SET f=props", facts=facts)
    )
    result = status(store, SimpleNamespace(namespace=ns))["corroboration"]
    assert result["min_sessions"] == 3
    assert result["uncertain_triples"] == 1 and result["uncertain_facts"] == 4
    (row,) = result["top"]
    assert row["sessions"] == 3 and row["facts"] == 4 and row["target"] == "thrice"
    assert row["latest_fact_id"] == f"{ns}thrice-3" and row["text"] == "Atlas thrice wording 3"
