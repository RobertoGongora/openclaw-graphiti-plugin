"""Compare efforts on frozen prepare packets, without any database connection.

Exercises the production model adapter, correction loops, schema, and evidence
validation. The commit sink only counts validated output; graph identity resolution
and semantic accuracy are not evaluated. Inputs and reports must remain private.
"""

import argparse
import json
import time
from pathlib import Path

from graph_memory.diagnostics import diagnostic
from graph_memory.llm import CodexLLM
from graph_memory.models import EpisodeRequest, Transcript
from graph_memory.service import MemoryService
from graph_memory.store import digest
from graph_memory.version import engine_fingerprint


class ValidationSink:
    def __init__(self, packet):
        self.packet = packet
        self.engine = engine_fingerprint()

    def assert_writable(self, namespace):
        pass

    def failed(self, *args):
        pass

    def commit(self, namespace, episode_id, extraction, **kwargs):
        extraction.validate_evidence(Transcript.model_validate(self.packet["transcript"]))
        return {"facts": len(extraction.facts), "entities": len(extraction.entities)}


class FrozenService(MemoryService):
    def prepare(self, request):
        return self.store.packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--effort", choices=("medium", "high"), required=True)
    args = parser.parse_args()
    cases = json.loads(args.inputs.read_text())
    if args.effort == "high":
        cases.reverse()  # Counterbalance two concurrent effort runs.
    report = {
        "effort": args.effort,
        "engine": engine_fingerprint(),
        "scope": "schema_and_evidence_only",
        "results": [],
    }
    for case in cases:
        assert digest(case["packet"]) == case["input_hash"]
        sink = ValidationSink(case["packet"])
        service = FrozenService(sink, CodexLLM(effort=args.effort))
        result = {"input_hash": case["input_hash"], "effort": args.effort}
        started = time.monotonic()
        try:
            result.update(
                service.extract(
                    EpisodeRequest(
                        namespace=case["packet"]["transcript"]["namespace"],
                        episode_id=case["episode_id"],
                    )
                )
            )
            result["passed"] = True
        except Exception as exc:
            result.update(
                passed=False, diagnostic=getattr(exc, "memory_diagnostic", None) or diagnostic(exc)
            )
        result["seconds"] = round(time.monotonic() - started, 3)
        report["results"].append(result)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        args.output.chmod(0o600)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
