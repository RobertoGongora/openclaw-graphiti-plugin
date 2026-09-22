import json
from types import SimpleNamespace

import pytest

from evals import vocabulary
from graph_memory.models import Extraction, Transcript
from tests.helpers import MYSQL, PROJECT


def test_vocabulary_resumes_completed_batches_and_refuses_different_inputs(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.json"
    output = tmp_path / "report.json"
    batches = [
        Transcript(
            namespace="eval:test",
            session_id="test",
            source_id=str(i),
            messages=[{"id": str(i), "role": "user", "content": "Atlas uses MySQL."}],
        ).model_dump(mode="json")
        for i in range(3)
    ]
    corpus.write_text(json.dumps(batches))
    monkeypatch.setattr(
        "sys.argv",
        [
            "vocabulary",
            str(tmp_path),
            "--corpus",
            str(corpus),
            "--output",
            str(output),
            "--details-dir",
            str(tmp_path / "details"),
        ],
    )
    monkeypatch.setattr(
        vocabulary, "configured_llm", lambda: SimpleNamespace(model="test", effort="low")
    )
    calls = []

    def interrupted(self, packet, progress=None):
        calls.append(packet["transcript"]["source_id"])
        if len(calls) == 2:
            raise KeyboardInterrupt
        return Extraction(
            entities=[PROJECT, MYSQL],
            facts=[
                {
                    "subject": PROJECT["key"],
                    "target": MYSQL["key"],
                    "relation": "uses_database",
                    "slot": "primary",
                    "summary": "Atlas uses MySQL.",
                    "evidence": [
                        {
                            "message_id": packet["transcript"]["source_id"],
                            "quote": "Atlas uses MySQL.",
                        }
                    ],
                }
            ],
        ), 1

    monkeypatch.setattr(vocabulary.MemoryService, "propose", interrupted)
    with pytest.raises(KeyboardInterrupt):
        vocabulary.main()
    partial = json.loads(output.read_text())
    assert partial["totals"]["batches"] == 1 and not partial["complete"]
    assert partial["slots"]["single_use"] == 1
    vocabulary.main()
    final = json.loads(output.read_text())
    assert calls == ["0", "1", "1", "2"]
    assert final["complete"] and final["totals"]["model_calls"] == 3
    assert final["totals"]["facts"] == final["relations"]["uses_database"] == 3
    assert [r["index"] for r in final["batch_results"]] == [0, 1, 2]
    assert sum(r["totals"]["facts"] for r in final["batch_results"]) == 3
    details = json.loads((tmp_path / "details/000.json").read_text())
    assert details["accepted"] and details["extraction"]["facts"][0]["slot"] == "primary"
    assert final["slots"] == {"distinct": 1, "shared": 1, "single_use": 0}
    vocabulary.main()
    assert calls == ["0", "1", "1", "2"]  # Already complete: no further model calls.
    batches[0]["messages"][0]["content"] = "Atlas uses PostgreSQL."
    corpus.write_text(json.dumps(batches))
    with pytest.raises(SystemExit, match="differs"):
        vocabulary.main()


def test_vocabulary_records_failed_batch_before_continuing(tmp_path, monkeypatch):
    corpus, output = tmp_path / "corpus.json", tmp_path / "report.json"
    corpus.write_text(
        json.dumps(
            [
                Transcript(
                    namespace="eval:test",
                    session_id="test",
                    source_id="1",
                    messages=[{"id": "1", "role": "user", "content": "Atlas uses MySQL."}],
                ).model_dump(mode="json")
            ]
        )
    )
    monkeypatch.setattr(
        "sys.argv", ["vocabulary", str(tmp_path), "--corpus", str(corpus), "--output", str(output)]
    )
    monkeypatch.setattr(
        vocabulary, "configured_llm", lambda: SimpleNamespace(model="test", effort="low")
    )

    def fail(self, packet, progress=None):
        progress.update(stage="evidence_validation", attempt=1)
        raise ValueError("Evidence must quote an exact substring of its source message")

    monkeypatch.setattr(vocabulary.MemoryService, "propose", fail)
    vocabulary.main()
    report = json.loads(output.read_text())
    assert report["complete"] and report["totals"]["failed:ValueError"] == 1
    assert report["totals"]["seconds"] >= 0
    assert report["totals"]["model_calls"] == 2
    assert report["batch_results"][0]["diagnostic"]["code"] == "evidence_quote_mismatch"
