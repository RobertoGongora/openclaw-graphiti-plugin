"""Local CLI: serve, ingest, import, drain the durable inbox, and run dreams."""

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from .importers import memory_files, transcripts
from .llm import configured_llm
from .mcp import Protocol, http_server, stdio
from .models import DreamCreate, DreamRequest, HistoricalScope, Ingest, Latest, Recall, Relation
from .service import MemoryService
from .store import GraphStore


def build_service():
    store = GraphStore(
        os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD"),
        os.environ.get("NEO4J_DATABASE", "neo4j"),
    )
    store.setup()
    return MemoryService(store, configured_llm())


class Version(argparse.Action):
    def __call__(self, parser, *_):
        from . import __version__
        from .version import engine_fingerprint

        print(f"graph-memory {__version__} engine {engine_fingerprint()}")
        parser.exit()


def env_list(name):
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def run_hook(namespace):
    """Instructions must reach the agent even when the database is slow or down."""
    from .feeds import feed, hook

    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    path = payload.get("transcript_path")
    # Without a transcript path the hook formats its output and never touches the service.
    output = hook(None, namespace, {k: v for k, v in payload.items() if k != "transcript_path"})
    instructing = "hookSpecificOutput" in output
    if instructing:
        print(json.dumps(output, indent=2), flush=True)
    service = None
    try:
        if path and Path(path).is_file():
            service = build_service()
            if instructing:
                feed(service, namespace, Path(path), payload["session_id"])
            else:
                output = hook(service, namespace, payload)
    except Exception as exc:
        print(f"graph-memory hook: staging skipped ({type(exc).__name__})", file=sys.stderr)
    finally:
        if service is not None:
            service.store.close()
    if not instructing:
        print(json.dumps(output, indent=2))


