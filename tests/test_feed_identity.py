import json
import shutil
from pathlib import Path

import pytest

from graph_memory import session_sources
from graph_memory.feed_identity import (
    Feeds,
    parse_root,
    session_uid,
    source_files,
    stamp_existing,
    validate_roots,
)
from graph_memory.follow import follow_once
from graph_memory.inventory import census
from graph_memory.service import MemoryService
from graph_memory.session_sources import feed_records
from graph_memory.store import digest
from tests.test_session_sources import claude


def tree(root):
    """Two sessions the way Claude lays them out, one of them a subagent's."""
    files = [
        root / "-Users-rob" / "one.jsonl",
        root / "-Users-rob" / "one" / "subagents" / "a.jsonl",
    ]
    for number, path in enumerate(files):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(claude("user", f"Atlas note {number}.") + claude("assistant", "Noted."))
    return files


def feeds(store, ns):
    return store.read(
        lambda tx: {
            row["f"]["id"]: row["f"]
            for row in tx.run(
                "MATCH (f:MemoryFeed {namespace:$ns}) RETURN properties(f) AS f", ns=ns
            )
        }
    )


def episodes(store, ns):
    return store.read(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN count(e) AS n", ns=ns
        ).single()["n"]
    )


def drain(service, ns, roots):
    seen = {}
    while follow_once(service, ns, roots, seen, source_records=True):
        pass


@pytest.fixture
def parsed(monkeypatch):
    calls = []
    real = session_sources.feed_records

    def counting(service, namespace, path, session_id, **kwargs):
        calls.append(path.name)
        return real(service, namespace, path, session_id, **kwargs)

    monkeypatch.setattr(session_sources, "feed_records", counting)
    return calls


def test_labels_do_not_depend_on_the_mount_point(tmp_path):
    assert parse_root(Path("/sessions/claude")).label == "claude"
    assert parse_root(Path("~/.claude/projects")).label == "claude"
    assert parse_root(Path("~/.codex/sessions")).label == "codex"
    stated = parse_root(Path("work=/mnt/anything"))
    assert (stated.label, str(stated.given)) == ("work", "/mnt/anything")
    # A path with "=" in it is a path when the part before "=" is not a valid label.
    odd = tmp_path / "a=b"
    odd.mkdir()
    assert parse_root(odd).label == "a_b"
    files = tree(tmp_path / "x" / ".claude" / "projects")
    assert sorted(source_files([tmp_path / "x" / ".claude" / "projects"]).values()) == [
        "claude:-Users-rob/one.jsonl",
        "claude:-Users-rob/one/subagents/a.jsonl",
    ]
    # A root that is one file, and overlapping roots in either order, name it the same.
    assert source_files([files[0]]) == {str(files[0].resolve()): "-Users-rob:one.jsonl"}
    nested = [
        tmp_path / "x" / ".claude" / "projects",
        tmp_path / "x" / ".claude" / "projects" / "-Users-rob",
    ]
    assert source_files(nested) == source_files(nested[::-1])


def test_session_uid_reads_both_hosts(tmp_path):
    a, b, c = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "c.jsonl"
    a.write_text('{"type":"queue-operation","sessionId":"claude-uid"}\n')
    b.write_text(json.dumps({"type": "session_meta", "payload": {"id": "codex-uid"}}) + "\n")
    c.write_text("not json\n" + claude("user", "No id here."))
    assert [session_uid(p) for p in (a, b, c, tmp_path / "gone")] == [
        "claude-uid",
        "codex-uid",
        None,
        None,
    ]


