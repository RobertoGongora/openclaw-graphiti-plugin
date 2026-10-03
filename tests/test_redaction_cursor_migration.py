"""Redaction upgrades must preserve source coverage and historical observations."""

import functools
import json

import pytest

from graph_memory import feeds, session_sources
from graph_memory.importers import redact_v1
from graph_memory.service import MemoryService
from graph_memory.session_sources import as_stored, cursor_matches, feed_records, records
from graph_memory.store import digest


def claude(role, content):
    return json.dumps({"type": role, "message": {"role": role, "content": content}}) + "\n"


def boundary_text(tail=""):
    return "x" * 23_950 + " GITHUB_TOKEN=" + "a" * 200 + " Atlas uses MySQL." + tail


def prefix(messages, count=None):
    return digest([m.model_dump(mode="json") for m in messages[:count]])


def write_pair(path):
    path.write_text(
        claude(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "w",
                    "name": "Write",
                    "input": {
                        "file_path": "/x/memory/notes.md",
                        "content": "GITHUB_TOKEN=abcd1234efgh",
                    },
                }
            ],
        )
        + claude(
            "user",
            [{"type": "tool_result", "tool_use_id": "w", "content": "File written successfully."}],
        )
    )


@pytest.mark.parametrize("tail", ["", "y" * 500], ids=["fewer-chunks", "same-chunk-count"])
@pytest.mark.parametrize("as_dict", [False, True])
def test_partly_consumed_record_requires_unchanged_coverage(tmp_path, tail, as_dict):
    path = tmp_path / "s.jsonl"
    path.write_text(
        "".join(claude("assistant", f"Earlier message {i}") for i in range(31))
        + claude("user", boundary_text(tail))
    )
    older, current = list(records(path, redact_v1)), list(records(path))
    assert len(older) == 33
    assert [m.id for m in older[:32]] == [m.id for m in current[:32]]
    assert "Atlas uses MySQL." not in "".join(m.content for m in older[:32])
    assert "Atlas uses MySQL." in current[31].content
    found = [m.model_dump(mode="json") for m in current] if as_dict else current
    assert not cursor_matches(path, found, 32, prefix(older, 32))


def test_fully_consumed_record_with_same_chunk_ids_can_migrate(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(claude("user", boundary_text("y" * 500)))
    older, current = list(records(path, redact_v1)), list(records(path))
    assert len(older) == len(current) == 2
    assert [m.id for m in older] == [m.id for m in current]
    assert prefix(older) != prefix(current)
    assert cursor_matches(path, current, 2, prefix(older))


def test_unchanged_partial_record_after_a_redaction_change_can_migrate(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(claude("user", "GITHUB_TOKEN=abcd1234efgh") + claude("assistant", "x" * 25_000))
    older, current = list(records(path, redact_v1)), list(records(path))
    assert older[1:] == current[1:]
    assert cursor_matches(path, current, 2, prefix(older, 2))


def test_legacy_cursor_check_is_cached_until_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    path.write_text(claude("user", "GITHUB_TOKEN=abcd1234efgh"))
    older, current = list(records(path, redact_v1)), list(records(path))
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return records(*args, **kwargs)

    monkeypatch.setattr(session_sources, "records", counted)
    for _ in range(2):
        assert cursor_matches(path, current, 1, prefix(older))
    assert len(calls) == 1
    with path.open("a") as stream:
        stream.write(claude("assistant", "Noted."))
    assert cursor_matches(path, list(records(path)), 1, prefix(older))
    assert len(calls) == 2


@pytest.mark.parametrize("stored_legacy", [True, False])
def test_carried_result_restores_touches_even_when_its_text_is_unchanged(tmp_path, stored_legacy):
    path = tmp_path / "s.jsonl"
    write_pair(path)
    older, current = list(records(path, redact_v1)), list(records(path))
    assert older[1].content == current[1].content
    assert older[1].touches != current[1].touches
    saved = older if stored_legacy else current

    class Tx:
        def run(self, query, **params):
            return [
                {
                    "id": mid,
                    "content": m.content,
                    "observations": [
                        digest([mid, i, touch.model_dump()]) for i, touch in enumerate(m.touches)
                    ],
                }
                for m in saved
                if (mid := digest(["test", "s", m.id])) in params["ids"]
            ]

    restored = as_stored(Tx(), "test", "s", path, current, {m.id for m in current})
    assert restored == saved


def feed_state(store, namespace, fid):
    return store.read(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {namespace:$ns,id:$id}) RETURN properties(f) AS f",
            ns=namespace,
            id=fid,
        ).single()["f"]
    )


