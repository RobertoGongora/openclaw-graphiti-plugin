"""Synthetic sessions through the real model: English fact summaries, verbatim
quotes, exact identifiers, kept polarity, and an English question that finds the
eventual decision. Checks are fixed per case; no model judges a model."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from graph_memory.llm import extraction_instructions
from graph_memory.models import Extraction, Ingest, Transcript
from graph_memory.version import engine_fingerprint

CASES = Path(__file__).parent / "english" / "cases.json"
NEGATORS = {"not", "never", "without", "no", "nor"}


def load_cases():
    return json.loads(CASES.read_text())


def transcript(case, namespace):
    return Transcript(
        namespace=namespace,
        source_id="english-" + case["id"],
        session_id="english-" + case["id"],
        source_format="session-records-v1",
        messages=[
            {k: v for k, v in m.items() if k != "lang"}
            | {"role": "user", "source_type": "user_assertion"}
            for m in case["messages"]
        ],
    )


def words(text):
    return set(re.findall(r"[^\W\d_]+", text.lower()))


def negated(text, start):
    """A negator among the three words before start, in the same clause: "was not
    restarted", "without restarting". A "no" elsewhere, as in "restarted with no
    downtime" or "had no downtime. It restarted", is not one."""
    clause = re.split(r"[.;:!?,]", text[:start])[-1]
    before = re.findall(r"[a-z]+(?:'[a-z]+)?", clause.lower())[-3:]
    return any(w in NEGATORS or w.endswith("n't") for w in before)


def exact(identifier, text):
    """Case and characters as in the source, whole token: v3.14.2 is not 3.14.2."""
    return re.search(r"(?<![\w.-])" + re.escape(identifier) + r"(?![\w-]|\.\w)", text)


def affirmed(pattern, text):
    """Matches of pattern whose claim (the named group, or the whole match) is not negated."""
    out = []
    for match in re.finditer(pattern, text, re.I):
        start = match.start("claim") if "claim" in match.groupdict() else match.start()
        if not negated(text, start):
            out.append(match.group(0))
    return out


def selected(facts, rule):
    return [
        f
        for f in facts
        if f["status"] in rule.get("status", [f["status"]])
        and f["relation"] in rule.get("relation", [f["relation"]])
        and rule.get("target", "") in f["target"]
    ]


def check_case(case, source, facts, found, entities=()):
    """facts and entities: the committed extraction. found: memory_search facts for
    the case's English question, best first, with their lanes."""
    content = {m.id: m.content for m in source.messages}
    summaries = [f["summary"] for f in facts]
    text = "\n".join(summaries)
    checks = []

    def record(name, passed, actual=None):
        checks.append({"check": name, "passed": bool(passed), "actual": actual})

    record("facts extracted", facts, len(facts))
    # Words of this case's Spanish source that an English summary has no use for.
    spanish = (
        {
            w
            for m in case["messages"]
            if m.get("lang", "es") == "es"
            for w in words(m["content"])
            if len(w) >= 4
        }
        - {w.lower() for w in case.get("shared", [])}
        - words(" ".join(case["identifiers"]))
    )
    leaked = {s: sorted(words(s) & spanish) for s in summaries if words(s) & spanish}
    record("no Spanish word of the source in a summary", not leaked, leaked)
    quotes = [e for f in facts for e in f["evidence"] + f.get("validation_evidence", [])]
    record(
        "every committed quote is verbatim source text",
        quotes and all(e["quote"] in content.get(e["message_id"], "") for e in quotes),
        [e for e in quotes if e["quote"] not in content.get(e["message_id"], "")],
    )
    missing = [i for i in case["identifiers"] if not exact(i, text)]
    record("identifiers kept exactly in summaries", not missing, missing)
    for alias in case.get("aliases", []):
        names = [n for e in entities for n in [e["name"], *e.get("aliases", [])]]
        loose = "[-_ ]?".join(re.escape(c) for c in alias.replace("-", "").replace("_", ""))
        variants = [m for m in re.findall(loose, text, re.I) if m != alias]
        record(
            f"alias {alias} kept exactly",
            (exact(alias, text) or alias in names) and not variants,
            variants,
        )
    rank = case.get("answer_rank", 1)
    record(
        "English question finds the eventual answer first",
        any(
            all(t in f["text"] for t in case.get("top_contains", []))
            and case.get("top_target", "") in f["target"]
            for f in found[:rank]
        ),
        [(f["lane"], f["target"], f["text"]) for f in found[:3]],
    )
    # Judged on what retrieval serves: a superseded choice may be extracted as
    # planned and still be moved to history by its slot.
    wrong = [
        (f["lane"], f["target"], f["text"])
        for f in found
        if f["lane"] in {"current", "planned"}
        and any(t in f["target"] for t in case.get("not_current_targets", []))
    ]
    if case.get("not_current_targets"):
        record("only the eventual choice is current", not wrong, wrong)
    for rule in case.get("negated", []):
        hits = [(s, m.start()) for s in summaries for m in re.finditer(rule["terms"], s, re.I)]
        record(
            f"{rule['name']} stays negated",
            hits and all(negated(s, at) for s, at in hits),
            [s for s, _ in hits] or "missing",
        )
    for rule in case.get("forbidden", []):
        bad = [
            f["summary"]
            for f in selected(facts, rule)
            if "pattern" not in rule or affirmed(rule["pattern"], f["summary"])
        ]
        record(f"not claimed: {rule['name']}", not bad, bad)
    for rule in case.get("required", []):
        good = [
            f["summary"] for f in selected(facts, rule) if affirmed(rule["pattern"], f["summary"])
        ]
        record(f"claimed: {rule['name']}", good, good or summaries)
    return checks


