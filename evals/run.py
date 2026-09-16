"""Golden behavioral eval runner. Never learns expectations from its own output."""

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from graph_memory.cli import build_service
from graph_memory.models import DreamCreate, DreamRequest, Ingest, Transcript
from graph_memory.store import digest
from graph_memory.version import engine_fingerprint

CASE_DIR = Path(__file__).parent / "cases"


def load_cases():
    return [json.loads(p.read_text()) for p in sorted(CASE_DIR.glob("*.json"))]


def suite_fingerprint():
    return digest(
        {
            "engine_cases": load_cases(),
            "e2e_cases": [
                json.loads(p.read_text())
                for p in sorted((Path(__file__).parent / "ab").glob("*.json"))
            ],
        }
    )


def assertion(value, check):
    if "equals" in check:
        return value == check["equals"]
    if "one_of" in check:
        return value in check["one_of"]
    spec = check.get("contains", check.get("excludes"))
    if not isinstance(value, list) or not isinstance(spec, dict):
        return False

    def matches(item):
        for key, expected in spec.items():
            if key.endswith("_contains"):
                actual = str(item.get(key[:-9], "")).lower()
                # Explicit fixture synonym; no model judges or moving expectations.
                if expected == "postgre":
                    expected = "postgr"
                if expected.lower() not in actual:
                    return False
            elif item.get(key) != expected:
                return False
        return True

    found = any(matches(item) for item in value)
    return found if "contains" in check else not found


def check_context(service, namespace, checks):
    outcomes = []
    for check in checks:
        result = service.call(check["tool"], {"namespace": namespace, **check["arguments"]})
        value = result
        for part in check["path"].split("."):
            value = value.get(part) if isinstance(value, dict) else None
        outcomes.append({"check": check, "passed": assertion(value, check), "actual": value})
    return outcomes


def evaluate(service, runs=1):
    if service.llm is None:
        raise ValueError("LLM eval requires MEMORY_LLM=codex or compatible")
    engine, suite = engine_fingerprint(), suite_fingerprint()
    results = []
    for repeat in range(runs):
        for case in load_cases():
            namespace = "eval:" + str(uuid.uuid4())
            try:
                receipts = []
                for raw in case["transcripts"]:
                    receipts.append(
                        service.ingest(
                            Ingest(
                                transcript=Transcript.model_validate(
                                    {**raw, "namespace": namespace}
                                ),
                                extract=True,
                            )
                        )
                    )
                checks = check_context(service, namespace, case["checks"])
                if case.get("dream"):
                    before = service.store.recall(namespace, case["dream"]["query"])
                    created = service.dream_create(
                        DreamCreate(
                            namespace=namespace,
                            query=case["dream"]["query"],
                            episode_ids=[r["episode_id"] for r in receipts],
                            instructions=case["dream"]["instructions"],
                        )
                    )
                    request = DreamRequest(namespace=namespace, dream_id=created["dream_id"])
                    dreamed = service.dream_run(request)
                    after = service.store.recall(namespace, case["dream"]["query"])
                    checks.append(
                        {
                            "check": "dream preserves input facts and produces supported candidate insights",
                            "passed": before["current"] == after["current"]
                            and not after["insights"]
                            and bool(dreamed["output"]["insights"]),
                        }
                    )
                    service.dream_apply(request)
                    checks.append(
                        {
                            "check": "dream promotion cannot replace current facts",
                            "passed": service.store.recall(namespace, case["dream"]["query"])[
                                "current"
                            ]
                            == before["current"],
                        }
                    )
                results.append(
                    {
                        "case": case["id"],
                        "repeat": repeat,
                        "passed": all(c["passed"] for c in checks),
                        "checks": checks,
                        "diagnostics": None
                        if all(c["passed"] for c in checks)
                        else {
                            "extractions": [
                                json.loads(
                                    service.store.episode(namespace, r["episode_id"])[
                                        "extraction_payload"
                                    ]
                                )
                                for r in receipts
                            ],
                            "contexts": {
                                c["arguments"]["query"]: service.store.recall(
                                    namespace, c["arguments"]["query"], limit=100
                                )
                                for c in case["checks"]
                                if c["tool"] == "memory_recall"
                            },
                        },
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "case": case["id"],
                        "repeat": repeat,
                        "passed": False,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:2000],
                    }
                )
            finally:
                service.store.transaction(
                    lambda tx, namespace=namespace: tx.run(
                        "MATCH (n) WHERE n.namespace=$ns OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                        ns=namespace,
                    ).consume()
                )
            print(
                json.dumps(
                    {
                        k: results[-1][k]
                        for k in ("case", "repeat", "passed", "error", "detail")
                        if k in results[-1]
                    }
                ),
                flush=True,
            )
    return {
        "engine": engine,
        "suite": suite,
        "model": getattr(service.llm, "model", "unknown"),
        "reasoning_effort": getattr(service.llm, "effort", None),
        "runs": runs,
        "passed": bool(results)
        and all(r["passed"] for r in results)
        and engine == engine_fingerprint()
        and suite == suite_fingerprint(),
        "inputs_unchanged_during_run": engine == engine_fingerprint()
        and suite == suite_fingerprint(),
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--ab-report", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".local/eval-report.json"))
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be 1..20")
    # Explicit test endpoint; never silently fall back to a production database.
    uri = os.environ.get("MEMORY_TEST_NEO4J_URI")
    if not uri:
        parser.error("MEMORY_TEST_NEO4J_URI is required")
    os.environ["NEO4J_URI"] = uri
    if "MEMORY_TEST_NEO4J_PASSWORD" in os.environ:
        os.environ["NEO4J_PASSWORD"] = os.environ["MEMORY_TEST_NEO4J_PASSWORD"]
    service = build_service()
    try:
        test_engine = engine_fingerprint()
        tests = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], check=False)
        if tests.returncode:
            raise SystemExit("Deterministic tests failed; model eval and promotion blocked")
        report = evaluate(service, args.runs)
        report["deterministic_passed"] = tests.returncode == 0 and test_engine == report["engine"]
        report["passed"] = report["passed"] and report["deterministic_passed"]
        if args.ab_report:
            ab = json.loads(args.ab_report.read_text())
            report["e2e_passed"] = ab.get("passed") is True and ab.get("engine") == report["engine"]
            report["e2e_report_digest"] = digest(ab)
            report["passed"] &= report["e2e_passed"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"passed": report["passed"], "report": str(args.output)}))
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        service.store.close()


if __name__ == "__main__":
    main()
