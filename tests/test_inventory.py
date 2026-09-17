from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from graph_memory.inventory import census, run_inventory, save_inventory
from graph_memory.service import MemoryService
from graph_memory.session_sources import feed_records
from graph_memory.status import status
from tests.test_session_sources import claude


def test_inventory_matches_real_remaining_batches_and_does_not_stage(graph, tmp_path):
    store, ns = graph
    path = tmp_path / "session.jsonl"
    # Long records exercise both record splitting and the character bound.
    path.write_text("".join(claude("user", "a" * 100_000) for _ in range(5)))
    service = MemoryService(store)
    from graph_memory.store import digest

    session = "host:" + digest(str(path))
    first = feed_records(service, ns, path, session, max_batches=1)
    initial = census(store, ns, [tmp_path, path])  # overlapping roots are deduplicated
    assert initial["state"] == "available"
    assert initial["files"] == initial["files_with_unstaged"] == 1
    assert len(store.pending(ns)) == len(first["receipts"]) == 1
    rest = feed_records(service, ns, path, session, max_batches=100)
    assert initial["unstaged_episodes"] == len(rest["receipts"])
    assert initial["unstaged_chunks"] == rest["message_count"] - first["message_count"]
    final = census(store, ns, [tmp_path])
    assert final["unstaged_episodes"] == final["unstaged_chunks"] == 0
    assert final["files_caught_up"] == 1


def test_inventory_gaps_are_partial_not_zero_backlog(graph, tmp_path):
    store, ns = graph
    (tmp_path / "bad.jsonl").write_text('{"invalid":\n')
    (tmp_path / "partial.jsonl").write_text('{"type":')
    (tmp_path / "good.jsonl").write_text(claude("user", "Atlas uses MySQL."))
    result = census(store, ns, [tmp_path, tmp_path / "missing"])
    assert result["state"] == "partial"
    assert result["gaps"]["parse_errors"] == 1
    assert result["gaps"]["partial_files"] == 1
    assert result["gaps"]["inaccessible_roots"] == 1
    assert result["unstaged_episodes"] == 1
    assert "Atlas" not in str(result) and str(tmp_path) not in str(result)


def test_inventory_excludes_rewritten_and_changing_sources(graph, tmp_path, monkeypatch):
    store, ns = graph
    from graph_memory import inventory
    from graph_memory.store import digest

    path = tmp_path / "rewritten.jsonl"
    path.write_text(claude("user", "Original"))
    feed_records(MemoryService(store), ns, path, "host:" + digest(str(path)))
    path.write_text(claude("user", "Rewritten"))
    assert census(store, ns, [tmp_path])["gaps"]["prefix_mismatches"] == 1
    growing = tmp_path / "growing.jsonl"
    growing.write_text(claude("user", "First"))
    original = inventory.records

    def append_while_reading(p):
        yield from original(p)
        if p == growing:
            with p.open("a") as stream:
                stream.write(claude("user", "Second"))

    monkeypatch.setattr(inventory, "records", append_while_reading)
    result = census(store, ns, [tmp_path])
    assert result["gaps"]["changed_files"] == 1
    assert result["files_counted"] == 0
    assert result["state"] == "partial"


def test_status_cached_inventory_scope_staleness_and_no_filesystem_reads(graph, monkeypatch):
    store, ns = graph
    checked = datetime(2026, 9, 17, tzinfo=UTC)
    monkeypatch.setattr("graph_memory.status.now", lambda: checked)
    request = SimpleNamespace(namespace=ns)
    assert status(store, request)["source_inventory"]["state"] == "unavailable"
    snapshot = {
        "state": "partial",
        "unstaged_episodes": 123,
        "started_at": (checked - timedelta(seconds=700)).isoformat(),
        "finished_at": (checked - timedelta(seconds=650)).isoformat(),
        "gaps": {"parse_errors": 1},
    }
    save_inventory(store, ns, snapshot, 300)

    def forbidden(*args):
        raise AssertionError("Status must never scan source files")

    monkeypatch.setattr("graph_memory.inventory.records", forbidden)
    report = status(store, request)["source_inventory"]
    assert report["unstaged_episodes"] == 123
    assert report["age_seconds"] == 650
    assert report["stale"] is False  # two intervals plus scan duration
    monkeypatch.setattr("graph_memory.status.now", lambda: checked + timedelta(seconds=1))
    assert status(store, request)["source_inventory"]["stale"] is True
    assert (
        status(store, SimpleNamespace(namespace=ns + "other"))["source_inventory"]["state"]
        == "unavailable"
    )
    assert status(store, request)["episodes"]["total"] == 0


def test_inventory_once_persists_snapshot_without_model(graph, tmp_path):
    store, ns = graph
    (tmp_path / "session.jsonl").write_text(claude("user", "New information"))
    result = run_inventory(store, ns, [tmp_path], once=True)
    assert result["unstaged_episodes"] == 1
    report = status(store, SimpleNamespace(namespace=ns))
    assert report["source_inventory"]["unstaged_episodes"] == 1
    assert report["source_inventory"]["stale"] is False
    assert report["episodes"]["total"] == 0