def search(service, namespace, case):
    return service.call(
        "memory_search", {"namespace": namespace, "question": case["question"], "limit": 20}
    )


def run_case(service, case):
    namespace = "eval:" + str(uuid.uuid4())
    source = transcript(case, namespace)
    raw = []
    generate = service.llm.generate

    def recording(instructions, payload, output):
        result = generate(instructions, payload, output)
        if output is Extraction:
            raw.append(result.model_dump(mode="json"))  # Before quote repair and commit.
        return result

    service.llm.generate = recording
    try:
        receipt = service.ingest(Ingest(transcript=source, extract=True))
        episode = service.store.episode(namespace, receipt["episode_id"])
        committed = json.loads(episode.get("extraction_payload") or '{"entities":[],"facts":[]}')
        found = search(service, namespace, case)["facts"]
        checks = check_case(case, source, committed["facts"], found, committed["entities"])
        content = {m.id: m.content for m in source.messages}
        return {
            "case": case["id"],
            "status": episode["status"],
            "passed": all(c["passed"] for c in checks),
            "checks": checks,
            "committed": committed,
            # The model's own output per call: quotes the engine had to repair show
            # here as not verbatim, while the committed evidence above is literal.
            "raw_outputs": raw,
            "raw_quotes_not_verbatim": [
                e
                for out in raw
                for f in out["facts"]
                for e in f["evidence"] + f.get("validation_evidence", [])
                if e["quote"] not in content.get(e["message_id"], "")
            ],
            "search": found[:5],
        }
    except Exception as exc:
        return {
            "case": case["id"],
            "passed": False,
            "error": type(exc).__name__,
            "detail": str(exc)[:2000],
            "raw_outputs": raw,
        }
    finally:
        service.llm.generate = generate
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
        started = datetime.now(UTC).isoformat()
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
            "dirty": bool(
                subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
            ),
            "engine": engine,
            "engine_unchanged": engine == engine_fingerprint(fresh=True),
            "prompt_sha256": hashlib.sha256(
                extraction_instructions(transcript(load_cases()[0], "x")).encode()
            ).hexdigest(),
            "cases_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
            "cli": subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, check=False
            ).stdout.strip(),
            "login": "subscription CLI login; API key variables unset",
            "model": service.llm.model,
            "reasoning_effort": service.llm.effort,
            "runs": args.runs,
            "started": started,
            "finished": datetime.now(UTC).isoformat(),
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
