"""Shared-graph registration, preserving real Neo4j feed and episode state."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_memory import cli, feed_identity
from graph_memory.follow import follow_once
from graph_memory.inventory import census
from graph_memory.session_sources import feed_records
from tests.test_feed_identity import episodes, feeds
from tests.test_session_sources import claude

ACK = "MEMORY_FEED_NEW_SOURCE_LABELS"


@pytest.fixture(autouse=True)
def identity_environment(monkeypatch):
    for name in (ACK, "MEMORY_FEED_ACCEPT_UNMATCHED", "MEMORY_FEED_REMOTE_RECEIVER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("MEMORY_INTAKE_QUEUE", "100")
    monkeypatch.setenv("MEMORY_INTAKE_FILES", "10")


@pytest.fixture
def intake(graph):
    store, ns = graph
    return SimpleNamespace(store=store), ns


def source(root, filename="journal.jsonl", content="Mac A uses MySQL."):
    path = root / "workflow" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(claude("user", content))
    return path


def scan(service, ns, root, label, seen=None):
    return follow_once(service, ns, [Path(f"{label}={root}")], {} if seen is None else seen, True)


def snapshot(service, ns):
    return feeds(service.store, ns), episodes(service.store, ns)


@pytest.mark.parametrize("old", ["claude", "rob-mbp.claude"])
def test_new_label_on_known_paths_blocks_follow_and_inventory(intake, tmp_path, monkeypatch, old):
    service, ns = intake
    root = tmp_path / "projects"
    source(root)
    source(root, "0138dd13-1c43-4517-84bc-416fc7aac737.jsonl")
    assert len(scan(service, ns, root, old)) == 2
    before = snapshot(service, ns)
    monkeypatch.setenv("NEO4J_URI", "bolt://receiver:7687")
    new = "rob-macbook.claude"
    seen = {}
    for _ in range(2):
        inventory = census(service.store, ns, [f"{new}={root}"])
        result = scan(service, ns, root, new, seen)
        assert snapshot(service, ns) == before
        assert inventory["state"] == "identity_blocked"
        assert "unstaged_episodes" not in inventory
        assert [r.get("status") for r in result] == ["feed_identity_blocked"]
        assert "feeds relabel" in result[0]["action"] and ACK in result[0]["action"]
        assert snapshot(service, ns) == before
        assert not seen  # No stale seed: an administrative relabel can unblock the next scan.


@pytest.mark.parametrize("mistyped_target", [False, True])
@pytest.mark.parametrize(
    ("consumer", "filename"),
    [
        ("claude", "0138dd13-1c43-4517-84bc-416fc7aac737.jsonl"),
        ("codex", "rollout-2026-09-16T10-20-30-0138dd13-1c43-4517-84bc-416fc7aac737.jsonl"),
    ],
)
def test_docker_cutover_blocks_missed_relabel_and_resumes_after_fix(
    intake, tmp_path, monkeypatch, mistyped_target, consumer, filename
):
    service, ns = intake
    mounted = tmp_path / "sessions" / consumer
    source(mounted, filename)
    source(mounted)  # The unique session must also protect generic files in this root.
    assert len(scan(service, ns, mounted, consumer)) == 2
    new_label = f"rob-mbp.{consumer}"
    old_label = consumer
    if mistyped_target:
        old_label = new_label + "-typo"
        assert (
            feed_identity.relabel(service.store, ns, consumer, old_label, True)["relabelled"] == 2
        )
    before, staged = snapshot(service, ns)
    host = tmp_path / "Users" / "rob" / f".{consumer}" / "projects"
    host.parent.mkdir(parents=True)
    mounted.rename(host)
    monkeypatch.setenv("MEMORY_FEED_REMOTE_RECEIVER", "1")
    seen = {}
    for _ in range(2):
        inventory = census(service.store, ns, [f"{new_label}={host}"])
        result = scan(service, ns, host, new_label, seen)
        assert inventory["state"] == "identity_blocked"
        assert "unstaged_episodes" not in inventory
        assert result[0]["status"] == "feed_identity_blocked"
        assert result[0]["new_source_labels"] == {new_label: [old_label]}
        assert snapshot(service, ns) == (before, staged)
        assert not seen
    # Correcting the administrative relabel unblocks this same follower without a restart.
    assert feed_identity.relabel(service.store, ns, old_label, new_label, True)["relabelled"] == 2
    assert scan(service, ns, host, new_label, seen) == []
    assert census(service.store, ns, [f"{new_label}={host}"])["unstaged_episodes"] == 0
    assert set(feeds(service.store, ns)) == set(before)
    assert episodes(service.store, ns) == staged
    path = host / "workflow" / filename
    path.write_text(path.read_text() + claude("user", "A new message after the cutover."))
    appended = scan(service, ns, host, new_label, seen)
    assert len(appended) == 1 and appended[0]["feed_id"] in before
    assert set(feeds(service.store, ns)) == set(before)
    assert episodes(service.store, ns) == staged + 1


@pytest.mark.parametrize("same_session", [False, True])
def test_new_machine_relative_session_registration(intake, tmp_path, monkeypatch, same_session):
    service, ns = intake
    monkeypatch.setenv("MEMORY_FEED_REMOTE_RECEIVER", "1")
    first = tmp_path / "first" / "projects"
    second = tmp_path / "second" / "projects"
    filename = "0138dd13-1c43-4517-84bc-416fc7aac737.jsonl"
    source(first, filename)
    assert len(scan(service, ns, first, "rob-mbp.claude")) == 1
    before, staged = snapshot(service, ns)
    source(second, filename if same_session else filename.replace("0138dd13", "0138dd14"))
    roots = [f"rob-mini.claude={second}"]
    if same_session:
        assert census(service.store, ns, roots)["state"] == "identity_blocked"
        assert scan(service, ns, second, "rob-mini.claude")[0]["status"] == "feed_identity_blocked"
        assert snapshot(service, ns) == (before, staged)
        monkeypatch.setenv(ACK, "rob-mini.claude")
    assert census(service.store, ns, roots)["unstaged_episodes"] == 1
    result = scan(service, ns, second, "rob-mini.claude")
    assert len(result) == 1 and result[0]["feed_id"] not in before
    found, count = snapshot(service, ns)
    assert count == staged + 1 and {fid: found[fid] for fid in before} == before
    monkeypatch.delenv(ACK, raising=False)
    assert scan(service, ns, second, "rob-mini.claude") == []


@pytest.mark.parametrize("same_path", [True, False])
def test_new_machine_ack_is_narrow_and_both_hosts_resume(intake, tmp_path, monkeypatch, same_path):
    service, ns = intake
    monkeypatch.setenv("NEO4J_URI", "bolt://receiver:7687")
    root = tmp_path / "a" / "projects"
    a = source(root)
    original = a.read_text()
    assert len(scan(service, ns, root, "rob-mbp.claude")) == 1
    before, staged = snapshot(service, ns)
    other = root if same_path else tmp_path / "b" / "projects"
    b = source(other, content="Mac B uses Postgres.")
    second = b.read_text()
    if same_path:
        monkeypatch.setenv(ACK, "rob-mini.claude-extra")
        assert scan(service, ns, other, "rob-mini.claude")[0]["status"] == "feed_identity_blocked"
        monkeypatch.setenv(ACK, "rob-mini.claude")
    inventory = census(service.store, ns, [f"rob-mini.claude={other}"])
    assert (inventory["state"], inventory["unstaged_episodes"]) == ("available", 1)
    result = scan(service, ns, other, "rob-mini.claude")
    assert len(result) == 1 and len(result[0]["receipts"]) == 1
    new_id = result[0]["feed_id"]
    found, count = snapshot(service, ns)
    assert len(found) == 2 and count == staged + 1
    assert new_id not in before and {fid: found[fid] for fid in before} == before
    monkeypatch.delenv(ACK, raising=False)
    assert scan(service, ns, other, "rob-mini.claude") == []
    # Each host resumes its own cursor after the transient acknowledgement is gone.
    a.write_text(original + claude("user", "Mac A adds a note."))
    result = scan(service, ns, root, "rob-mbp.claude")
    assert len(result) == 1 and result[0]["feed_id"] in before
    b.write_text(second + claude("user", "Mac B adds a note."))
    result = scan(service, ns, other, "rob-mini.claude")
    assert len(result) == 1 and result[0]["feed_id"] == new_id
    assert len(feeds(service.store, ns)) == 2 and episodes(service.store, ns) == staged + 3


@pytest.mark.parametrize("same_path", [True, False])
def test_shared_unkeyed_feeds_block_without_auto_stamping(intake, tmp_path, monkeypatch, same_path):
    service, ns = intake
    root = tmp_path / "projects"
    path = source(root)
    feed_records(service, ns, path, "legacy")
    before = snapshot(service, ns)
    other = root if same_path else tmp_path / "other"
    if not same_path:
        source(other)
    monkeypatch.setenv("MEMORY_FEED_REMOTE_RECEIVER", "1")
    # The narrow new-machine acknowledgement cannot bypass unidentified legacy feeds.
    monkeypatch.setenv(ACK, "rob-mini.claude")
    roots = [f"rob-mini.claude={other}"]
    inventory = census(service.store, ns, roots)
    result = scan(service, ns, other, "rob-mini.claude")
    assert snapshot(service, ns) == before
    assert inventory["state"] == "identity_blocked"
    assert [r.get("status") for r in result] == ["feed_identity_blocked"]
    assert "feeds stamp" in result[0]["action"]
    assert snapshot(service, ns) == before
    # Explicit administration can identify the owner and unblock its next scan.
    assert (
        feed_identity.stamp_existing(service.store, ns, [f"rob-mbp.claude={root}"])["stamped"] == 1
    )
    assert scan(service, ns, root, "rob-mbp.claude") == []
    found, count = snapshot(service, ns)
    assert set(found) == set(before[0]) and count == before[1]
    assert {f["source_key"] for f in found.values()} == {"rob-mbp.claude:workflow/journal.jsonl"}


@pytest.mark.parametrize("remote", [False, True])
def test_same_label_remount_keeps_cursor(intake, tmp_path, monkeypatch, remote):
    service, ns = intake
    if remote:
        monkeypatch.setenv("NEO4J_URI", "bolt://receiver:7687")
    root = tmp_path / "before"
    source(root)
    assert len(scan(service, ns, root, "rob-mbp.claude")) == 1
    before = snapshot(service, ns)
    moved = tmp_path / "after"
    root.rename(moved)
    assert scan(service, ns, moved, "rob-mbp.claude") == []
    assert census(service.store, ns, [f"rob-mbp.claude={moved}"])["unstaged_episodes"] == 0
    assert snapshot(service, ns) == before


def test_local_follow_still_stamps_legacy(intake, tmp_path):
    service, ns = intake
    root = tmp_path / "projects"
    path = source(root)
    feed_records(service, ns, path, "legacy")
    before, count = snapshot(service, ns)
    assert scan(service, ns, root, "claude") == []
    found, after = snapshot(service, ns)
    assert set(found) == set(before) and after == count
    assert {f["source_key"] for f in found.values()} == {"claude:workflow/journal.jsonl"}


@pytest.mark.parametrize("mode", ["remote", "receiver"])
def test_unkeyed_feed_cli_rejects_before_connecting(monkeypatch, tmp_path, capsys, mode):
    if mode == "remote":
        monkeypatch.setenv("NEO4J_URI", "bolt+ssc://receiver:7687")
    else:
        monkeypatch.setenv("MEMORY_FEED_REMOTE_RECEIVER", "1")
    calls = []
    monkeypatch.setattr(cli, "build_service", lambda: calls.append(True))
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "graph-memory",
            "feed",
            str(tmp_path / "one.jsonl"),
            "--session-id",
            "one",
            "--source-records",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and calls == []
    assert "follow LABEL=PATH --source-records --once" in capsys.readouterr().err


@pytest.mark.parametrize("apply", [False, True])
def test_zero_match_relabel_cli_exit(monkeypatch, capsys, apply):
    monkeypatch.setattr(
        cli, "build_service", lambda: SimpleNamespace(store=SimpleNamespace(close=lambda: None))
    )
    monkeypatch.setattr(
        feed_identity, "relabel", lambda *args: dict(feeds=0, conflicts=0, relabelled=0)
    )
    argv = ["graph-memory", "feeds", "relabel", "--from", "claud", "--to", "rob-mbp.claude"]
    monkeypatch.setattr(cli.sys, "argv", argv + (["--apply"] if apply else []))
    if apply:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
    else:
        cli.main()
    assert json.loads(capsys.readouterr().out) == dict(feeds=0, conflicts=0, relabelled=0)


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "rob-mini.*",
        "rob-mini.claude,",
        ",rob-mini.claude",
        "rob-mini.claude,,rob-mini.codex",
        "rob mini.claude",
        "label:path",
    ],
)
def test_malformed_acknowledgement_rejects_before_connecting(monkeypatch, tmp_path, value):
    monkeypatch.setenv(ACK, value)
    calls = []
    monkeypatch.setattr(cli, "build_service", lambda: calls.append(True))
    monkeypatch.setattr(
        cli.sys, "argv", ["graph-memory", "follow", "--once", f"rob-mini.claude={tmp_path}"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and calls == []


def test_acknowledgement_parses_exact_labels(monkeypatch):
    assert feed_identity.new_source_labels() == set()
    monkeypatch.setenv(ACK, " rob-mini.claude, rob-mini.codex,rob-mini.claude ")
    assert feed_identity.new_source_labels() == {"rob-mini.claude", "rob-mini.codex"}
    monkeypatch.setenv("NEO4J_URI", "bolt://receiver:7687")
    monkeypatch.setenv(ACK, "claude")
    with pytest.raises(ValueError, match="machine-qualified"):
        feed_identity.new_source_labels()


@pytest.mark.parametrize("file_root", [False, True])
@pytest.mark.parametrize("stored_path", ["given", "resolved"])
def test_registration_checks_given_and_resolved_roots(
    monkeypatch, tmp_path, file_root, stored_path
):
    monkeypatch.setenv("NEO4J_URI", "bolt://receiver:7687")
    real = tmp_path / "real"
    path = source(real)
    link = tmp_path / "mounted"
    link.symlink_to(path if file_root else real)
    given = link if file_root else link / "workflow" / path.name
    uri = str(given if stored_path == "given" else given.resolve())
    rows = [
        {"id": "old", "session": "old", "key": "other.claude:workflow/journal.jsonl", "uri": uri}
    ]
    store = SimpleNamespace(read=lambda *_: rows)
    resolver = feed_identity.Feeds(store, "test", [f"new.claude={link}"])
    assert resolver.blocked["new_source_labels"] == {"new.claude": ["other.claude"]}


def test_registration_checks_path_boundaries_and_single_file_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("NEO4J_URI", "bolt://receiver:7687")
    root = tmp_path / "projects"
    path = source(root)
    rows = [
        {
            "id": "old",
            "session": "old",
            "key": "other.claude:workflow/journal.jsonl",
            "uri": str(path),
        }
    ]
    store = SimpleNamespace(read=lambda *_: rows)
    sibling = source(root, filename="different.jsonl")
    assert not feed_identity.Feeds(store, "test", [f"new.claude={sibling}"]).blocked
    rows[0]["uri"] = str(tmp_path / "projects-backup" / "workflow" / path.name)
    assert not feed_identity.Feeds(store, "test", [f"new.claude={root}"]).blocked


@pytest.mark.parametrize("mode", ["directory", "file", "symlink_directory", "symlink_file"])
def test_relative_session_guard_respects_root_and_symlinks(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("MEMORY_FEED_REMOTE_RECEIVER", "1")
    directory = tmp_path / "host"
    directory.mkdir()
    filename = "0138dd13-1c43-4517-84bc-416fc7aac737.jsonl"
    path = directory / filename
    path.write_text(claude("user", "A relocated session."))
    root = path if mode.endswith("file") else directory
    if mode.startswith("symlink"):
        link = directory / "alias.jsonl" if mode.endswith("file") else tmp_path / "mounted"
        link.symlink_to(root, target_is_directory=root.is_dir())
        root = link
    rows = [
        {
            "id": "old",
            "session": "old",
            "key": f"old.claude:{filename}",
            "uri": f"/sessions/claude/{filename}",
        }
    ]
    store = SimpleNamespace(read=lambda *_: rows)
    roots = [f"new.claude={root}"]
    assert feed_identity.Feeds(store, "test", roots).blocked["new_source_labels"] == {
        "new.claude": ["old.claude"]
    }
    if mode.endswith("file"):
        sibling = directory / filename.replace("0138dd13", "0138dd14")
        sibling.write_text(path.read_text())
        rows[0]["key"] = f"old.claude:{sibling.name}"
        assert not feed_identity.Feeds(store, "test", roots).blocked


@pytest.mark.parametrize("absolute", [False, True])
def test_relative_session_guard_does_not_follow_keys_outside_root(monkeypatch, tmp_path, absolute):
    monkeypatch.setenv("MEMORY_FEED_REMOTE_RECEIVER", "1")
    root = tmp_path / "host"
    root.mkdir()
    outside = tmp_path / "0138dd13-1c43-4517-84bc-416fc7aac737.jsonl"
    outside.write_text(claude("user", "Outside the watched root."))
    relative = str(outside) if absolute else f"../{outside.name}"
    rows = [{"id": "old", "session": "old", "key": f"old.claude:{relative}", "uri": str(outside)}]
    store = SimpleNamespace(read=lambda *_: rows)
    assert not feed_identity.Feeds(store, "test", [f"new.claude={root}"]).blocked


def test_acknowledgement_is_per_label(monkeypatch, tmp_path):
    monkeypatch.setenv("NEO4J_URI", "bolt://receiver:7687")
    one, two = tmp_path / "claude", tmp_path / "codex"
    rows = [
        {
            "id": label,
            "session": label,
            "key": f"other.{label}:workflow/journal.jsonl",
            "uri": str(source(root)),
        }
        for label, root in (("claude", one), ("codex", two))
    ]
    store = SimpleNamespace(read=lambda *_: rows)
    roots = [f"new.claude={one}", f"new.codex={two}"]
    monkeypatch.setenv(ACK, "new.claude")
    assert feed_identity.Feeds(store, "test", roots).blocked["new_source_labels"] == {
        "new.codex": ["other.codex"]
    }
    monkeypatch.setenv(ACK, "new.claude,new.codex")
    assert not feed_identity.Feeds(store, "test", roots).blocked


def test_documented_wrapper_inherits_transient_ack_for_inventory_and_once(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    docs = (Path(__file__).parents[1] / "docs" / "remote-push.md").read_text()
    script = docs.split("```sh\n#!/bin/sh\n# ~/.local/bin/graph-memory-push\n", 1)[1].split(
        "\n```", 1
    )[0]
    wrapper = tmp_path / "push"
    wrapper.write_text("#!/bin/sh\n" + script)
    security = tmp_path / "security"
    security.write_text("#!/bin/sh\nprintf 'test-password\\n'\n")
    binary = tmp_path / "graph-memory"
    binary.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'env': {k: os.environ.get(k) for k in "
        "['NEO4J_URI', 'NEO4J_PASSWORD', 'MEMORY_FEED_REMOTE_RECEIVER', 'MEMORY_FEED_NEW_SOURCE_LABELS']}}))\n"
    )
    security.chmod(0o700)
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv(ACK, "rob-mbp.claude,rob-mbp.codex")
    results = {}
    for mode in ("inventory", "once", "follow", ""):
        result = subprocess.run(
            ["/bin/sh", str(wrapper), *([mode] if mode else [])],
            capture_output=True,
            text=True,
            check=True,
        )
        results[mode] = json.loads(result.stdout)
    expected_env = {
        "NEO4J_URI": "bolt://graph-memory.taild00569.ts.net:27687",
        "NEO4J_PASSWORD": "test-password",
        "MEMORY_FEED_REMOTE_RECEIVER": "1",
        ACK: "rob-mbp.claude,rob-mbp.codex",
    }
    for result in results.values():
        assert result["env"] == expected_env
    assert results["once"]["args"][:5] == [
        "--namespace",
        "transcripts",
        "follow",
        "--source-records",
        "--once",
    ]
    assert results["follow"]["args"][:4] == [
        "--namespace",
        "transcripts",
        "follow",
        "--source-records",
    ]
    assert results[""] == results["follow"]
    roots = results["follow"]["args"][4:]
    assert results["once"]["args"][5:] == roots
    assert results["inventory"]["args"] == [
        "--namespace",
        "transcripts",
        "inventory",
        "--once",
        "--transcripts",
        roots[0],
        "--transcripts",
        roots[1],
    ]
    bad = subprocess.run(["/bin/sh", str(wrapper), "bogus"], capture_output=True, text=True)
    assert bad.returncode == 2
    monkeypatch.delenv(ACK)
    normal = subprocess.run(["/bin/sh", str(wrapper)], capture_output=True, text=True, check=True)
    assert json.loads(normal.stdout)["env"][ACK] is None
