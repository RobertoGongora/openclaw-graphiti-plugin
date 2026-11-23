"""Synthetic Spanish sessions through the real model: English fact summaries,
verbatim Spanish quotes, preserved identifiers, and an English question that
finds the eventual decision. Checks are fixed code; no model judges a model."""

import argparse
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from graph_memory.models import Ingest, Transcript
from graph_memory.version import engine_fingerprint

CASES = Path(__file__).parent / "english" / "cases.json"
# Spanish function and content words of these cases that English never uses. "la"
# and similar short words are left out: they occur in English names too.
SPANISH = set(
    "el los las del de para con por una unos unas que se es está están fue pero aún "
    "todavía hasta según sin también decidimos vamos propuesta decisión corrección "
    "incidente subiendo resolvió reinició clúster producción aprobado activar propuse "
    "mensajes servicio pagos desplegado".split()
)
ENGLISH = set("the to is was for and of in not as with on by an it be has".split())


def load_cases():
    return json.loads(CASES.read_text())


def transcript(case, namespace):
    return Transcript(
        namespace=namespace,
        source_id="english-" + case["id"],
        session_id="english-" + case["id"],
        source_format="session-records-v1",
        messages=[{**m, "role": "user", "source_type": "user_assertion"} for m in case["messages"]],
    )


def words(text):
    return set(re.findall(r"[^\W\d_]+", text.lower()))


def check_case(case, source, facts, found):
    """facts: committed facts of the episode. found: memory_search facts for the
    case's English question, best first, with their lanes."""
    content = {m.id: m.content for m in source.messages}
    summaries = [f["summary"] for f in facts]
    checks = []

    def record(name, passed, actual=None):
        checks.append({"check": name, "passed": bool(passed), "actual": actual})

    record("facts extracted", facts, len(facts))
    spanish = {s: sorted(words(s) & SPANISH) for s in summaries}
    record(
        "every summary is English",
        facts and all(not spanish[s] and words(s) & ENGLISH for s in summaries),
        {s: w for s, w in spanish.items() if w or not words(s) & ENGLISH},
    )
    quotes = [e for f in facts for e in f["evidence"] + f.get("validation_evidence", [])]
    record(
        "every quote is verbatim source text",
        quotes and all(e["quote"] in content.get(e["message_id"], "") for e in quotes),
        [e for e in quotes if e["quote"] not in content.get(e["message_id"], "")],
    )
    record(
        "quotes are untranslated",
        any(words(e["quote"]) & SPANISH for e in quotes),
        [e["quote"] for e in quotes],
    )
    text = " ".join(summaries).lower()
    missing = [i for i in case["identifiers"] if i.lower() not in text]
    record("identifiers kept in summaries", not missing, missing)
    top = found[0]["text"] if found else ""
    record(
        "English question finds the eventual answer first",
        all(t.lower() in top.lower() for t in case["top_contains"]),
        [f["text"] for f in found[:3]],
    )
    # Judged on what retrieval serves, not on raw statuses: a superseded decision
    # may be extracted as planned and still be moved to history by its slot.
    for rule in case.get("not_current", []):
        wrong = [
            f["text"]
            for f in found
            if f["lane"] in {"current", "planned"}
            and rule["mentions"] in f["text"].lower()
            and not re.search(rule["unless"], f["text"].lower())
        ]
        record(f"{rule['mentions']} is not the current choice", not wrong, wrong)
    if rule := case.get("not_executed"):
        wrong = [
            f["text"]
            for f in found
            if f["lane"] == "current"
            and rule["mentions"] in f["text"].lower()
            and re.search(rule["words"], f["text"].lower())
            and not re.search(rule["unless"], f["text"].lower())
        ]
        record(f"{rule['mentions']} is not reported as done", not wrong, wrong)
    if rule := case.get("negated"):
        wrong = [
            s
            for s in summaries
            if rule["mentions"] in s.lower() and not re.search(rule["requires"], s.lower())
        ]
        record(f"negation kept for {rule['mentions']}", not wrong, wrong)
    return checks


def search(service, namespace, case):
    return service.call(
        "memory_search", {"namespace": namespace, "question": case["question"], "limit": 20}
    )


def run_case(service, case):
    namespace = "eval:" + str(uuid.uuid4())
    source = transcript(case, namespace)
    try:
        receipt = service.ingest(Ingest(transcript=source, extract=True))
        episode = service.store.episode(namespace, receipt["episode_id"])
        facts = (
            json.loads(episode["extraction_payload"])["facts"]
            if episode.get("extraction_payload")
            else []
        )
        found = search(service, namespace, case)
        checks = check_case(case, source, facts, found["facts"])
        return {
            "case": case["id"],
            "status": episode["status"],
            "passed": all(c["passed"] for c in checks),
            "checks": checks,
            "facts": [
                {
                    k: f.get(k)
                    for k in ("subject", "relation", "target", "status", "summary", "evidence")
                }
                for f in facts
            ],
        }
    except Exception as exc:
        return {
            "case": case["id"],
            "passed": False,
            "error": type(exc).__name__,
            "detail": str(exc)[:2000],
        }
    finally:
        service.store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR (n:MemoryChange AND n.scope=$ns) OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                ns=namespace,
            ).consume()
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path(".local/english-summaries.json"))
    args = parser.parse_args()
    if not 1 <= args.runs <= 5:
        parser.error("--runs must be 1..5")
    uri = os.environ.get("MEMORY_TEST_NEO4J_URI")
    if not uri:
        parser.error("MEMORY_TEST_NEO4J_URI is required")
    # The subscription CLI login only: never an API key, never a paid fallback.
    if os.environ.get("MEMORY_LLM") != "codex" or any(
        os.environ.get(k) for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "MEMORY_LLM_API_KEY")
    ):
        parser.error("Set MEMORY_LLM=codex and unset API keys: this eval uses the CLI login only")
    os.environ["NEO4J_URI"] = uri
    if "MEMORY_TEST_NEO4J_PASSWORD" in os.environ:
        os.environ["NEO4J_PASSWORD"] = os.environ["MEMORY_TEST_NEO4J_PASSWORD"]
    from graph_memory.cli import build_service

    service = build_service()
    try:
        engine = engine_fingerprint()
        results = []
        for repeat in range(args.runs):
            for case in load_cases():
                results.append({"repeat": repeat, **run_case(service, case)})
                print(
                    json.dumps({k: results[-1].get(k) for k in ("case", "repeat", "passed")}),
                    flush=True,
                )
        report = {
            "commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
            ).stdout.strip(),
            "engine": engine,
            "engine_unchanged": engine == engine_fingerprint(fresh=True),
            "model": service.llm.model,
            "reasoning_effort": service.llm.effort,
            "runs": args.runs,
            "passed": all(r["passed"] for r in results)
            and engine == engine_fingerprint(fresh=True),
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"passed": report["passed"], "report": str(args.output)}))
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        service.store.close()


if __name__ == "__main__":
    main()
