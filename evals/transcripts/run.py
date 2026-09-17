"""Frozen source-boundary model canaries. No graph writes or private bank content."""

import json
import time
from pathlib import Path

from graph_memory.llm import configured_llm, extraction_instructions
from graph_memory.models import Extraction, Transcript
from graph_memory.version import engine_fingerprint


def main():
    llm = configured_llm()
    results = []
    for case in json.loads(Path(__file__).with_name("cases.json").read_text()):
        t = Transcript(
            namespace="eval:transcript-sources",
            source_id=case["id"],
            session_id=case["id"],
            source_format="session-records-v1",
            messages=case["messages"],
        )
        payload = {
            "transcript": t.model_dump(mode="json"),
            "existing_entities": [],
            "existing_relationships": [],
        }
        started = time.monotonic()
        try:
            for attempt in range(2):
                e = llm.generate(extraction_instructions(t), payload, Extraction)
                try:
                    e.validate_evidence(t)
                    break
                except ValueError as exc:
                    if attempt:
                        raise
                    payload.update(
                        rejected_candidate=e.model_dump(mode="json"), validation_error=str(exc)
                    )
            names = {x.key: x.name.lower() for x in e.entities}
            checks = {
                "active_mysql": any(
                    "mysql" in names[f.target] and f.status == "active" for f in e.facts
                ),
                "planned_postgres": any(
                    "postgr" in names[f.target] and f.status == "planned" for f in e.facts
                ),
                "no_active_postgres": not any(
                    "postgr" in names[f.target] and f.status == "active" for f in e.facts
                ),
                "no_redis": not any(
                    "redis" in names[f.target] or "redis" in f.summary.lower() for f in e.facts
                ),
                "zero_facts": not e.facts,
            }
            checked = {k: checks[k] == v for k, v in case["checks"].items()}
            results.append(
                {
                    "id": case["id"],
                    "passed": all(checked.values()),
                    "checks": checked,
                    "facts": len(e.facts),
                    "seconds": round(time.monotonic() - started, 2),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": case["id"],
                    "passed": False,
                    "error": type(exc).__name__,
                    "reason": str(exc),
                    "seconds": round(time.monotonic() - started, 2),
                }
            )
        print(json.dumps(results[-1]), flush=True)
    print(
        json.dumps(
            {
                "engine": engine_fingerprint(),
                "model": llm.model,
                "effort": llm.effort,
                "passed": all(r["passed"] for r in results),
                "results": results,
            }
        ),
        flush=True,
    )
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
