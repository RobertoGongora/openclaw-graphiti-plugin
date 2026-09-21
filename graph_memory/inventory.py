"""Periodic source census. Stores operational metadata, never stages or extracts facts."""

import json
import os
import signal
import threading
from pathlib import Path

from .diagnostics import diagnostic
from .feed_identity import Feeds, KnownElsewhere, validate_roots
from .models import now
from .session_sources import FORMAT, MAX_BATCH_CHARS, records
from .store import digest


def inventory_id(namespace):
    return digest(["transcript-inventory-v1", namespace])


def remaining_batches(messages, count):
    # Same eight-new-chunk / character bounds as feed_records; context adds no episodes.
    batches = 0
    while count < len(messages):
        end, size = count, 0
        while end < len(messages) and end - count < 8:
            length = len(messages[end].content)
            if end > count and size + length > MAX_BATCH_CHARS:
                break
            size += length
            end += 1
        count = end
        batches += 1
    return batches


def census(store, namespace, roots, stop=None):
    """Compare a cursor snapshot with mounted files; gaps make totals partial."""
    started = now().isoformat()
    cursors = store.transaction(
        lambda tx: {
            row["id"]: row["cursor"]
            for row in tx.run(
                "MATCH (f:MemoryFeed {namespace:$ns}) "
                "RETURN f.id AS id, f {.message_count, .prefix_hash} AS cursor",
                ns=namespace,
            )
        }
    )
    counts = dict(
        files=0,
        files_counted=0,
        files_with_unstaged=0,
        files_caught_up=0,
        unstaged_chunks=0,
        unstaged_episodes=0,
    )
    gaps = dict(
        inaccessible_roots=0,
        traversal_errors=0,
        unreadable_files=0,
        parse_errors=0,
        prefix_mismatches=0,
        changed_files=0,
        partial_files=0,
        identity_refused=0,
    )
    errors = []

    def failure(exc):
        if len(errors) < 5:
            errors.append(diagnostic(exc, stage="inventory"))

    def traversal_error(exc):
        gaps["traversal_errors"] += 1
        failure(exc)

    # The watcher's resolution, so a remounted root is not counted as a new backlog.
    feeds = Feeds(store, namespace, roots)
    if feeds.blocked:
        # Every file would read as unstaged; the watcher refuses to feed in this state.
        return {
            "state": "identity_blocked",
            "started_at": started,
            "finished_at": now().isoformat(),
            "source_format": FORMAT,
            "identity": feeds.blocked,
            "basis": "Older feeds are stored under paths outside the mounted roots, so files cannot be matched to their cursors and no backlog is estimated. Stamp the feeds with the roots they were written under.",
        }
    files = {}
    for root in validate_roots(roots):
        if root.is_file():
            files.setdefault(*root.key(root.given))
        elif root.given.is_dir():
            for directory, _, names in os.walk(root.given, onerror=traversal_error):
                for n in names:
                    if n.endswith(".jsonl"):
                        files.setdefault(*root.key(Path(directory) / n))
        else:
            gaps["inaccessible_roots"] += 1
    counts["files"] = len(files)
    for name, key in sorted(files.items()):
        if stop and stop.is_set():
            return None  # Keep the last finished snapshot on shutdown.
        path = Path(name)
        try:
            before = path.stat()
            messages = list(records(path))
            try:
                cursor = cursors.get(feeds.resolve(key, name).feed_id, {})
            except KnownElsewhere:
                gaps["identity_refused"] += 1
                continue
            count = cursor.get("message_count", 0)
            if count > len(messages) or (
                count
                and digest([m.model_dump(mode="json") for m in messages[:count]])
                != cursor["prefix_hash"]
            ):
                gaps["prefix_mismatches"] += 1
                continue
            with path.open("rb") as stream:
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    gaps["partial_files"] += stream.read(1) != b"\n"
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                gaps["changed_files"] += 1
                continue
            counts["files_counted"] += 1
            counts["files_with_unstaged"] += count < len(messages)
            counts["files_caught_up"] += count == len(messages)
            counts["unstaged_chunks"] += len(messages) - count
            counts["unstaged_episodes"] += remaining_batches(messages, count)
        except OSError as exc:
            gaps["unreadable_files"] += 1
            failure(exc)
        except Exception as exc:
            gaps["parse_errors"] += 1
            failure(exc)
    return {
        "state": "partial" if any(gaps.values()) else "available",
        "started_at": started,
        "finished_at": now().isoformat(),
        "source_format": FORMAT,
        **counts,
        "gaps": gaps,
        "diagnostics": errors,
        "basis": "Mounted transcripts at scan time against cursors at scan start. Unstaged counts exclude unreadable, invalid, rewritten or changing files; incomplete trailing records are not counted. Active ingestion and new messages can change the backlog after this snapshot.",
    }


def save_inventory(store, namespace, snapshot, interval):
    payload = {**snapshot, "refresh_interval_seconds": interval}
    store.transaction(
        lambda tx: tx.run(
            "MERGE (i:MemoryInventory {id:$id}) "
            "SET i.namespace=$ns, i.name='Transcript intake inventory', i.payload=$payload",
            id=inventory_id(namespace),
            ns=namespace,
            payload=json.dumps(payload),
        ).consume()
    )
    return payload


def run_inventory(store, namespace, roots, interval=300, once=False):
    if interval < 1 or not roots:
        raise ValueError("Inventory requires transcript roots and a positive interval")
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            started = now().isoformat()
            try:
                snapshot = census(store, namespace, roots, stop)
            except Exception as exc:
                snapshot = {
                    "state": "unavailable",
                    "started_at": started,
                    "finished_at": now().isoformat(),
                    "diagnostics": [diagnostic(exc, stage="inventory")],
                }
            if snapshot is None:
                return
            payload = save_inventory(store, namespace, snapshot, interval)
            print(json.dumps({"event": "transcript_inventory", **payload}), flush=True)
            if once:
                return payload
            stop.wait(interval)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