def test_moved_root_keeps_feeds_cursors_and_caught_up_state(graph, tmp_path, parsed):
    store, ns = graph
    service = MemoryService(store)
    old = tmp_path / "mnt" / "claude"
    files = tree(old)
    files[0].write_text('{"type":"queue-operation","sessionId":"uid-1"}\n' + files[0].read_text())
    drain(service, ns, [old])
    before, staged = feeds(store, ns), episodes(store, ns)
    assert sorted(f["source_key"] for f in before.values()) == [
        "claude:-Users-rob/one.jsonl",
        "claude:-Users-rob/one/subagents/a.jsonl",
    ]
    assert sorted(f.get("session_uid", "") for f in before.values()) == ["", "uid-1"]
    assert all(f["session_id"].startswith("source:") for f in before.values())

    # A rename keeps size and mtime: a new process under a new mount reparses nothing.
    new = tmp_path / "elsewhere" / ".claude" / "projects"
    new.parent.mkdir(parents=True)
    old.rename(new)
    parsed.clear()
    assert follow_once(service, ns, [new], {}, source_records=True) == []
    assert parsed == [] and episodes(store, ns) == staged
    assert feeds(store, ns) == before
    moved = census(store, ns, [new])
    assert (moved["state"], moved["files_caught_up"], moved["unstaged_episodes"]) == (
        "available",
        2,
        0,
    )

    # Only what was appended after the move is staged, on the same feed and session.
    with (new / "-Users-rob" / "one.jsonl").open("a") as stream:
        stream.write(claude("user", "Atlas moved to Postgres."))
    fed = follow_once(service, ns, [new], {}, source_records=True)
    assert parsed == ["one.jsonl"] and len(fed) == 1 and len(fed[0]["receipts"]) == 1
    after = feeds(store, ns)
    assert set(after) == set(before) and episodes(store, ns) == staged + 1
    feed = after[fed[0]["feed_id"]]
    assert feed["session_id"] == before[feed["id"]]["session_id"]
    assert feed["message_count"] == before[feed["id"]]["message_count"] + 1
    assert feed["source_uri"] == str((new / "-Users-rob" / "one.jsonl").resolve())
    payload = store.read(
        lambda tx: json.loads(
            tx.run(
                "MATCH (e:MemoryEpisode {id:$id}) RETURN e.payload AS p",
                id=fed[0]["receipts"][0]["episode_id"],
            ).single()["p"]
        )
    )
    assert len(payload["focus_message_ids"]) == 1
    assert payload["session_id"] == feed["session_id"]

    # A copy changes mtime, so the files are read again, and found already fed.
    copied = tmp_path / "third" / "claude"
    shutil.copytree(new, copied, copy_function=shutil.copy)
    shutil.rmtree(new)
    parsed.clear()
    assert follow_once(service, ns, [copied], {}, source_records=True) == []
    assert sorted(parsed) == ["a.jsonl", "one.jsonl"]
    assert set(feeds(store, ns)) == set(before) and episodes(store, ns) == staged + 1


def legacy(service, ns, files):
    return {
        feed_records(service, ns, path, "host:" + digest(str(path.resolve())))["feed_id"]
        for path in files
    }


def test_legacy_feeds_are_adopted_in_place_without_a_migration(graph, tmp_path, parsed):
    store, ns = graph
    service = MemoryService(store)
    root = tmp_path / "sessions" / "claude"
    files = tree(root)
    ids = legacy(service, ns, files)
    before, staged = feeds(store, ns), episodes(store, ns)
    assert set(before) == ids and not any("source_key" in f for f in before.values())
    # The path alone finds an unnamed feed: nothing reads as backlog.
    index = Feeds(store, ns)
    assert {index.resolve(key, name).feed_id for name, key in source_files([root]).items()} == ids
    assert census(store, ns, [root])["unstaged_episodes"] == 0
    parsed.clear()
    assert follow_once(service, ns, [root], {}, source_records=True) == []
    after = feeds(store, ns)
    assert set(after) == ids and episodes(store, ns) == staged
    # Ids, sessions and cursors are untouched; the feeds are now named for the next move.
    for fid, feed in after.items():
        assert feed.pop("source_key").startswith("claude:-Users-rob/")
        feed.pop("caught_up_size"), feed.pop("caught_up_mtime_ns")
        assert feed == before[fid]
    new = tmp_path / "host" / ".claude" / "projects"
    new.parent.mkdir(parents=True)
    root.rename(new)
    assert follow_once(service, ns, [new], {}, source_records=True) == []
    assert set(feeds(store, ns)) == ids and episodes(store, ns) == staged


