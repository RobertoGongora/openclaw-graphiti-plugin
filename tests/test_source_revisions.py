import json

import pytest

from graph_memory.feed_identity import Feeds, parse_root
from graph_memory.follow import follow_once
from graph_memory.inventory import census
from graph_memory.journal import Journal
from graph_memory.service import MemoryService
from graph_memory.session_sources import feed_records
from graph_memory.source_revisions import apply, plan
from graph_memory.store import digest
from tests.test_session_sources import claude


def setup_source(graph, tmp_path):
    store, ns = graph
    path = tmp_path / "session.jsonl"
    common = claude("user", "Atlas uses MySQL.")
    path.write_text(common + claude("assistant", "Old branch statement."))
    key = parse_root(tmp_path).key(path)[1]
    session = "host:" + digest(str(path))
    result = feed_records(MemoryService(store), ns, path, session, source_key=key)
    path.write_text(common + claude("assistant", "New branch statement."))
    return store, ns, path, result["feed_id"], common


def test_reviewed_revision_preserves_evidence_and_resumes_only_changed_tail(graph, tmp_path):
    store, ns, path, fid, _ = setup_source(graph, tmp_path)
    old_payloads = store.read(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN e.id AS id,e.payload AS p", ns=ns
        ).data()
    )
    reviewed = plan(store, ns, fid, path, "Transcript resumed along a different branch")
    assert reviewed["common_prefix_count"] == 1
    assert reviewed["retained_old_tail_chunks"] == reviewed["new_tail_chunks"] == 1
    assert census(store, ns, [tmp_path])["gaps"]["prefix_mismatches"] == 1
    result = apply(store, ns, reviewed)
    assert result["session_id"] != reviewed["old_session_id"]
    assert apply(store, ns, reviewed)["replayed"]
    resolver = Feeds(store, ns, [tmp_path])
    identity = resolver.resolve(reviewed["source_key"], str(path))
    assert identity.feed_id == result["feed_id"] and not resolver.blocked
    outputs = follow_once(MemoryService(store), ns, [tmp_path], {}, source_records=True)
    assert sum(len(row.get("receipts", [])) for row in outputs) == 1
    assert not follow_once(MemoryService(store), ns, [tmp_path], {}, source_records=True)
    rows = store.read(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN e.id AS id,e.payload AS p", ns=ns
        ).data()
    )
    assert all(row in rows for row in old_payloads)
    new = json.loads(next(row["p"] for row in rows if row not in old_payloads))
    assert new["session_id"] == result["session_id"]
    assert new["focus_message_ids"] == ["line-2-block-0-0"]
    assert census(store, ns, [tmp_path])["state"] == "available"
    assert Journal(store).verify_live(ns)["verified"]


def test_revision_refuses_changed_file_changed_plan_and_cross_namespace(graph, tmp_path):
    store, ns, path, fid, common = setup_source(graph, tmp_path)
    reviewed = plan(store, ns, fid, path, "Reviewed changed continuation")
    with pytest.raises(ValueError, match="namespace"):
        apply(store, ns + "other", reviewed)
    with pytest.raises(ValueError, match="digest"):
        apply(store, ns, {**reviewed, "common_prefix_count": 0})
    path.write_text(common + claude("assistant", "A third branch."))
    with pytest.raises(ValueError, match="after review"):
        apply(store, ns, reviewed)
    assert not store.read(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {namespace:$ns,id:$id}) RETURN f.superseded_by AS next",
            ns=ns,
            id=fid,
        ).single()
    )["next"]


def test_revision_requires_actual_rewrite_and_complete_source(graph, tmp_path):
    store, ns, path, fid, common = setup_source(graph, tmp_path)
    path.write_text(
        common + claude("assistant", "Old branch statement.") + claude("user", "Appended.")
    )
    with pytest.raises(ValueError, match="unchanged"):
        plan(store, ns, fid, path, "Only appended")
    path.write_text(common + '{"type":')
    with pytest.raises(ValueError, match="incomplete"):
        plan(store, ns, fid, path, "Incomplete")


def test_revision_rejects_cursor_not_reconstructable_from_stored_evidence(graph, tmp_path):
    store, ns, path, fid, _ = setup_source(graph, tmp_path)
    store.transaction(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {namespace:$ns,id:$id}) SET f.message_count=999", ns=ns, id=fid
        ).consume()
    )
    with pytest.raises(ValueError, match="reconstruct"):
        plan(store, ns, fid, path, "Review")


def test_revision_preserves_mixed_parser_history(graph, tmp_path, monkeypatch):
    from graph_memory import session_sources
    from graph_memory.models import Message
    from tests.test_session_sources import codex

    store, ns = graph
    path = tmp_path / "session.jsonl"
    common = (
        claude("user", "Run tests.")
        + codex("custom_tool_call", call_id="c", name="exec", input="make test")
        + codex("custom_tool_call_output", call_id="c", output="12 passed")
    )
    path.write_text(common)
    real_records = session_sources.records

    def legacy_records(p):
        for m in real_records(p):
            yield Message.model_validate(
                session_sources.before_shell_results(m.model_dump(mode="json"))
            )

    monkeypatch.setattr(session_sources, "records", legacy_records)
    key = parse_root(tmp_path).key(path)[1]
    fid = feed_records(MemoryService(store), ns, path, "session", source_key=key)["feed_id"]
    monkeypatch.setattr(session_sources, "records", real_records)
    path.write_text(common + claude("assistant", "Original continuation."))
    feed_records(MemoryService(store), ns, path, "session", source_key=key)
    path.write_text(common + claude("assistant", "Rewritten continuation."))
    reviewed = plan(store, ns, fid, path, "Mixed parser versions remain immutable")
    assert reviewed["common_prefix_count"] == 3
    assert apply(store, ns, reviewed)["applied"]
