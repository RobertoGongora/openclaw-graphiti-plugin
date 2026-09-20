"""Read-only, paired first-attempt prompt study. Private inputs/results stay local.

Snapshot once, review expectations, then run baseline and candidate on identical
frozen packets. No staging, committing, repair, or live-graph writes occur here.
"""

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from graph_memory.diagnostics import diagnostic
from graph_memory.llm import EXTRACTION_INSTRUCTIONS, CodexLLM, extraction_instructions
from graph_memory.models import Extraction, Transcript


def candidate_instructions():
    from graph_memory.extraction_policy import source_instructions

    return source_instructions(EXTRACTION_INSTRUCTIONS)


SNAPSHOT = r"""
import json,os
from graph_memory.store import GraphStore
from graph_memory.service import MemoryService
from graph_memory.models import EpisodeRequest
s=GraphStore(os.environ['NEO4J_URI'], password=os.environ.get('NEO4J_PASSWORD'))
svc=MemoryService(s)
rows=s.transaction(lambda tx:tx.run("MATCH (e:MemoryEpisode {namespace:'transcripts',status:'failed'}) WHERE e.retry_feedback IS NOT NULL RETURN e.id AS id,e.retry_feedback AS feedback ORDER BY e.id").data())
selected=[]
quotas={'unvalidated_assistant_claim':2,'evidence_quote_mismatch':1,'schema_validation':1}
for r in rows:
 f=json.loads(r['feedback']); code=f.get('diagnostic',{}).get('code')
 if quotas.get(code,0)<=0: continue
 quotas[code]-=1
 p=svc.prepare(EpisodeRequest(namespace='transcripts',episode_id=r['id']))
 selected.append({'id':r['id'],'failure':code,'payload':{k:p[k] for k in ('transcript','existing_entities','existing_relationships')},'rejected':f.get('rejected_candidate'),'diagnostic':f.get('diagnostic')})
print(json.dumps(selected))
s.close()
"""