@pytest.mark.integration
def test_default_feed_refuses_unread_tail_migration_atomically(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    path = tmp_path / "s.jsonl"
    path.write_text(
        "".join(claude("assistant", f"Earlier message {i}") for i in range(31))
        + claude("user", boundary_text())
    )
    with monkeypatch.context() as patch:
        patch.setattr(session_sources, "records", functools.partial(records, scrub=redact_v1))
        first = feed_records(service, ns, path, "s")
    assert (first["message_count"], first["available_messages"], first["caught_up"]) == (
        32,
        33,
        False,
    )
    before = feed_state(store, ns, first["feed_id"])
    pending = store.pending(ns)
    with pytest.raises(ValueError, match="prefix changed"):
        feed_records(service, ns, path, "s")
    assert feed_state(store, ns, first["feed_id"]) == before
    assert store.pending(ns) == pending


@pytest.mark.integration
def test_text_feed_refuses_shifted_message_identity(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    path = tmp_path / "s.jsonl"
    path.write_text(claude("user", boundary_text()))
    with monkeypatch.context() as patch:
        patch.setattr(
            feeds, "read_messages", functools.partial(feeds.read_messages, scrub=redact_v1)
        )
        first = feeds.feed(service, ns, path, "s")
    assert first["message_count"] == 2
    with path.open("a") as stream:
        stream.write(claude("user", "Atlas moved to Postgres."))
    before = feed_state(store, ns, first["feed_id"])
    pending = store.pending(ns)
    with pytest.raises(ValueError, match="prefix changed"):
        feeds.feed(service, ns, path, "s")
    assert feed_state(store, ns, first["feed_id"]) == before
    assert store.pending(ns) == pending


@pytest.mark.integration
@pytest.mark.parametrize("text_only", [False, True])
def test_complete_multichunk_record_still_appends(graph, tmp_path, monkeypatch, text_only):
    store, ns = graph
    service = MemoryService(store)
    path = tmp_path / "s.jsonl"
    path.write_text(claude("user", boundary_text("y" * 500)))
    module, name, intake = (
        (feeds, "read_messages", feeds.feed)
        if text_only
        else (session_sources, "records", feed_records)
    )
    with monkeypatch.context() as patch:
        patch.setattr(module, name, functools.partial(getattr(module, name), scrub=redact_v1))
        first = intake(service, ns, path, "s")
    assert first["message_count"] == 2
    with path.open("a") as stream:
        stream.write(claude("user", "Atlas moved to Postgres."))
    appended = intake(service, ns, path, "s")
    assert appended["message_count"] == 3
    assert len(appended["receipts"]) == 1
    episode = store.episode(ns, appended["receipts"][0]["episode_id"])
    payload = json.loads(episode["payload"])
    assert payload["focus_message_ids"] == [payload["messages"][-1]["id"]]
    assert payload["messages"][-1]["content"] == "Atlas moved to Postgres."
    assert intake(service, ns, path, "s")["receipts"] == []


@pytest.mark.integration
def test_recarried_write_result_retains_original_observations(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    path = tmp_path / "s.jsonl"
    write_pair(path)
    with monkeypatch.context() as patch:
        patch.setattr(session_sources, "records", functools.partial(records, scrub=redact_v1))
        first = feed_records(service, ns, path, "s")
    original = json.loads(store.episode(ns, first["receipts"][0]["episode_id"])["payload"])

    def observations():
        return store.read(
            lambda tx: [
                dict(row)
                for row in tx.run(
                    "MATCH (o:MemoryArtifactObservation {namespace:$ns}) "
                    "RETURN properties(o) AS o ORDER BY o.id",
                    ns=ns,
                )
            ]
        )

    before = observations()
    assert len(before) == 2
    # The second append exercises restoration after the cursor uses the new hash.
    for reply in ("First.", "Second."):
        with path.open("a") as stream:
            stream.write(claude("assistant", reply))
        appended = feed_records(service, ns, path, "s")
        episode = store.episode(ns, appended["receipts"][0]["episode_id"])
        assert json.loads(episode["payload"])["messages"][:2] == original["messages"]
        assert observations() == before
