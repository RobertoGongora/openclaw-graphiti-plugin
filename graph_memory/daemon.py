"""Independent memory-bank scanner and durable queue consumers. No MCP caller needed."""

import json
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .diagnostics import diagnostic
from .feeds import worker_tick
from .follow import follow_once
from .importers import memory_files, transcripts
from .store import digest


def source_content(payload):
    # Filesystem birthtime is not portable between macOS and Linux. A touch or
    # mount change must not re-import identical evidence. Keep the first recorded
    # dates in Neo4j; changed message content remains a new immutable version.
    return digest(
        {
            k: v
            for k, v in payload.items()
            if k
            not in (
                "source_created_at",
                "source_updated_at",
            )
        }
    )


def scan_bank(service, namespace, roots: list[Path], seen: dict):
    """Atomically stage each file's new content; adopt existing CLI imports on restart."""
    result = {"files": 0, "changed_files": 0, "staged": 0, "existing": 0, "failures": []}
    # Enumerate separately so an unavailable mount doesn't block healthy roots.
    files = set()
    for root in roots:
        try:
            files.update(memory_files([root]))
        except (OSError, ValueError) as exc:
            result["failures"].append({"source": str(root), "error": type(exc).__name__})
    for path in sorted(files):
        result["files"] += 1
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if seen.get(str(path)) == stamp:
                continue
            batches = list(transcripts(path, namespace))
            after = path.stat()
            if stamp != (after.st_mtime_ns, after.st_size, after.st_ino):
                raise ValueError("Source changed during read; retry on next scan")

            def stage_file(tx, batches=batches):
                service.store.lock(tx, namespace)
                rows = (
                    tx.run(
                        "MATCH (e:MemoryEpisode {namespace:$ns}) WHERE e.session_id=$session "
                        "RETURN e.payload AS payload",
                        ns=namespace,
                        session=batches[0].session_id,
                    ).data()
                    if batches
                    else []
                )
                known = {source_content(json.loads(row["payload"])) for row in rows}
                staged = existing = 0
                for transcript in batches:
                    fingerprint = source_content(transcript.model_dump(mode="json"))
                    if fingerprint in known:
                        existing += 1
                        continue
                    service.store.stage(transcript, transaction=tx)
                    known.add(fingerprint)
                    staged += 1
                return staged, existing

            staged, existing = service.store.transaction(stage_file)
            result["changed_files"] += 1
            result["staged"] += staged
            result["existing"] += existing
            seen[str(path)] = stamp
        except Exception as exc:
            # Do not mark failed input as seen; retry without logging source text.
            result["failures"].append({"source": str(path), "error": type(exc).__name__})
    return result


def emit(event, **data):
    print(json.dumps({"event": event, **data}), flush=True)


def run_daemon(service, namespace, roots, transcript_roots=(), workers=4, interval=30, once=False):
    if service.llm is None:
        raise ValueError("daemon requires MEMORY_LLM=codex or compatible")
    if not 1 <= workers <= 16 or interval < 1:
        raise ValueError("workers must be 1..16 and interval at least 1 second")
    seen, feed_seen = {}, {}
    stop = threading.Event()

    def consume(number):
        while not stop.is_set():
            try:
                result = worker_tick(service, namespace, limit=1)
                for receipt in result["receipts"]:
                    emit(
                        "processed",
                        worker=number,
                        episode_id=receipt["episode_id"],
                        status=receipt["status"],
                        error=receipt.get("error"),
                        **{
                            key: receipt[key]
                            for key in ("diagnostic", "failed_attempts", "retry_after")
                            if key in receipt
                        },
                    )
            except Exception as exc:
                emit(
                    "worker_error",
                    worker=number,
                    error=type(exc).__name__,
                    diagnostic=diagnostic(exc),
                )
                stop.wait(interval)
            if once:
                return
            stop.wait(1)

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            while not stop.is_set():
                result = scan_bank(service, namespace, roots, seen)
                emit("bank_scan", **result)
                if transcript_roots:
                    try:
                        feeds = follow_once(service, namespace, transcript_roots, feed_seen)
                        emit("transcript_scan", feeds=feeds)
                    except Exception as exc:
                        emit("transcript_scan_error", error=type(exc).__name__)
                if not futures:
                    futures = [pool.submit(consume, i + 1) for i in range(workers)]
                if once:
                    for future in futures:
                        future.result()
                    return result
                stop.wait(interval)
            emit("draining", message="Finishing active jobs before exit")
    finally:
        stop.set()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
