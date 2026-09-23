"""Evaluate frozen recall projections offline. Never reads or writes a live graph.

Input: {cases:[{entity, question, raw, expected_ids, baseline_ids?, max_answer_rank?}]}.
Expected IDs are source-reviewed answer records, not model-generated expectations.
Empty expected_ids means unscored, not an assertion that no answer exists.
Reports omit private questions, fact text and IDs; keep the input private.
"""

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

from graph_memory.retrieval import RecallView, recall


def evaluate(cases, limit=8):
    rows = []
    for index, case in enumerate(cases, 1):
        if not case.get("question"):
            raise ValueError(f"Case {index} has no question; full detail without one is unranked")
        store = SimpleNamespace(recall=lambda *a, raw=case["raw"], **kw: raw)
        args = dict(
            namespace="snapshot", entity=case["entity"], question=case["question"], limit=limit
        )
        started = time.perf_counter()
        compact = recall(store, RecallView(**args))
        elapsed = (time.perf_counter() - started) * 1000
        full = recall(store, RecallView(**args, detail="full"))
        ids = [f["id"] for f in compact["facts"]]
        same_ids = ids == [f["id"] for f in full["facts"]]
        if not same_ids:
            raise ValueError(f"Case {index}: detail changed answer selection")
        expected = set(case.get("expected_ids", []))
        hits = expected & set(ids)
        first_answer_rank = next((i for i, fid in enumerate(ids, 1) if fid in expected), None)
        max_rank = case.get("max_answer_rank", limit)
        if (
            not isinstance(max_rank, int)
            or isinstance(max_rank, bool)
            or not 1 <= max_rank <= limit
        ):
            raise ValueError(f"Case {index}: max_answer_rank must be between 1 and {limit}")
        rows.append(
            {
                "case": index,
                "scored": bool(expected),
                "expected_records": len(expected),
                "answer_records_returned": len(hits),
                "baseline_answer_records_returned": len(
                    expected & set(case.get("baseline_ids", []))
                ),
                "first_answer_rank": first_answer_rank,
                "max_answer_rank": max_rank,
                "rank_target_met": first_answer_rank is not None and first_answer_rank <= max_rank
                if expected
                else None,
                "returned_facts": len(ids),
                "matching_unique": compact["counts"]["matching_unique"],
                "compact_json_bytes": len(json.dumps(compact).encode()),
                "full_json_bytes": len(json.dumps(full).encode()),
                "formatting_ms": round(elapsed, 2),
                "compact_full_same_ids": same_ids,
            }
        )
    return {
        "cases": rows,
        "limit": limit,
        "scored_cases": sum(r["scored"] for r in rows),
        "cases_with_answer_before": sum(r["baseline_answer_records_returned"] > 0 for r in rows),
        "cases_with_answer_after": sum(r["answer_records_returned"] > 0 for r in rows),
        "rank_targets_met": all(r["rank_target_met"] for r in rows if r["scored"]),
        "median_formatting_ms": statistics.median(r["formatting_ms"] for r in rows)
        if rows
        else None,
        "basis": "Offline selection on identical frozen facts. Development regression cases, not an independent accuracy estimate. Times exclude MCP/database/model work. No answer claim for unscored cases.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.snapshot.read_bytes()
    report = evaluate(json.loads(raw)["cases"])
    report["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}))
    if not report["rank_targets_met"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