def test_stamp_existing_then_follow_from_a_moved_root(graph, tmp_path, parsed):
    store, ns = graph
    service = MemoryService(store)
    root = tmp_path / "sessions" / "claude"
    ids = legacy(service, ns, tree(root))
    before, staged = feeds(store, ns), episodes(store, ns)
    other = stamp_existing(store, ns, [tmp_path / "sessions" / "codex"])
    assert (other["stamped"], other["unmatched"]) == (0, 2) and feeds(store, ns) == before
    first = stamp_existing(store, ns, [root])
    assert (first["feeds"], first["stamped"], first["already_stamped"], first["unmatched"]) == (
        2,
        2,
        0,
        0,
    )
    again = stamp_existing(store, ns, [root])
    assert (again["stamped"], again["already_stamped"]) == (0, 2)
    stamped = feeds(store, ns)
    for fid, feed in stamped.items():
        assert feed.pop("source_key").startswith("claude:")
        assert feed == before[fid]
    # The mount the paths were written under need not exist where the stamping runs.
    new = tmp_path / "data" / "claude-sessions"
    new.parent.mkdir()
    root.rename(new)
    gone = stamp_existing(store, ns, [Path(f"claude={root}")])
    assert (gone["stamped"], gone["already_stamped"]) == (0, 2)
    parsed.clear()
    fed = follow_once(service, ns, [Path(f"claude={new}")], {}, source_records=True)
    # Legacy feeds carried no caught-up stamp, so they are read once and found fed.
    assert fed == [] and sorted(parsed) == ["a.jsonl", "one.jsonl"]
    assert set(feeds(store, ns)) == ids and episodes(store, ns) == staged
    parsed.clear()
    assert follow_once(service, ns, [Path(f"claude={new}")], {}, source_records=True) == []
    assert parsed == []


