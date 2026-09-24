"""Historical recall experiment on an explicitly supplied frozen database copy.

The candidate is opt-in here only. It retains all facts/entities/insights (and
thus complete temporal roles), episode statuses, and message timestamps. Source
bodies remain available through ordinary historical evidence retrieval. Private
queries and responses belong under .local/, never in a committed report.

Run each mode in a fresh process/container against the same restored backup.
First-request timings are process-cold, not necessarily database-cache-cold.
"""

import argparse
import gc
import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from graph_memory.journal import Journal
from graph_memory.service import MemoryService
from graph_memory.store import GraphStore

RECALL_LABELS = frozenset(
    {"MemoryEntity", "MemoryFact", "MemoryInsight", "MemoryEpisode", "MemoryMessage"}
)


def recall_node(label, node):
    if label == "MemoryEpisode":
        return {key: node[key] for key in ("id", "status") if key in node}
    if label == "MemoryMessage":
        return {key: node[key] for key in ("id", "timestamp") if key in node}
    return node


class RecallJournal(Journal):
    def snapshot(self, namespace, **kwargs):
        return super().snapshot(
            namespace,
            **kwargs,
            select=lambda label, node: label in RECALL_LABELS,
            project_node=recall_node,
        )

    def _complete_only(self, *args, **kwargs):
        raise NotImplementedError("RecallJournal serves projected recall only")

    # Both would write or compare a projected state as if it were complete.
    replay = verify = _complete_only


class ProjectedStore(GraphStore):
    def recall(
        self,
        namespace,
        query,
        as_of=None,
        limit=30,
        *,
        _complete=False,
        known_at=None,
        at_change=None,
        _search=False,
        _related_question=None,
    ):
        if known_at is None and at_change is None:
            raise ValueError("This experiment requires a fixed historical cutoff")
        return RecallJournal(self).recall(
            namespace,
            query,
            as_of,
            limit,
            known_at=known_at,
            sequence=at_change,
            complete=_complete,
            search=_search,
            related_question=_related_question,
        )


def memory():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {"process_peak_bytes": peak if sys.platform == "darwin" else peak * 1024}
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                result["process_rss_bytes"] = int(line.split()[1]) * 1024
    for field in ("current", "peak"):
        path = Path(f"/sys/fs/cgroup/memory.{field}")
        if path.exists():
            result[f"container_{field}_bytes"] = int(path.read_text())
    return result


def run(store, namespace, cases, repeats, output):
    # Leftover responses from an earlier run would enter the byte comparison.
    if output.exists() and any(output.iterdir()):
        raise ValueError("--output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    catalog = MemoryService(store).session_tools()
    rows = []
    for repetition in range(repeats):
        for index, case in enumerate(cases):
            tool = case["tool"]
            if tool not in {
                "memory_recall",
                "memory_search",
                "memory_search_entities",
                "memory_latest",
                "memory_evidence",
            }:
                raise ValueError("Only historical read tools are permitted")
            args = {**case["arguments"], "namespace": namespace}
            if args.get("known_at") is None and args.get("at_change") is None:
                raise ValueError("Every request requires a historical cutoff")
            schema, handler, _ = catalog[tool]
            before = memory()
            start = time.perf_counter()
            # Only request validation is recorded as an expected error; a
            # ValidationError from inside a handler must fail the run.
            try:
                request = schema.model_validate(args)
            except ValidationError as exc:
                response = {"validation_error": json.loads(exc.json(include_url=False))}
            else:
                try:
                    response = handler(request)
                except ValueError as exc:
                    # The engine's own messages, as MCP returns them; subclasses raise.
                    if type(exc) is not ValueError:
                        raise
                    response = {"error": str(exc)}
            # Same canonical response encoding in both modes; compare every field.
            encoded = json.dumps(response, sort_keys=True, ensure_ascii=False).encode()
            elapsed = time.perf_counter() - start
            held = memory()
            path = output / f"response-{repetition}-{index}.json"
            path.write_bytes(encoded)
            row = {
                "repetition": repetition,
                "case": index,
                "tool": tool,
                "seconds": elapsed,
                "response_bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "before": before,
                "response_held": held,
            }
            del response, encoded
            gc.collect()
            row["after_gc"] = memory()
            rows.append(row)
            (output / "measurements.json").write_text(json.dumps(rows, indent=2) + "\n")
            print(json.dumps(row), flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--frozen-copy", action="store_true", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--mode", choices=("full", "projected"), required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    uri = urlparse(args.uri)
    try:
        port = uri.port
    except ValueError:
        parser.error("--uri has an invalid port")
    # Without a port the driver would use 7687, which this guard cannot vouch for.
    if uri.hostname not in {"localhost", "127.0.0.1", "::1"} or port in {None, 17687, 27687}:
        parser.error(
            "Use an isolated loopback database with an explicit port; live graph ports are forbidden"
        )
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    store = (ProjectedStore if args.mode == "projected" else GraphStore)(
        args.uri, password=os.environ.get("NEO4J_PASSWORD")
    )
    try:
        run(
            store,
            args.namespace,
            json.loads(args.queries.read_text())["calls"],
            args.repeats,
            args.output,
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
