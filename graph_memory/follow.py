"""Host-neutral watcher for append-only Claude/Codex JSONL session directories."""

import time
from pathlib import Path

from . import settings
from .feeds import feed
from .store import digest

CURSOR, SEEDED = "\0cursor", "\0seeded"


def seed_seen(service, namespace, seen):
    """Restore which files were fully fed, so a restart does not reparse them all."""
    rows = service.store.read(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {namespace:$ns}) WHERE f.caught_up_size IS NOT NULL "
            "RETURN f.source_uri AS path,f.caught_up_mtime_ns AS mtime,f.caught_up_size AS size",
            ns=namespace,
        ).data()
    )
    for row in rows:
        seen.setdefault(row["path"], (row["mtime"], row["size"]))
    seen[SEEDED] = True


def follow_once(service, namespace, roots: list[Path], seen: dict, source_records=False):
    outputs = []
    examined = queued = 0
    if source_records:
        queued = service.store.read(
            lambda tx: tx.run(
                # Work that is due now, whatever its last outcome: a failed episode
                # whose retry time has come is queue depth too.
                "MATCH (e:MemoryEpisode {namespace:$ns}) WHERE e.status IN ['pending','failed'] "
                "AND e.quarantine_engine IS NULL AND coalesce(e.retry_after,0)<=$now "
                "RETURN count(e) AS n",
                ns=namespace,
                now=time.time(),
            ).single()["n"]
        )
        if queued >= settings.intake_queue():
            return outputs  # Leave unread source on disk until the durable queue drains.
        if SEEDED not in seen:
            seed_seen(service, namespace, seen)
    paths = set()
    for root in roots:
        found = [root] if root.is_file() else root.rglob("*.jsonl")
        paths.update(str(path.resolve()) for path in found)
    paths = sorted(paths)
    # Resume after the last file examined: files that keep changing must not
    # spend every scan's budget ahead of the files behind them.
    cursor = seen.get(CURSOR)
    if cursor is not None:
        paths = [p for p in paths if p > cursor] + [p for p in paths if p <= cursor]
    started = time.monotonic()
    for name in paths:
        path = Path(name)
        try:
            stat = path.stat()
        except OSError:
            continue  # Removed since listing, or a dangling link; not worth a whole scan.
        version = (stat.st_mtime_ns, stat.st_size)
        if seen.get(name) == version:
            continue
        if source_records and (
            examined >= settings.intake_files()
            or queued >= settings.intake_queue()
            or time.monotonic() - started > 120
        ):
            return outputs
        seen[CURSOR] = name
        try:
            if source_records:
                from .session_sources import feed_records

                result = feed_records(service, namespace, path, "host:" + digest(name))
            else:
                result = feed(service, namespace, path, "host:" + digest(name))
        except Exception as exc:
            # Keep unseen so a transient database/partial source failure is retried.
            examined += 1
            outputs.append({"source": name, "status": "failed", "error": type(exc).__name__})
        else:
            if result.get("caught_up", True):
                seen[name] = version
                if source_records:
                    service.store.transaction(
                        lambda tx, result=result, version=version: tx.run(
                            "MATCH (f:MemoryFeed {id:$id}) "
                            "SET f.caught_up_mtime_ns=$mtime,f.caught_up_size=$size",
                            id=result["feed_id"],
                            mtime=version[0],
                            size=version[1],
                        ).consume()
                    )
            # Only confirming that a file is already fed is free, so a restart cannot
            # starve the queue; any file that staged work or has more left is budgeted.
            if result["receipts"] or not result.get("caught_up", True):
                examined += 1
            if result["receipts"]:
                queued += len(result["receipts"])
                outputs.append({"source": name, **result})
    return outputs


def follow_loop(service, namespace, roots, interval=5, source_records=False):
    import json

    seen = {}
    while True:
        results = follow_once(service, namespace, roots, seen, source_records)
        if results:
            print(json.dumps({"feeds": results}), flush=True)
        time.sleep(interval)
