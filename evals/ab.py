"""Real fresh-session Claude native-memory vs MCP comparison, isolated and trace-scored."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from graph_memory.cli import build_service
from graph_memory.feeds import AGENT_INSTRUCTIONS
from graph_memory.importers import redact, transcripts
from graph_memory.models import Ingest
from graph_memory.version import engine_fingerprint

ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "migration_status": {
            "type": "string",
            "enum": ["paused", "planned", "deployed", "unknown"],
        },
        "deployment_confirmed": {"type": "boolean"},
        "live_verified": {"type": "boolean"},
        "needs_live_verification": {"type": "boolean"},
        "evidence_quotes": {
            "type": "array",
            "minItems": 1,
            "description": "Exact contiguous verbatim excerpts from retrieved source evidence. Preserve original punctuation and Markdown markers; do not paraphrase or insert ellipses.",
            "items": {"type": "string"},
        },
        "answer": {"type": "string"},
    },
}
ANSWER_SCHEMA["required"] = list(ANSWER_SCHEMA["properties"])


def run_session(mode, directory, bank, namespace, case, model):
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="0" if mode == "native" else "1")
    settings = {"autoMemoryEnabled": mode == "native", "disableAllHooks": True}
    if mode == "native":
        settings["autoMemoryDirectory"] = str(bank) + "/"
    mcp = {"mcpServers": {}}
    if mode == "graph":
        mcp["mcpServers"]["graph-memory"] = {
            "command": sys.executable,
            "args": ["-m", "graph_memory.cli", "--namespace", namespace, "serve", "--read-only"],
            "env": {k: v for k, v in env.items() if k.startswith(("NEO4J_", "MEMORY_"))},
        }
    cmd = [
        "claude",
        "-p",
        case["question"],
        "--model",
        model,
        "--setting-sources",
        "",
        "--settings",
        json.dumps(settings),
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps(mcp),
        "--restricted",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--tools",
        "Read,Glob,Grep" if mode == "native" else "",
        "--allowedTools",
        "Read,Glob,Grep"
        if mode == "native"
        else "mcp__graph-memory__memory_recall,mcp__graph-memory__memory_latest,mcp__graph-memory__memory_pending",
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        json.dumps(ANSWER_SCHEMA),
    ]
    if mode == "native":
        cmd += ["--add-dir", str(bank)]
    else:
        cmd += [
            "--append-system-prompt",
            AGENT_INSTRUCTIONS + f"\nAuthorized namespace: {namespace}.",
        ]
    run = subprocess.run(cmd, cwd=directory, env=env, capture_output=True, text=True, timeout=600)
    if run.returncode:
        raise RuntimeError(
            f"Claude {mode} session failed: exit {run.returncode}; {run.stderr[-500:]}"
        )
    events = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    results = [e for e in events if e.get("type") == "result"]
    if not results or results[-1].get("is_error"):
        raise RuntimeError(f"Claude {mode} did not complete successfully")
    result = results[-1]
    calls = [
        c
        for e in events
        for c in e.get("message", {}).get("content", [])
        if isinstance(c, dict) and c.get("type") == "tool_use"
    ]
    answer = result.get("structured_output")
    if answer is None:
        # Older CLI versions return JSON in the final text instead.
        answer = json.loads(result["result"])
    sources = "\n".join(p.read_text() for p in bank.glob("*.md"))
    checks = score_session(mode, case, answer, calls, events, sources, result)
    return {
        "mode": mode,
        "session_id": result.get("session_id"),
        "answer": answer,
        "checks": checks,
        "passed": all(checks.values()),
        "tool_calls": calls,
        "model_usage": result.get("modelUsage"),
        "cost_usd": result.get("total_cost_usd"),
        "events": events,
    }


def score_session(mode, case, answer, calls, events, sources, result):
    checks = {f"answer.{k}": answer.get(k) == v for k, v in case["expected"].items()}
    checks["quoted_original_evidence"] = bool(answer.get("evidence_quotes")) and all(
        isinstance(q, str) and len(q) >= 8 and q in sources
        for q in answer.get("evidence_quotes", [])
    )
    succeeded = {
        c.get("tool_use_id")
        for e in events
        for c in e.get("message", {}).get("content", [])
        if isinstance(c, dict) and c.get("type") == "tool_result" and not c.get("is_error", False)
    }

    def retrieved(call):
        if call.get("id") not in succeeded:
            return False
        if mode == "graph":
            return call["name"].endswith("__memory_recall")
        args = call.get("input", {})
        content_read = call["name"] == "Read" or (
            call["name"] == "Grep" and args.get("output_mode") == "content"
        )
        return content_read and any(name in str(args) for name in case["source_files"])

    checks["retrieved_memory"] = any(retrieved(c) for c in calls)
    checks["no_denied_tools"] = not result.get("permission_denials")
    checks["memory_source_isolation"] = mode == "native" or not any(
        c["name"] in ("Read", "Glob", "Grep", "Bash") for c in calls
    )
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path, required=True)
    parser.add_argument(
        "--case", type=Path, default=Path(__file__).parent / "ab/atlas-postgres.json"
    )
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--output", type=Path, default=Path(".local/ab-report.json"))
    args = parser.parse_args()
    if not os.environ.get("MEMORY_TEST_NEO4J_URI"):
        parser.error("Explicit MEMORY_TEST_NEO4J_URI required")
    os.environ["NEO4J_URI"] = os.environ["MEMORY_TEST_NEO4J_URI"]
    if "MEMORY_TEST_NEO4J_PASSWORD" in os.environ:
        os.environ["NEO4J_PASSWORD"] = os.environ["MEMORY_TEST_NEO4J_PASSWORD"]
    case = json.loads(args.case.read_text())
    namespace = "eval:ab:" + str(uuid.uuid4())
    service = build_service()
    engine = engine_fingerprint()
    report = {
        "engine": engine,
        "case": case,
        "namespace": namespace,
        "model": args.model,
        "corpus": [],
        "sessions": [],
        "passed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="graph-memory-ab-") as tmp:
            root = Path(tmp)
            bank = root / "memory"
            bank.mkdir()
            # Identical redacted bytes/dates in both arms; originals are never writable by either.
            for source in sorted(args.memory_dir.glob("*.md")):
                target = bank / source.name
                target.write_text(redact(source.read_text()))
                shutil.copystat(source, target)
                report["corpus"].append(
                    {
                        "name": source.name,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "updated_at": target.stat().st_mtime,
                    }
                )
            # A focused bank is supported, but the expected evidence must actually be present.
            if not all((bank / name).is_file() for name in case["source_files"]):
                raise ValueError("Known-answer source files are missing")
            native_dir, graph_dir = root / "native", root / "graph"
            native_dir.mkdir()
            graph_dir.mkdir()
            native = run_session("native", native_dir, bank, namespace, case, args.model)
            report["sessions"].append(native)
            print(
                json.dumps(
                    {"mode": "native", "passed": native["passed"], "checks": native["checks"]}
                ),
                flush=True,
            )
            for source in sorted(bank.glob("*.md")):
                # The index is navigation, not additional factual evidence.
                if source.name == "MEMORY.md":
                    continue
                for transcript in transcripts(source, namespace):
                    receipt = service.ingest(Ingest(transcript=transcript, extract=True))
                    if receipt["status"] != "complete":
                        raise ValueError("A/B graph extraction is incomplete")
                print(json.dumps({"ingested": source.name}), flush=True)
            graph = run_session("graph", graph_dir, bank, namespace, case, args.model)
            report["sessions"].append(graph)
            report["passed"] = (
                all(s["passed"] for s in report["sessions"]) and engine == engine_fingerprint()
            )
            report["engine_unchanged"] = engine == engine_fingerprint()
            report["sessions_independent"] = native["session_id"] != graph["session_id"]
            report["passed"] &= report["sessions_independent"]
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        service.store.transaction(
            lambda tx: tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n",
                ns=namespace,
            ).consume()
        )
        service.store.close()
    print(json.dumps({"passed": report["passed"], "report": str(args.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
