"""How many assistant claims the model validates under different batch rules.

Reads real session files, so the report holds counts only and no graph is touched.
Each sampled batch is extracted once per variant:

  old     shell output as context, no turn results carried (the rules before 26b2f63)
  carry0  shell output as a result, nothing carried
  carryN  shell output as a result, up to N characters of turn results carried
"""

import argparse
import json
import random
import time
from collections import Counter
from pathlib import Path

from graph_memory.llm import configured_llm, extraction_instructions
from graph_memory.models import CLAIMS, Message, Transcript
from graph_memory.service import MemoryService
from graph_memory.session_sources import FORMAT, batch, before_shell_results, records
from graph_memory.version import engine_fingerprint


def variant(name):
    if name == "old":
        return True, 0
    if not name.startswith("carry"):
        raise SystemExit(f"Unknown variant {name}")
    return False, int(name[5:])


def sample(paths, per_file, limit, rng):
    """(path, count) of batches whose new messages hold an assistant claim made
    after at least one tool output in the same file: the only ones the variants differ on."""
    found = []
    for path in paths:
        messages = list(records(path))
        count, mine = 0, []
        while count < len(messages):
            end, _ = batch(messages, count, 0)
            if any(m.source_type == "assistant_report" for m in messages[count:end]) and any(
                m.role == "tool" for m in messages[:count]
            ):
                mine.append((path, count))
            count = end
        found += rng.sample(mine, min(per_file, len(mine)))
    return rng.sample(found, min(limit, len(found)))


def transcript(path, count, old, lookback):
    messages = list(records(path))
    if old:
        messages = [
            Message.model_validate(before_shell_results(m.model_dump(mode="json")))
            for m in messages
        ]
    end, selected = batch(messages, count, lookback)
    from graph_memory.recall_provenance import report_origins

    # Whole-session origins, as the feed computes them, so the eval sees what production sends.
    origins = report_origins(messages)
    return Transcript(
        namespace="eval:lookback",
        session_id=path.stem,
        source_id=f"lookback:{path.stem}:{count}",
        source_format=FORMAT,
        messages=selected,
        memory_origins={m.id: origins[m.id] for m in selected if m.id in origins},
        focus_message_ids=[m.id for m in messages[count:end]],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path, help="Directories of session JSONL files")
    parser.add_argument("--variants", default="old,carry0,carry20000,carry40000")
    parser.add_argument("--files", type=int, default=40)
    parser.add_argument("--per-file", type=int, default=2)
    parser.add_argument("--batches", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    paths = sorted(p for root in args.roots for p in root.rglob("*.jsonl"))
    picked = sample(
        rng.sample(paths, min(args.files, len(paths))), args.per_file, args.batches, rng
    )
    service = MemoryService(None, configured_llm())
    report = {}
    for name in args.variants.split(","):
        old, lookback = variant(name)
        totals, seconds = Counter(), 0.0
        for path, count in picked:
            t = transcript(path, count, old, lookback)
            totals["batches"] += 1
            totals["characters"] += sum(len(m.content) for m in t.messages)
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
                by_assistant = all(
                    claims.get(e.message_id) == "assistant_report" for e in fact.evidence
                )
                if by_assistant:
                    totals["assistant_facts"] += 1
                    totals["assistant_facts_validated"] += bool(fact.validation_evidence)
        report[name] = {**totals, "seconds": round(seconds, 1)}
        print(json.dumps({name: report[name]}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "engine": engine_fingerprint(),
                "model": service.llm.model,
                "effort": service.llm.effort,
                "seed": args.seed,
                "batches": len(picked),
                "variants": report,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
