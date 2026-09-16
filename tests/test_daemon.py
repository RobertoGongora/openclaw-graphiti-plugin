from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from graph_memory.daemon import run_daemon, scan_bank
from graph_memory.importers import transcripts
from graph_memory.models import Extraction
from graph_memory.service import MemoryService


class EmptyModel:
    def __init__(self):
        self.calls = 0

    def generate(self, instructions, payload, output):
        self.calls += 1
        return Extraction(entities=[], facts=[])


def test_scanner_adopts_import_across_birthtime_difference_and_restart(graph, tmp_path):
    store, ns = graph
    note = tmp_path / "note.md"
    note.write_text("Atlas uses MySQL.")
    original = next(transcripts(note, ns))
    original.source_created_at = datetime(2020, 1, 1, tzinfo=UTC)
    receipt = store.stage(original)
    store.commit(ns, receipt["episode_id"], Extraction(entities=[], facts=[]))
    service = MemoryService(store, EmptyModel())
    for _ in range(2):
        result = scan_bank(service, ns, [tmp_path], {})
        assert result["staged"] == 0
        assert result["existing"] == 1
    assert store.episode(ns, receipt["episode_id"])["status"] == "complete"
    assert service.llm.calls == 0


def test_scanner_changed_source_concurrency_and_missing_root(graph, tmp_path):
    store, ns = graph
    note = tmp_path / "note.md"
    note.write_text("Atlas uses MySQL.")
    service = MemoryService(store, EmptyModel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: scan_bank(service, ns, [tmp_path], {}), range(2)))
    assert sum(r["staged"] for r in results) == 1
    old = store.pending(ns)[0]["episode_id"]
    note.write_text("Atlas plans to use PostgreSQL.")
    result = scan_bank(service, ns, [tmp_path, tmp_path / "missing"], {})
    assert result["staged"] == 1
    assert len(result["failures"]) == 1
    assert len(store.pending(ns)) == 2
    assert "MySQL" in store.episode(ns, old)["payload"]
    note.unlink()
    assert scan_bank(service, ns, [tmp_path], {})["staged"] == 0
    assert len(store.pending(ns)) == 2  # source deletion does not erase evidence


def test_daemon_resumes_pending_once_without_reprocessing_complete(graph, tmp_path):
    store, ns = graph
    (tmp_path / "note.md").write_text("No durable information.")
    service = MemoryService(store, EmptyModel())
    scan_bank(service, ns, [tmp_path], {})  # previous host only staged the input
    run_daemon(service, ns, [tmp_path], workers=2, once=True)
    assert service.llm.calls == 1
    assert store.pending(ns) == []
    run_daemon(service, ns, [tmp_path], workers=2, once=True)
    assert service.llm.calls == 1


def test_scanner_retries_failed_file_without_marking_seen(graph, tmp_path):
    store, ns = graph
    path = tmp_path / "note.md"
    path.write_bytes(b"\xff")
    seen = {}
    service = MemoryService(store)
    assert scan_bank(service, ns, [tmp_path], seen)["failures"]
    assert str(path) not in seen
    path.write_text("Atlas uses MySQL.")
    assert scan_bank(service, ns, [tmp_path], seen)["staged"] == 1
