"""What the extractor makes of real batches: relations, slots and validation.

Reads real session files; the report holds counts only and touches no graph. Run
it from two worktrees with the same seed to compare engines: the relation mix,
how many facts fall back to related_to, how many slots are emitted and how many
of those are used by a single fact, and the validation rate as a regression check.

    MEMORY_LLM=codex uv run python -m evals.vocabulary ~/.claude/projects --batches 24 \\
        --output .local/vocabulary-<engine>.json
"""

import argparse
import json
import random
import time
from collections import Counter
from pathlib import Path

from graph_memory.llm import configured_llm, extraction_instructions
from graph_memory.models import CLAIMS
from graph_memory.service import MemoryService
from graph_memory.session_sources import LOOKBACK_CHARS
from graph_memory.version import engine_fingerprint

from .lookback import sample, transcript


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--files", type=int, default=40)
    parser.add_argument("--per-file", type=int, default=2)
    parser.add_argument("--batches", type=int, default=24)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    paths = sorted(p for root in args.roots for p in root.rglob("*.jsonl"))
    picked = sample(
        rng.sample(paths, min(args.files, len(paths))), args.per_file, args.batches, rng
    )
    llm = configured_llm()
    service = MemoryService(None, llm)
    totals, relations, slots, seconds = Counter(), Counter(), Counter(), 0.0
    for path, count in picked:
        t = transcript(path, count, False, LOOKBACK_CHARS)
        totals["batches"] += 1
        if not t.can_yield_facts():
            totals["skipped"] += 1
            continue
        packet = {
            "transcript": t.model_dump(mode="json"),
            "existing_entities": [],
            "existing_relationships": [],
            "instructions": extraction_instructions(t),
        }
        started = time.monotonic()
        try:
            extraction, calls = service.propose(packet)
        except Exception as exc:
            totals["failed"] += 1
            totals[f"failed:{type(exc).__name__}"] += 1
            continue
        finally:
            seconds += time.monotonic() - started
        totals["model_calls"] += calls
        claims = {m.id: m.source_type for m in t.messages if m.source_type in CLAIMS}
        for fact in extraction.facts:
            totals["facts"] += 1
            totals[f"status:{fact.status}"] += 1
            relations[fact.relation.value] += 1
            if fact.slot:
                totals["facts_with_slot"] += 1
                slots[(fact.subject, fact.relation.value, fact.slot)] += 1
            if all(claims.get(e.message_id) == "assistant_report" for e in fact.evidence):
                totals["assistant_facts"] += 1
                totals["assistant_facts_validated"] += bool(fact.validation_evidence)
    report = {
        "engine": engine_fingerprint(),
        "model": llm.model,
        "effort": llm.effort,
        "seed": args.seed,
        "batches": len(picked),
        "totals": {**totals, "seconds": round(seconds, 1)},
        "relations": dict(relations.most_common()),
        "related_to_share": round(relations["related_to"] / max(1, totals["facts"]), 3),
        "slots": {
            "distinct": len(slots),
            "single_use": sum(1 for c in slots.values() if c == 1),
            "shared": sum(1 for c in slots.values() if c > 1),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
