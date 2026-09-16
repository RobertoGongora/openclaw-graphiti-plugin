"""Local CLI: serve, ingest, import, drain the durable inbox, and run dreams."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .importers import memory_files, transcripts
from .llm import configured_llm
from .mcp import Protocol, http_server, stdio
from .models import DreamCreate, DreamRequest, Ingest, Relation
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    worker = commands.add_parser("work")
    worker.add_argument("--limit", type=int, default=10)
    worker.add_argument("--watch", action="store_true")
    worker.add_argument("--interval", type=float, default=5)
    daemon = commands.add_parser("daemon", help="Watch memory banks and process the queue")
    daemon.add_argument("paths", nargs="*", type=Path)
    daemon.add_argument("--transcripts", action="append", type=Path, default=[])
    daemon.add_argument("--workers", type=int, default=4)
    daemon.add_argument("--interval", type=float, default=30)
    daemon.add_argument("--once", action="store_true")
    scanner = commands.add_parser("scan", help="Stage new memory-bank content without extraction")
    scanner.add_argument("paths", nargs="+", type=Path)
    commands.add_parser("hook")
    follower = commands.add_parser("follow")
    follower.add_argument("paths", nargs="+", type=Path)
    follower.add_argument("--interval", type=float, default=5)
    follower.add_argument("--once", action="store_true")
    configure = commands.add_parser("claude-config")
    configure.add_argument("directory", type=Path)
    feeder = commands.add_parser("feed")
    feeder.add_argument("path", type=Path)
    feeder.add_argument("--session-id", required=True)
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
    service = build_service()
    try:
        if args.command == "serve":
            protocol = Protocol(service, namespace=args.namespace, read_only=args.read_only)
            if args.transport == "stdio":
                stdio(protocol)
            else:
                server = http_server(
                    protocol, args.host, args.port, os.environ.get("MEMORY_HTTP_TOKEN")
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
            result = service.store.recall(args.namespace, args.query)
        elif args.command == "latest":
            result = service.store.latest(args.namespace, args.entity, relation=args.relation)
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
            )
            if result is None:
                return
        elif args.command == "work":
            if service.llm is None:
                parser.error("work requires MEMORY_LLM=codex or compatible")
            if not 1 <= args.limit <= 100:
                parser.error("--limit must be 1..100")
            from .feeds import worker_tick

            if args.interval < 1:
                parser.error("--interval must be at least 1 second")
            while True:
                result = worker_tick(service, args.namespace, args.limit)
                if not args.watch:
                    break
                if result["receipts"]:
                    print(json.dumps(result), flush=True)
                time.sleep(args.interval)
        elif args.command == "follow":
            from .follow import follow_loop, follow_once

            if args.interval < 1:
                parser.error("--interval must be at least 1 second")
            if not all(p.exists() for p in args.paths):
                parser.error("All transcript paths must exist")
            if args.once:
                result = {"feeds": follow_once(service, args.namespace, args.paths, {})}
            else:
                follow_loop(service, args.namespace, args.paths, args.interval)
                return
        elif args.command == "hook":
            from .feeds import hook

            result = hook(service, args.namespace, json.load(sys.stdin))
        elif args.command == "feed":
            from .feeds import feed

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