def test_same_relative_path_under_two_roots_does_not_collide(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    roots = [tmp_path / "sessions" / "claude", tmp_path / "sessions" / "codex"]
    tree(roots[0])
    (roots[1] / "-Users-rob").mkdir(parents=True)
    (roots[1] / "-Users-rob" / "two.jsonl").write_text(claude("user", "A Codex note."))
    drain(service, ns, roots)
    found = feeds(store, ns)
    assert len(found) == 3 and len({f["session_id"] for f in found.values()}) == 3
    assert {f["source_key"].split(":")[0] for f in found.values()} == {"claude", "codex"}
    # The same relative path under the other root is another key, never the same feed.
    # Its name is one a known feed carries, so it is reported instead of fed.
    twin = roots[1] / "-Users-rob" / "one.jsonl"
    twin.write_text(claude("user", "Codex has its own one."))
    seen = {}
    fed = follow_once(service, ns, roots, seen, source_records=True)
    claude_one = next(f for f in found.values() if f["source_key"] == "claude:-Users-rob/one.jsonl")
    assert fed == [
        {
            "source": str(twin.resolve()),
            "status": "feed_identity_refused",
            "source_key": "codex:-Users-rob/one.jsonl",
            "known_feed_id": claude_one["id"],
            "known_source_key": "claude:-Users-rob/one.jsonl",
        }
    ]
    assert follow_once(service, ns, roots, seen, source_records=True) == []  # said once
    assert feeds(store, ns) == found
    assert census(store, ns, roots)["gaps"]["identity_refused"] == 1
    # An operator who knows the files differ accepts them: two feeds, two sessions.
    monkeypatch.setenv("MEMORY_FEED_ACCEPT_UNMATCHED", "1")
    drain(service, ns, roots)
    after = feeds(store, ns)
    assert len(after) == 4 and claude_one == after[claude_one["id"]]
    assert "codex:-Users-rob/one.jsonl" in {f["source_key"] for f in after.values()}


def test_a_unique_name_is_known_wherever_it_reappears(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    name = "0138dd13-1c43-4517-84bc-416fc7aac737.jsonl"
    first = tmp_path / "sessions" / "claude" / "-Users-rob" / name
    first.parent.mkdir(parents=True)
    first.write_text(claude("user", "Atlas uses MySQL."))
    journal = tmp_path / "sessions" / "claude" / "wf_1" / "journal.jsonl"
    journal.parent.mkdir()
    journal.write_text(claude("user", "Workflow one."))
    drain(service, ns, [tmp_path / "sessions" / "claude"])
    before, staged = feeds(store, ns), episodes(store, ns)
    # Relabelled and moved to another directory at once: neither key nor path finds it.
    copy = tmp_path / "backup" / "renamed-project" / name
    copy.parent.mkdir(parents=True)
    shutil.copy(first, copy)
    # Every workflow has a journal.jsonl: only the same directory makes it the same file.
    other = tmp_path / "backup" / "wf_2" / "journal.jsonl"
    other.parent.mkdir()
    other.write_text(claude("user", "Workflow two."))
    fed = follow_once(service, ns, [tmp_path / "backup"], {}, source_records=True)
    assert [(Path(f["source"]).name, f.get("status")) for f in fed] == [
        (name, "feed_identity_refused"),
        ("journal.jsonl", None),
    ]
    assert len(feeds(store, ns)) == len(before) + 1 and episodes(store, ns) == staged + 1


def test_roots_that_cannot_name_older_feeds_block_all_intake(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    container = tmp_path / "sessions" / "claude"
    ids = legacy(service, ns, tree(container))
    before, staged = feeds(store, ns), episodes(store, ns)
    # The daemon now runs where the same files are mounted elsewhere.
    host = tmp_path / "home" / ".claude" / "projects"
    host.parent.mkdir(parents=True)
    container.rename(host)
    (host / "-Users-rob" / "new.jsonl").write_text(claude("user", "A genuinely new session."))
    seen = {}
    for _ in range(2):
        fed = follow_once(service, ns, [host], seen, source_records=True)
        assert [f["status"] for f in fed] == ["feed_identity_blocked"]
        assert (fed[0]["feeds"], fed[0]["stamped"], fed[0]["unmatched"]) == (2, 0, 2)
    assert feeds(store, ns) == before and episodes(store, ns) == staged
    blocked = census(store, ns, [host])
    assert blocked["state"] == "identity_blocked" and blocked["identity"]["unmatched"] == 2
    assert "unstaged_episodes" not in blocked
    # Stamped from anywhere with the prefix the paths were stored under, which is gone.
    assert not container.exists()
    result = stamp_existing(store, ns, [f"claude={container}"])
    assert result == dict(feeds=2, stamped=2, already_stamped=0, unmatched=0, conflicts=0)
    # The running watcher unblocks, finds its feeds, and stages only the new file.
    fed = follow_once(service, ns, [host], seen, source_records=True)
    assert [Path(f["source"]).name for f in fed] == ["new.jsonl"]
    assert ids < set(feeds(store, ns)) and len(feeds(store, ns)) == 3
    assert episodes(store, ns) == staged + 1
    assert census(store, ns, [host])["unstaged_episodes"] == 0


def test_accepting_unmatched_feeds_is_an_explicit_choice(graph, tmp_path, monkeypatch):
    store, ns = graph
    service = MemoryService(store)
    elsewhere = tmp_path / "gone" / "claude"
    legacy(service, ns, tree(elsewhere))
    root = tmp_path / "sessions" / "codex"
    root.mkdir(parents=True)
    (root / "fresh.jsonl").write_text(claude("user", "Unrelated to the unmatched feeds."))
    assert follow_once(service, ns, [root], {}, source_records=True)[0]["status"] == (
        "feed_identity_blocked"
    )
    monkeypatch.setenv("MEMORY_FEED_ACCEPT_UNMATCHED", "1")
    fed = follow_once(service, ns, [root], {}, source_records=True)
    assert [Path(f["source"]).name for f in fed] == ["fresh.jsonl"]
    assert census(store, ns, [root])["state"] == "available"


def test_a_change_of_roots_never_mints_a_feed_for_a_known_path(graph, tmp_path, parsed):
    store, ns = graph
    service = MemoryService(store)
    root = tmp_path / "sessions" / "claude"
    tree(root)
    drain(service, ns, [root])
    before, staged = feeds(store, ns), episodes(store, ns)
    link = tmp_path / "linked-under-another-name"
    link.symlink_to(root)
    for roots, keys in (
        (
            [Path(f"work={root}")],
            ["work:-Users-rob/one.jsonl", "work:-Users-rob/one/subagents/a.jsonl"],
        ),
        ([root, root / "-Users-rob"], ["-Users-rob:one.jsonl", "-Users-rob:one/subagents/a.jsonl"]),
        (
            [link],
            [
                "linked-under-another-name:-Users-rob/one.jsonl",
                "linked-under-another-name:-Users-rob/one/subagents/a.jsonl",
            ],
        ),
    ):
        parsed.clear()
        assert follow_once(service, ns, roots, {}, source_records=True) == []
        assert census(store, ns, roots)["unstaged_episodes"] == 0
        after = feeds(store, ns)
        # Same feeds, never reopened, and named by today's roots for the next move.
        assert parsed == [] and set(after) == set(before) and episodes(store, ns) == staged
        assert sorted(f["source_key"] for f in after.values()) == keys
        for fid, feed in after.items():
            assert {**feed, "source_key": ""} == {**before[fid], "source_key": ""}
    # The re-keyed feeds still survive a move under the roots that re-keyed them.
    link.unlink()
    moved = tmp_path / "volume" / "linked-under-another-name"
    moved.parent.mkdir()
    root.rename(moved)
    assert follow_once(service, ns, [moved], {}, source_records=True) == []
    assert parsed == [] and set(feeds(store, ns)) == set(before)


def test_label_clash_names_both_roots(tmp_path):
    first, second = tmp_path / "a" / "claude", tmp_path / "b" / "claude"
    with pytest.raises(ValueError) as raised:
        validate_roots([first, second])
    assert str(first) in str(raised.value) and str(second) in str(raised.value)
    assert "'claude'" in str(raised.value) and "LABEL=PATH" in str(raised.value)
    assert [r.label for r in validate_roots([first, f"backup={second}", first])] == [
        "claude",
        "backup",
        "claude",
    ]


def test_label_fallbacks_and_bounded_uid(tmp_path):
    labels = {
        "/": "root",
        "/sessions": "root",
        "/data/sessions/sessions": "data",
        "/srv/sesiones-año": "sesiones-año",
        "/srv/日本": "日本",
        "/srv/my sessions!": "my_sessions",
        "/srv/.../sessions": "srv",
    }
    assert {path: parse_root(path).label for path in labels} == labels
    huge = tmp_path / "huge.jsonl"
    huge.write_text(json.dumps({"pad": "x" * 400_000, "sessionId": "beyond-the-bound"}) + "\n")
    assert session_uid(huge) is None


def test_label_path_parsed_before_existence_check():
    """LABEL=PATH syntax is recognized by syntax, not existence.

    This allows remote paths that do not exist locally, such as
    ``rob-mbp.claude=/sessions/claude`` where the path is on a remote machine
    reached via Tailscale.
    """
    # Remote path that definitely doesn't exist locally
    remote = parse_root("rob-mbp.claude=/nonexistent/sessions/claude")
    assert remote.label == "rob-mbp.claude"
    assert str(remote.given) == "/nonexistent/sessions/claude"

    # Machine-qualified labels are valid
    for spec in [
        "rob-mbp.claude=/sessions/claude",
        "ct-160.transcripts=/data/transcripts",
        "dev.codex=/home/user/.codex/sessions",
    ]:
        label, _, path = spec.partition("=")
        root = parse_root(spec)
        assert root.label == label
        assert str(root.given) == path


def test_remote_mode_detection():
    from graph_memory.feed_identity import is_remote_bolt

    # Loopback addresses are local
    assert is_remote_bolt("bolt://127.0.0.1:7687") is False
    assert is_remote_bolt("bolt://localhost:7687") is False
    assert is_remote_bolt("bolt://[::1]:7687") is False  # IPv6 loopback with brackets
    # Docker internal hostname is local
    assert is_remote_bolt("bolt://neo4j:7687") is False

    # Remote addresses
    assert is_remote_bolt("bolt://ct-160:7687") is True
    assert is_remote_bolt("bolt://ct-160.tailnet.ts.net:7687") is True
    assert is_remote_bolt("bolt://192.168.1.100:7687") is True
    assert is_remote_bolt("neo4j://graph.example.com:7687") is True

    # TLS variants
    assert is_remote_bolt("bolt+s://graph.example.com:7687") is True
    assert is_remote_bolt("neo4j+s://127.0.0.1:7687") is False


def test_bare_labels_refused_in_remote_mode():
    from graph_memory.feed_identity import validate_remote_label

    # Bare labels are fine locally
    validate_remote_label("claude", remote=False)
    validate_remote_label("codex", remote=False)
    validate_remote_label("cursor", remote=False)

    # Bare labels are refused in remote mode
    for bare in ("claude", "codex", "cursor"):
        with pytest.raises(ValueError) as exc:
            validate_remote_label(bare, remote=True)
        assert "machine-qualified" in str(exc.value)
        assert f"hostname.{bare}" in str(exc.value)

    # Machine-qualified labels are always fine
    validate_remote_label("rob-mbp.claude", remote=True)
    validate_remote_label("ct-160.codex", remote=True)
    validate_remote_label("dev-box.cursor", remote=True)

    # Non-bare labels are fine even if not machine-qualified
    validate_remote_label("personal", remote=True)
    validate_remote_label("transcripts", remote=True)


def test_validate_roots_refuses_bare_labels_in_remote_mode(tmp_path):
    root = tmp_path / "sessions" / "claude"
    root.mkdir(parents=True)

    # Local mode: bare derived labels are fine
    validate_roots([root], remote=False)

    # Remote mode: bare derived labels are refused
    with pytest.raises(ValueError) as exc:
        validate_roots([root], remote=True)
    assert "machine-qualified" in str(exc.value)

    # Remote mode: machine-qualified explicit labels are fine
    validate_roots([f"rob-mbp.claude={root}"], remote=True)


def test_a_different_file_at_a_known_key_is_reported_not_merged(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    old = tmp_path / "a" / "claude"
    tree(old)
    drain(service, ns, [old])
    before, staged = feeds(store, ns), episodes(store, ns)
    new = tmp_path / "b" / "claude"
    impostor = new / "-Users-rob" / "one.jsonl"
    impostor.parent.mkdir(parents=True)
    impostor.write_text(claude("user", "An unrelated session.") + claude("assistant", "Indeed."))
    fed = follow_once(service, ns, [new], {}, source_records=True)
    assert fed == [{"source": str(impostor.resolve()), "status": "failed", "error": "ValueError"}]
    assert feeds(store, ns) == before and episodes(store, ns) == staged
    assert census(store, ns, [new])["gaps"]["prefix_mismatches"] == 1


UUID = "0138dd13-1c43-4517-84bc-416fc7aac737.jsonl"


def project(root, directory="-Users-rob-atlas"):
    """A uuid-named session and a workflow journal, whose name many directories share."""
    session, journal = root / directory / UUID, root / directory / "journal.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(claude("user", "Atlas uses MySQL.") + claude("assistant", "Noted."))
    journal.write_text(claude("user", "Workflow step one.") + claude("assistant", "Done."))
    return session, journal


def test_renamed_directory_continues_the_same_feeds(graph, tmp_path, parsed):
    store, ns = graph
    service = MemoryService(store)
    root = tmp_path / "sessions" / "claude"
    project(root)
    # Another workflow's journal: the name alone must never decide.
    other = root / "-Users-rob-other" / "journal.jsonl"
    other.parent.mkdir()
    other.write_text(claude("user", "Another workflow entirely."))
    seen = {}
    while follow_once(service, ns, [root], seen, source_records=True):
        pass
    before, staged = feeds(store, ns), episodes(store, ns)
    (root / "-Users-rob-atlas").rename(root / "-Users-rob-atlas-v2")
    session = root / "-Users-rob-atlas-v2" / UUID
    with session.open("a") as stream:
        stream.write(claude("user", "Atlas moved to Postgres."))
    # The running watcher and a restarted one agree.
    fed = follow_once(service, ns, [root], seen, source_records=True)
    assert follow_once(service, ns, [root], {}, source_records=True) == []
    assert [(Path(f["source"]).name, f.get("status"), len(f["receipts"])) for f in fed] == [
        (UUID, None, 1)
    ]
    after = feeds(store, ns)
    assert set(after) == set(before) and episodes(store, ns) == staged + 1
    feed = after[fed[0]["feed_id"]]
    assert feed["session_id"] == before[feed["id"]]["session_id"]
    assert feed["message_count"] == before[feed["id"]]["message_count"] + 1
    assert sorted(f["source_key"] for f in after.values()) == [
        "claude:-Users-rob-atlas-v2/" + UUID,
        "claude:-Users-rob-atlas-v2/journal.jsonl",
        "claude:-Users-rob-other/journal.jsonl",
    ]
    assert census(store, ns, [root])["unstaged_episodes"] == 0
    # The journal was adopted because it begins with what its feed holds; appending works.
    with (root / "-Users-rob-atlas-v2" / "journal.jsonl").open("a") as stream:
        stream.write(claude("user", "Workflow step two."))
    fed = follow_once(service, ns, [root], seen, source_records=True)
    assert len(fed) == 1 and len(fed[0]["receipts"]) == 1
    assert set(feeds(store, ns)) == set(before) and episodes(store, ns) == staged + 2


def test_rename_seen_from_another_mount_is_still_a_rename(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    container = tmp_path / "sessions" / "claude"
    project(container)
    drain(service, ns, [container])
    before, staged = feeds(store, ns), episodes(store, ns)
    # Remounted AND a directory renamed: stored paths exist nowhere, keys point at the old name.
    host = tmp_path / "home" / ".claude" / "projects"
    host.parent.mkdir(parents=True)
    container.rename(host)
    (host / "-Users-rob-atlas").rename(host / "-Users-rob-atlas-v2")
    assert census(store, ns, [host])["unstaged_episodes"] == 0
    assert follow_once(service, ns, [host], {}, source_records=True) == []
    assert set(feeds(store, ns)) == set(before) and episodes(store, ns) == staged


def test_a_copy_beside_the_original_is_refused_under_the_lowest_feed_id(
    graph, tmp_path, monkeypatch
):
    store, ns = graph
    service = MemoryService(store)
    root = tmp_path / "sessions" / "claude"
    session, journal = project(root)
    drain(service, ns, [root])
    for number in (1, 2):
        copy = root / f"copy-{number}"
        copy.mkdir()
        shutil.copy(session, copy / UUID)
    shutil.copy(journal, root / "copy-1" / "journal.jsonl")
    # Two feeds under one unique name, accepted on purpose, then a third copy arrives.
    monkeypatch.setenv("MEMORY_FEED_ACCEPT_UNMATCHED", "1")
    (root / "copy-2" / UUID).unlink()
    drain(service, ns, [root])
    monkeypatch.delenv("MEMORY_FEED_ACCEPT_UNMATCHED")
    before = feeds(store, ns)
    same = sorted(fid for fid, f in before.items() if f["source_uri"].endswith(UUID))
    assert len(before) == 4 and len(same) == 2
    shutil.copy(session, root / "copy-2" / UUID)
    for _ in range(3):  # Whatever order the index was built in.
        fed = follow_once(service, ns, [root], {}, source_records=True)
        assert [(f["status"], f["known_feed_id"]) for f in fed] == [
            ("feed_identity_refused", same[0])
        ]
    assert feeds(store, ns) == before


def test_an_impostor_under_a_vanished_name_is_not_adopted(graph, tmp_path):
    store, ns = graph
    service = MemoryService(store)
    root = tmp_path / "sessions" / "claude"
    session, journal = project(root)
    drain(service, ns, [root])
    before, staged = feeds(store, ns), episodes(store, ns)
    shutil.rmtree(session.parent)
    impostor, unrelated = project(root, "-Users-rob-elsewhere")
    impostor.write_text(claude("user", "A different conversation under the same uuid."))
    unrelated.write_text(claude("user", "A workflow that shares only its file name."))
    fed = follow_once(service, ns, [root], {}, source_records=True)
    # The unique name is tried against its feed and rejected by content; the shared
    # name was never a claim, so that file is simply new.
    assert [(Path(f["source"]).name, f.get("status"), f.get("error")) for f in fed] == [
        (UUID, "failed", "ValueError"),
        ("journal.jsonl", None, None),
    ]
    after = feeds(store, ns)
    assert {fid: after[fid] for fid in before} == before
    assert len(after) == len(before) + 1 and episodes(store, ns) == staged + 1
    assert census(store, ns, [root])["gaps"]["prefix_mismatches"] == 1