def run_one(job):
    case, variant, instructions, outdir, repeat, omit_context = job
    path = outdir / f"{case['id']}-{variant}-{repeat}.json"
    if path.exists():
        return json.loads(path.read_text())
    started = time.monotonic()
    result = {
        "id": case["id"],
        "variant": variant,
        "repeat": repeat,
        "prompt_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
        "input_policy": "context-metadata-only" if omit_context else "full",
    }
    try:
        model = CodexLLM(max_attempts=1)
        from graph_memory.extraction_policy import extraction_payload

        payload = extraction_payload(case["payload"]) if omit_context else case["payload"]
        result["input_chars"] = len(json.dumps(payload))
        extraction = model.generate(instructions, payload, Extraction)
        result["extraction"] = extraction.model_dump(mode="json")
        extraction.validate_evidence(Transcript.model_validate(case["payload"]["transcript"]))
        result["valid"] = True
    except Exception as exc:
        result.update(valid=False, error=diagnostic(exc, "eval"))
        raw = getattr(exc, "memory_rejected_candidate", None)
        if raw:
            result["rejected_candidate"] = raw
    result["seconds"] = round(time.monotonic() - started, 2)
    facts = result.get("extraction", {}).get("facts", [])
    expected = case.get("expectations", {})
    # Coverage checks are frozen before running; they complement, not replace,
    # human review of grounding. Never count unrelated entities as retained claims.
    text = json.dumps(facts, ensure_ascii=False)
    missed = [
        pattern for pattern in expected.get("concepts", []) if not re.search(pattern, text, re.I)
    ]
    statuses = {f["status"] for f in facts}
    result["coverage"] = {
        "facts": len(facts),
        "missing_concepts": missed,
        "passed": not missed
        and len(facts) >= expected.get("min_facts", 0)
        and len(facts) <= expected.get("max_facts", 10000)
        and set(expected.get("required_statuses", [])) <= statuses
        and (
            not expected.get("require_validation")
            or any(f.get("validation_evidence") for f in facts)
        ),
    }
    path.write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"extraction", "rejected_candidate"}}
        ),
        flush=True,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "revise", "run", "report"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--real-only", action="store_true")
    parser.add_argument("--from-directory", type=Path)
    parser.add_argument("--variant", choices=("baseline", "candidate"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--omit-ineligible-context", action="store_true")
    args = parser.parse_args()
    root = args.directory
    root.mkdir(parents=True, exist_ok=True)
    if args.action == "report":
        results = {}
        if args.from_directory:
            if (root / "cases.json").read_bytes() != (
                args.from_directory / "cases.json"
            ).read_bytes():
                raise ValueError("Baseline and candidate must use identical frozen cases")
            for path in (args.from_directory / "results").glob("*-baseline-*.json"):
                row = json.loads(path.read_text())
                results[(row["id"], row["variant"], row["repeat"])] = row
        for path in (root / "results").glob("*.json"):
            row = json.loads(path.read_text())
            results[(row["id"], row["variant"], row["repeat"])] = row
        groups = {}
        for variant in ("baseline", "candidate"):
            for group in ("real_failures", "controls"):
                rows = [
                    r
                    for r in results.values()
                    if r["variant"] == variant
                    and r["id"].startswith("control-") == (group == "controls")
                ]
                if rows:
                    groups[f"{variant}:{group}"] = {
                        "attempts": len(rows),
                        "validation_pass": sum(r["valid"] for r in rows),
                        "validation_and_coverage_pass": sum(
                            r["valid"] and r["coverage"]["passed"] for r in rows
                        ),
                        "median_seconds": round(statistics.median(r["seconds"] for r in rows), 2),
                    }
        report = {
            "model": "gpt-5.6-terra",
            "effort": "low",
            "automatic_retries": 0,
            "prompts": json.loads((root / "prompts.json").read_text()),
            "dataset_sha256": hashlib.sha256((root / "cases.json").read_bytes()).hexdigest(),
            "expectations_sha256": hashlib.sha256(
                (root / "expectations.json").read_bytes()
            ).hexdigest(),
            "groups": groups,
            "results": [
                {k: v for k, v in r.items() if k not in {"extraction", "rejected_candidate"}}
                for r in sorted(
                    results.values(), key=lambda r: (r["id"], r["repeat"], r["variant"])
                )
            ],
            "limitations": [
                "Small diagnostic sample selected from known failures, not a random archive-wide estimate.",
                "Coverage checks are a frozen subset of important concepts; semantic grounding also needs human review.",
                "First-attempt comparison; production retains correction retries.",
                "Elapsed time and calls are cost proxies, not measured billable tokens or subscription usage.",
            ],
        }
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(groups, indent=2))
        return
    if args.action == "revise":
        if (root / "cases.json").exists() or args.from_directory is None:
            raise ValueError("Use a new directory and --from-directory for a frozen revision")
        for name in ("cases.json", "expectations.json"):
            (root / name).write_bytes((args.from_directory / name).read_bytes())
        prompts = json.loads((args.from_directory / "prompts.json").read_text())
        prompts["candidate"] = candidate_instructions()
        (root / "prompts.json").write_text(json.dumps(prompts, indent=2))
        return
    if args.action == "snapshot":
        if (root / "cases.json").exists():
            raise ValueError("Snapshot exists; use it unchanged or select a new directory")
        raw = subprocess.run(
            ["docker", "exec", "-i", "graph-memory-transcripts-mcp-1", "python", "-c", SNAPSHOT],
            capture_output=True,
            text=True,
            check=True,
        )
        (root / "cases.json").write_text(raw.stdout)
        (root / "prompts.json").write_text(
            json.dumps(
                {
                    "baseline": extraction_instructions(
                        Transcript.model_validate(
                            json.loads(raw.stdout)[0]["payload"]["transcript"]
                        )
                    ),
                    "candidate": candidate_instructions(),
                },
                indent=2,
            )
        )
        return
    cases = json.loads((root / "cases.json").read_text())
    expectations = json.loads((root / "expectations.json").read_text())
    for case in cases:
        case["expectations"] = expectations[case["id"]]
    if not args.real_only:
        cases += json.loads(Path(__file__).with_name("failure_controls.json").read_text())
    prompts = json.loads((root / "prompts.json").read_text())
    if args.variant:
        prompts = {args.variant: prompts[args.variant]}
    out = root / "results"
    out.mkdir(exist_ok=True)
    jobs = []
    for i, case in enumerate(cases):
        # Alternate order within pairs to avoid always giving one variant first.
        variants = list(prompts) if (i + args.repeat) % 2 else list(reversed(prompts))
        jobs.extend(
            (
                case,
                variant,
                prompts[variant],
                out,
                args.repeat,
                args.omit_ineligible_context and variant == "candidate",
            )
            for variant in variants
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run_one, jobs))


if __name__ == "__main__":
    main()
