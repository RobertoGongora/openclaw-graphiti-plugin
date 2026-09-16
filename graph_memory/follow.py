"""Host-neutral watcher for append-only Claude/Codex JSONL session directories."""

import time
from pathlib import Path

from .feeds import feed
from .store import digest


def follow_once(service, namespace, roots: list[Path], seen: dict):
    outputs = []
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
        for path in paths:
            path = path.resolve()
            stat = path.stat()
            version = (stat.st_mtime_ns, stat.st_size)
            if seen.get(str(path)) == version:
                continue
            try:
                result = feed(service, namespace, path, "host:" + digest(str(path)))
            except Exception as exc:
                # Keep unseen so a transient database/partial source failure is retried.
                outputs.append(
                    {"source": str(path), "status": "failed", "error": type(exc).__name__}
                )
            else:
                seen[str(path)] = version
                if result["receipts"]:
                    outputs.append({"source": str(path), **result})
    return outputs


def follow_loop(service, namespace, roots, interval=5):
    import json

    seen = {}
    while True:
        results = follow_once(service, namespace, roots, seen)
        if results:
            print(json.dumps({"feeds": results}), flush=True)
        time.sleep(interval)