def main():
    debug = "--debug" in sys.argv[1:]
    try:
        run()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        if debug:
            raise
        from neo4j.exceptions import AuthError, ServiceUnavailable
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            # Locations and messages only; inputs can hold private content.
            detail = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                for e in exc.errors(include_input=False)
            )
            print(f"graph-memory: invalid input: {detail}", file=sys.stderr)
            raise SystemExit(2) from None
        if isinstance(exc, ValueError):
            print(f"graph-memory: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        if isinstance(exc, (ServiceUnavailable, AuthError)):
            print(f"graph-memory: database unavailable ({type(exc).__name__})", file=sys.stderr)
            raise SystemExit(69) from None
        print(f"graph-memory: {type(exc).__name__}: {exc} (--debug for traceback)", file=sys.stderr)
        raise SystemExit(1) from None


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action=Version, nargs=0, help="Package and engine identity")
    parser.add_argument("--debug", action="store_true", help="Show tracebacks on failure")
    parser.add_argument("--namespace", default=os.environ.get("MEMORY_NAMESPACE", "personal"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--read-only", action="store_true")
    importer = commands.add_parser("import")
    importer.add_argument("paths", nargs="+", type=Path)
    importer.add_argument(
        "--apply",
        action="store_true",
        help="Persist full redacted sources; default is inventory only",
    )
    importer.add_argument("--extract", action="store_true")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--extract", action="store_true")
    recall = commands.add_parser("recall")
    recall.add_argument("query")
    latest = commands.add_parser("latest")
    latest.add_argument("entity")
    latest.add_argument("--relation", choices=[r.value for r in Relation])
    for query_parser in (recall, latest):
        query_parser.add_argument("--as-of")
        cutoffs = query_parser.add_mutually_exclusive_group()
        cutoffs.add_argument("--known-at")
        cutoffs.add_argument("--at-change", type=int)
    history = commands.add_parser("history", help="Inspect and replay the knowledge journal")
    history.add_argument(
        "action", choices=("init", "list", "snapshot", "replay", "verify", "checkpoint")
    )
    history.add_argument(
        "--accept-live",
        action="store_true",
        help="checkpoint: record a graph that differs from its journal as the new truth",
    )
    history.add_argument("--after", type=int, default=-1)
    history.add_argument("--limit", type=int, default=100)
    cutoffs = history.add_mutually_exclusive_group()
    cutoffs.add_argument("--known-at")
    cutoffs.add_argument("--at-change", type=int)
    history.add_argument("--target", help="New replay: namespace for an isolated historical graph")
    worker = commands.add_parser("work")
    worker.add_argument("--limit", type=int, default=10)
    worker.add_argument("--watch", action="store_true")
    worker.add_argument("--interval", type=float, default=5)
    retry = commands.add_parser("retry-quarantined", help="Explicitly retry one paused episode")
    retry.add_argument("episode_id")
    daemon = commands.add_parser("daemon", help="Watch memory banks and process the queue")
    daemon.add_argument("paths", nargs="*", type=Path)
    daemon.add_argument("--transcripts", action="append", type=Path, default=[])
    daemon.add_argument("--workers", type=int, default=4)
    daemon.add_argument("--interval", type=float, default=30)
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument(
        "--source-records",
        action="store_true",
        help="Preserve transcript tools and artifact provenance",
    )
    scanner = commands.add_parser("scan", help="Stage new memory-bank content without extraction")
    scanner.add_argument("paths", nargs="+", type=Path)
    inventory = commands.add_parser(
        "inventory", help="Cache transcript backlog counts without staging"
    )
    inventory.add_argument("--transcripts", action="append", type=Path, required=True)
    inventory.add_argument("--interval", type=float, default=300)
    inventory.add_argument("--once", action="store_true")
    commands.add_parser("hook")
    health = commands.add_parser("health", help="Container health probe; no schema setup")
    health.add_argument("--role", choices=("worker", "inventory", "mcp"), required=True)
    follower = commands.add_parser("follow")
    follower.add_argument("paths", nargs="+", type=Path)
    follower.add_argument("--interval", type=float, default=5)
    follower.add_argument("--once", action="store_true")
    follower.add_argument("--source-records", action="store_true")
    configure = commands.add_parser("claude-config")
    configure.add_argument("directory", type=Path)
    feeder = commands.add_parser("feed")
    feeder.add_argument("path", type=Path)
    feeder.add_argument("--session-id", required=True)
    feeder.add_argument("--source-records", action="store_true")
    dream = commands.add_parser("dream")
    dream.add_argument("query")
    dream.add_argument("--episode", action="append", required=True)
    dream.add_argument(
        "--apply", action="store_true", help="Promote supported inferences after completion"
    )
    call = commands.add_parser("call")
    call.add_argument("tool")
    call.add_argument("arguments", help="JSON object, or @path to a JSON file")
    commands.add_parser("repair")
    revision = commands.add_parser("revision")
    revision.add_argument(
        "action", choices=("create", "build", "diff", "get", "validate", "promote")
    )
    revision.add_argument("--id")
    revision.add_argument("--episode", action="append")
    revision.add_argument("--eval-report", type=Path)
    revision.add_argument("--checks", type=Path)
    revision.add_argument("--accept-diff")
    args = parser.parse_args()
    files = memory_files(args.paths) if args.command == "import" else []
    if args.command == "import" and not args.apply:
        print(
            json.dumps(
                {
                    "files": len(files),
                    "bytes": sum(p.stat().st_size for p in files),
                    "paths": [str(p) for p in files],
                    "applied": False,
                },
                indent=2,
            )
        )
        return
    if args.command == "claude-config":
        from .feeds import write_claude_config

        print(json.dumps(write_claude_config(args.directory, args.namespace), indent=2))
        return
    if args.command == "hook":
        return run_hook(args.namespace)
    if args.command == "health":
        # Runs every few seconds as the container HEALTHCHECK: one line, no traceback.
        try:
            from .health import check

            ok, report = check(args.role, args.namespace)
        except Exception as exc:
            ok, report = False, {"ok": False, "error": type(exc).__name__}
        print(json.dumps(report))
        raise SystemExit(0 if ok else 1)
    # Argument errors must not cost a database connection and schema setup first.
    if args.command == "work":
        if not 1 <= args.limit <= 100:
            parser.error("--limit must be 1..100")
        if args.interval < 1:
            parser.error("--interval must be at least 1 second")
    if args.command == "daemon" and (not 1 <= args.workers <= 16 or args.interval < 1):
        parser.error("--workers must be 1..16 and --interval at least 1 second")
    if args.command == "follow":
        if args.interval < 1:
            parser.error("--interval must be at least 1 second")
        if not all(p.exists() for p in args.paths):
            parser.error("All transcript paths must exist")
    if args.command in ("work", "daemon") and os.environ.get("MEMORY_LLM", "caller") == "caller":
        parser.error(f"{args.command} requires MEMORY_LLM=codex or compatible")
    service = build_service()
    try:
        if args.command == "serve":
            protocol = Protocol(service, namespace=args.namespace, read_only=args.read_only)
            if args.transport == "stdio":

                def leave(*_):
                    raise SystemExit(0)

                signal.signal(signal.SIGTERM, leave)
                stdio(protocol)
            else:
                server = http_server(
                    protocol,
                    args.host,
                    args.port,
                    os.environ.get("MEMORY_HTTP_TOKEN"),
                    env_list("MEMORY_HTTP_ORIGINS"),
                    env_list("MEMORY_HTTP_HOSTS"),
                )
                # shutdown() blocks until serve_forever returns, so it cannot run
                # in the handler, which interrupts that very loop.
                signal.signal(
                    signal.SIGTERM,
                    lambda *_: threading.Thread(target=server.shutdown, daemon=True).start(),
                )
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
            return
        if args.command in ("import", "ingest"):
            outputs, failures = [], []
            for path in files if args.command == "import" else [args.path]:
                try:
                    for transcript in transcripts(path, args.namespace):
                        outputs.append(
                            service.ingest(Ingest(transcript=transcript, extract=args.extract))
                        )
                except Exception as exc:
                    failures.append({"path": str(path), "error": type(exc).__name__})
            result = {"receipts": outputs, "failures": failures}
        elif args.command == "revision":
            from .revisions import Revisions

            manager = Revisions(service)
            if args.action == "create":
                result = manager.create(args.namespace, args.episode)
            elif not args.id:
                parser.error("revision action requires --id")
            elif args.action == "validate":
                if not args.eval_report or not args.checks:
                    parser.error("validate requires --eval-report and --checks")
                result = manager.validate(
                    args.namespace,
                    args.id,
                    json.loads(args.eval_report.read_text()),
                    json.loads(args.checks.read_text()),
                )
            elif args.action == "promote":
                result = manager.promote(args.namespace, args.id, args.accept_diff)
            else:
                result = getattr(manager, args.action)(args.namespace, args.id)
        elif args.command == "recall":
            result = service.call(
                "memory_recall",
                Recall(
                    namespace=args.namespace,
                    query=args.query,
                    as_of=args.as_of,
                    known_at=args.known_at,
                    at_change=args.at_change,
                ).model_dump(mode="json"),
            )
        elif args.command == "latest":
            result = service.call(
                "memory_latest",
                Latest(
                    namespace=args.namespace,
                    entity=args.entity,
                    relation=args.relation,
                    as_of=args.as_of,
                    known_at=args.known_at,
                    at_change=args.at_change,
                ).model_dump(mode="json"),
            )
        elif args.command == "history":
            from .journal import Journal

            journal = Journal(service.store)
            cutoff = HistoricalScope(
                namespace=args.namespace, known_at=args.known_at, at_change=args.at_change
            )
            if args.action == "init":
                result = journal.initialize(args.namespace)
            elif args.action == "verify":
                result = journal.verify(args.namespace)
            elif args.action == "checkpoint":
                result = journal.checkpoint(args.namespace, args.accept_live)
            elif args.action == "list":
                result = {"changes": journal.events(args.namespace, args.after, args.limit)}
            elif args.action == "snapshot":
                result = journal.snapshot(
                    args.namespace, known_at=cutoff.known_at, sequence=cutoff.at_change
                )
            else:
                if not args.target:
                    parser.error("history replay requires --target replay:<name>")
                result = journal.replay(
                    args.namespace, args.target, known_at=cutoff.known_at, sequence=cutoff.at_change
                )
        elif args.command == "scan":
            from .daemon import scan_bank

            result = scan_bank(service, args.namespace, args.paths, {})
        elif args.command == "daemon":
            from .daemon import run_daemon

            result = run_daemon(
                service,
                args.namespace,
                args.paths,
                args.transcripts,
                args.workers,
                args.interval,
                args.once,
                args.source_records,
            )
            if result is None:
                return
        elif args.command == "retry-quarantined":
            result = service.store.retry_quarantined(args.namespace, args.episode_id)
        elif args.command == "work":
            if service.llm is None:
                parser.error("work requires MEMORY_LLM=codex or compatible")
            from .feeds import worker_tick

            stop = threading.Event()
            if args.watch:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: stop.set())
            while True:
                result = worker_tick(service, args.namespace, args.limit)
                if not args.watch:
                    break
                if result["receipts"]:
                    print(json.dumps(result), flush=True)
                if stop.wait(args.interval):
                    return
        elif args.command == "inventory":
            from .inventory import run_inventory

            run_inventory(service.store, args.namespace, args.transcripts, args.interval, args.once)
            return
        elif args.command == "follow":
            from .follow import follow_loop, follow_once

            if args.once:
                result = {
                    "feeds": follow_once(
                        service, args.namespace, args.paths, {}, args.source_records
                    )
                }
            else:
                follow_loop(service, args.namespace, args.paths, args.interval, args.source_records)
                return
        elif args.command == "feed":
            from .feeds import feed

            if args.source_records:
                from .session_sources import feed_records as feed

            result = feed(service, args.namespace, args.path, args.session_id)
        elif args.command == "dream":
            created = service.dream_create(
                DreamCreate(namespace=args.namespace, query=args.query, episode_ids=args.episode)
            )
            request = DreamRequest(namespace=args.namespace, dream_id=created["dream_id"])
            result = service.dream_run(request)
            if args.apply:
                result = {"dream": result, "promotion": service.dream_apply(request)}
        elif args.command == "call":
            raw = (
                Path(args.arguments[1:]).read_text()
                if args.arguments.startswith("@")
                else args.arguments
            )
            if args.tool not in service.tools():
                parser.error(f"Unknown tool {args.tool}; see: {', '.join(sorted(service.tools()))}")
            result = service.call(args.tool, json.loads(raw))
        else:
            result = service.store.repair(args.namespace)
        print(json.dumps(result, indent=2))
        if result.get("failures") or any(
            r.get("status") == "failed" for r in result.get("receipts", [])
        ):
            raise SystemExit(1)
    finally:
        service.store.close()


if __name__ == "__main__":
    main()
