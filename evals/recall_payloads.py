"""Compare response sizes on a saved read-only snapshot, never ingest or alter a graph.

Input is a private JSON list of {query, question?, raw} objects. Output contains
only sizes/counts and a snapshot digest; it is not an accuracy benchmark.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from graph_memory.retrieval import LANES, RecallView, recall


def compare(cases):
    results = []
    for index, case in enumerate(cases):
        raw = case["raw"]
        legacy = {**raw, **{k: raw[k][:30] for k in LANES}}
        request = RecallView(
            namespace="snapshot", entity=case["query"], question=case.get("question")
        )
        store = SimpleNamespace(recall=lambda *args, raw=raw, **kwargs: raw)
        started = time.perf_counter()
        compact = recall(store, request)
        ms = (time.perf_counter() - started) * 1000
        before = len(json.dumps(legacy).encode())
        after = len(json.dumps(compact).encode())
        available_ids = {f["id"] for lane in LANES for f in raw[lane]}
        assert all(f["id"] in available_ids for f in compact["facts"])
        results.append(
            {
                "case": index + 1,
                "question_supplied": bool(case.get("question")),
                "legacy_json_bytes": before,
                "compact_json_bytes": after,
                "reduction_percent": round(100 * (1 - after / before), 1),
                "formatting_ms": round(ms, 2),
                "returned_facts": len(compact["facts"]),
                "matching_unique": compact["counts"]["matching_unique"],
                "facts_are_existing_records": True,
            }
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    payload = args.snapshot.read_bytes()
    print(
        json.dumps(
            {
                "kind": "payload-size-check-not-accuracy-benchmark",
                "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
                "results": compare(json.loads(payload)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
