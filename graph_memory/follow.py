"""Host-neutral watcher for append-only Claude/Codex JSONL session directories."""

import json
import time
from pathlib import Path

from . import settings
from .feed_identity import KnownElsewhere, session_uid, source_files
from .feeds import feed
from .store import digest

CURSOR, SEEDED, FEEDS = "\0cursor", "\0seeded", "\0feeds"


def seed_seen(service, namespace, roots, files, seen):
    """Restore which files were fully fed, so a restart does not reparse them all.
    Returns why nothing may be fed, if older feeds cannot be told from new files."""
    from .feed_identity import Feeds, stamp_existing

    # A fully fed file is never opened again, so it would never be adopted one at a
    # time: name every older feed now, while its stored path still says where it is.
    stamped = stamp_existing(service.store, namespace, roots)
    if stamped["stamped"]:
        print(json.dumps({"event": "feed_identity", **stamped}), flush=True)
    feeds = Feeds(service.store, namespace, roots)
    if feeds.blocked:
        # Not seeded: the next scan looks again, so stamping from outside unblocks it.
        return {
            "status": "feed_identity_blocked",
            **feeds.blocked,
            "action": "Older feeds are stored under paths outside these roots, so their "
            "files would be fed again as new. Stamp them with the roots they were written "
            "under (graph-memory feeds stamp --root LABEL=STORED_PREFIX), or set "
            "MEMORY_FEED_ACCEPT_UNMATCHED=1 to feed regardless.",
        }
    feeds.rekey(service.store, files)
    rows = service.store.read(
        lambda tx: tx.run(
            "MATCH (f:MemoryFeed {namespace:$ns}) WHERE f.caught_up_size IS NOT NULL "
            "RETURN f.source_key AS key,f.source_uri AS uri,"
            "f.caught_up_mtime_ns AS mtime,f.caught_up_size AS size",
            ns=namespace,
        ).data()
    )
    for row in rows:
        for mark in (row["key"], row["uri"]):
            if mark:
                seen.setdefault(mark, (row["mtime"], row["size"]))
    seen[FEEDS] = feeds
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
                "AND coalesce(e.quarantine_engine,'') <> $engine AND coalesce(e.retry_after,0)<=$now "
                "RETURN count(e) AS n",
                ns=namespace,
                now=time.time(),
                engine=service.store.engine,
            ).single()["n"]
        )
        if queued >= settings.intake_queue():
            return outputs  # Leave unread source on disk until the durable queue drains.
    files = source_files(roots)
    if source_records and SEEDED not in seen:
        blocked = seed_seen(service, namespace, roots, files, seen)
        if blocked:
            return [blocked]
    # What is remembered about a file is kept under its source key, not its path:
    # a remount must not make every fed file look unread. The legacy text feed
    # still names its feeds by path, so there a moved file is a new one.
    paths = sorted((key if source_records else name, name) for name, key in files.items())
    # Resume after the last file examined: files that keep changing must not
    # spend every scan's budget ahead of the files behind them.
    cursor = seen.get(CURSOR)
    if cursor is not None:
        paths = [p for p in paths if p[0] > cursor] + [p for p in paths if p[0] <= cursor]
    started = time.monotonic()
    for mark, name in paths:
        path = Path(name)
        try:
            stat = path.stat()
        except OSError:
            continue  # Removed since listing, or a dangling link; not worth a whole scan.
        version = (stat.st_mtime_ns, stat.st_size)
        # A feed not yet named is remembered under the path it was stored with.
        if version in (seen.get(mark), seen.get(name)):
            continue
        if source_records and (
            examined >= settings.intake_files()
            or queued >= settings.intake_queue()
            or time.monotonic() - started > 120
        ):
            return outputs
        seen[CURSOR] = mark
        try:
            if source_records:
                from .session_sources import feed_records

                identity = seen[FEEDS].resolve(mark, name)
                result = feed_records(
                    service,
                    namespace,
                    path,
                    identity.session_id,
                    feed_id=identity.feed_id,
                    source_key=mark,
                    session_uid=None if identity.known else session_uid(path),
                )
                seen[FEEDS].record(identity, name)
            else:
                result = feed(service, namespace, path, "host:" + digest(name))
        except KnownElsewhere as exc:
            # Nothing was read or staged. Said once per version of the file, not per scan.
            seen[mark] = version
            outputs.append(
                {
                    "source": name,
                    "status": "feed_identity_refused",
                    "source_key": mark,
                    "known_feed_id": exc.feed_id,
                    "known_source_key": exc.source_key,
                }
            )
        except Exception as exc:
            # Keep unseen so a transient database/partial source failure is retried.
            examined += 1
            outputs.append({"source": name, "status": "failed", "error": type(exc).__name__})
        else:
            if result.get("caught_up", True):
                seen[mark] = version
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
    seen = {}
    while True:
        results = follow_once(service, namespace, roots, seen, source_records)
        if results:
            print(json.dumps({"feeds": results}), flush=True)
        time.sleep(interval)
