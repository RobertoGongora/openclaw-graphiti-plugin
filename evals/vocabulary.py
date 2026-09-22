"""What the extractor makes of real batches: relations, slots and validation.

Reads real session files; the report holds counts only and touches no graph. Run
it from two worktrees with the same frozen corpus to compare engines: the relation mix,
how many facts fall back to related_to, how many slots are emitted and how many
of those are used by a single fact, and the validation rate as a regression check.

    MEMORY_LLM=codex uv run python -m evals.vocabulary ~/.claude/projects --batches 24 \\
        --corpus /absolute/path/to/.local/vocabulary-corpus.json \\
        --output .local/vocabulary-<engine>.json

The corpus contains private transcript text: keep it under ignored .local/.
Reports checkpoint after every batch. Repeating the command resumes unfinished
work only when the engine, model, effort and corpus still match.
"""

import argparse
import hashlib
import json
import random
import time
from collections import Counter
from pathlib import Path

from graph_memory.llm import configured_llm, extraction_instructions
from graph_memory.models import CLAIMS, Transcript
from graph_memory.service import MemoryService
from graph_memory.session_sources import LOOKBACK_CHARS
from graph_memory.version import engine_fingerprint

from .lookback import sample, transcript


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--files", type=int, default=40)
    parser.add_argument("--per-file", type=int, default=2)
    parser.add_argument("--batches", type=int, default=24)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--corpus", type=Path, help="Freeze input transcripts here; reuse for both engines"
    )
    args = parser.parse_args()
    if args.corpus and args.corpus.exists():
        corpus = json.loads(args.corpus.read_text())
    else:
        rng = random.Random(args.seed)
        paths = sorted(p for root in args.roots for p in root.rglob("*.jsonl"))
        picked = sample(
            rng.sample(paths, min(args.files, len(paths))), args.per_file, args.batches, rng
        )
        corpus = [
            transcript(path, count, False, LOOKBACK_CHARS).model_dump(mode="json")
            for path, count in picked
        ]
        if args.corpus:
            write_json(args.corpus, corpus)
    if not corpus:
        raise SystemExit("No eligible batches")
    llm = configured_llm()
    service = MemoryService(None, llm)
    identity = {
        "engine": engine_fingerprint(),
        "model": llm.model,
        "effort": llm.effort,
        "corpus_sha256": hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest(),
        "batches": len(corpus),
    }
    totals, relations, slots, seconds = Counter(), Counter(), Counter(), 0.0
    if args.output.exists():
        previous = json.loads(args.output.read_text())
        if any(previous.get(k) != v for k, v in identity.items()):
            raise SystemExit("Checkpoint engine, model, effort or corpus differs; use a new output")
        totals.update(previous["totals"])
        seconds = totals.pop("seconds", 0.0)
        relations.update(previous["relations"])
        slots.update(previous["slot_counts"])

    def checkpoint():
        if engine_fingerprint(fresh=True) != identity["engine"]:
            raise RuntimeError("Engine changed during eval; refusing to save mixed results")
        report = {
            **identity,
            "complete": totals["batches"] == len(corpus),
            "totals": {**totals, "seconds": round(seconds, 1)},
            "relations": dict(relations.most_common()),
            "related_to_share": round(relations["related_to"] / max(1, totals["facts"]), 3),
            "validation_rate": round(
                totals["assistant_facts_validated"] / max(1, totals["assistant_facts"]), 3
            ),
            "slots": {
                "distinct": len(slots),
                "single_use": sum(1 for c in slots.values() if c == 1),
                "shared": sum(1 for c in slots.values() if c > 1),
            },
            "slot_counts": dict(slots),
        }
        write_json(args.output, report)
        print(json.dumps({k: v for k, v in report.items() if k != "slot_counts"}), flush=True)

    checkpoint()
    for data in corpus[totals["batches"] :]:
        t = Transcript.model_validate(data)
        totals["batches"] += 1
        if not t.can_yield_facts():
            totals["skipped"] += 1
            checkpoint()
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
            seconds += time.monotonic() - started
            checkpoint()
            continue
        else:
            seconds += time.monotonic() - started
        totals["model_calls"] += calls
        claims = {m.id: m.source_type for m in t.messages if m.source_type in CLAIMS}
        for fact in extraction.facts:
            totals["facts"] += 1
            totals[f"status:{fact.status}"] += 1
            relations[fact.relation.value] += 1
            if fact.slot:
                totals["facts_with_slot"] += 1
                slot_key = hashlib.sha256(
                    json.dumps([fact.subject, fact.relation.value, fact.slot]).encode()
                ).hexdigest()
                slots[slot_key] += 1
            if all(claims.get(e.message_id) == "assistant_report" for e in fact.evidence):
                totals["assistant_facts"] += 1
                totals["assistant_facts_validated"] += bool(fact.validation_evidence)
        checkpoint()


if __name__ == "__main__":
    main()
