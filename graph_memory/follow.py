"""Host-neutral watcher for append-only Claude/Codex JSONL session directories."""

import time
from pathlib import Path

from .feeds import feed
from .store import digest


def follow_once(service, namespace, roots: list[Path], seen: dict, source_records=False):
    outputs = []
    examined = 0
    if source_records:
        queued = service.store.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns,status:'pending'}) RETURN count(e) AS n",
                ns=namespace,
            ).single()["n"]
        )
        if queued >= 32:
            return outputs  # Leave unread source on disk until the durable queue drains.
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
        for path in paths:
            path = path.resolve()
            stat = path.stat()
            version = (stat.st_mtime_ns, stat.st_size)
            if seen.get(str(path)) == version:
                continue
            if source_records and examined >= 4:
                return outputs
            examined += 1
            try:
                if source_records:
                    from .session_sources import feed_records

                    result = feed_records(service, namespace, path, "host:" + digest(str(path)))
                else:
                    result = feed(service, namespace, path, "host:" + digest(str(path)))
            except Exception as exc:
                # Keep unseen so a transient database/partial source failure is retried.
                outputs.append(
                    {"source": str(path), "status": "failed", "error": type(exc).__name__}
                )
            else:
                if result.get("caught_up", True):
                    seen[str(path)] = version
                if result["receipts"]:
                    outputs.append({"source": str(path), **result})
    return outputs


def follow_loop(service, namespace, roots, interval=5, source_records=False):
    import json

    seen = {}
    while True:
        results = follow_once(service, namespace, roots, seen, source_records)
        if results:
            print(json.dumps({"feeds": results}), flush=True)
        time.sleep(interval)
