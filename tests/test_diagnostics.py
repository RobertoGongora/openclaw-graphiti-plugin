import json

import pytest
from pydantic import ValidationError

from graph_memory.daemon import run_daemon
from graph_memory.diagnostics import diagnostic
from graph_memory.journal import Journal
from graph_memory.models import Extraction
from graph_memory.service import MemoryService


def test_schema_diagnostics_omit_private_values_and_unknown_field_names():
    secret = "private-source-and-token"
    with pytest.raises(ValidationError) as caught:
        Extraction.model_validate(
            {
                "entities": [{"key": "project:a", "name": "A", "kind": secret}],
                "facts": [],
                secret: secret,
            }
        )
    result = diagnostic(caught.value, "model_output")
    assert result["code"] == "schema_validation"
    assert secret not in json.dumps(result)
    assert result["issues"][0]["location"] == ["entities", 0, "kind"]
    assert result["issues"][1]["location"] == ["<field>"]
    with pytest.raises(ValidationError) as caught:
        Extraction.model_validate(
            {"entities": [], "facts": [], **{f"secret-{i}": i for i in range(30)}}
        )
    result = diagnostic(caught.value)
    assert result["issue_count"] == 30
    assert len(result["issues"]) == 10


def test_known_reasons_are_specific_and_unknown_errors_remain_private():
    assert (
        diagnostic(ValueError("Evidence must quote an exact substring of its source message"))[
            "code"
        ]
        == "evidence_quote_mismatch"
    )
    assert (
        diagnostic(
            ValueError("Ambiguous identity for private-person; merge or disambiguate explicitly")
        )["code"]
        == "ambiguous_identity"
    )
    assert "private-person" not in json.dumps(
        diagnostic(
            ValueError("Ambiguous identity for private-person; merge or disambiguate explicitly")
        )
    )
    assert (
        diagnostic(RuntimeError("Codex extraction timed out; durable input can be retried"))["code"]
        == "model_timeout"
    )
    result = diagnostic(
        RuntimeError(
            "Codex model invocation failed (exit -15); check CLI authentication/model availability"
        )
    )
    assert result["exit_code"] == -15
    assert result["code"] == "model_invocation_failed"
    assert diagnostic(RuntimeError("password=do-not-log"))["code"] == "unclassified_error"
    assert "password" not in json.dumps(diagnostic(RuntimeError("password=do-not-log")))


def test_rejected_evidence_reaches_error_stream_without_changing_knowledge(graph, tmp_path, capsys):
    store, ns = graph
    (tmp_path / "note.md").write_text("A uses MySQL.")

    class Model:
        calls = 0

        def generate(self, *args):
            self.calls += 1
            return Extraction.model_validate(
                {
                    "entities": [
                        {"key": "a", "name": "A", "kind": "project"},
                        {"key": "db", "name": "MySQL", "kind": "database"},
                    ],
                    "facts": [
                        {
                            "subject": "a",
                            "target": "db",
                            "relation": "uses_database",
                            "summary": "Private candidate text",
                            "evidence": [
                                {"message_id": "missing", "quote": "Private candidate text"}
                            ],
                        }
                    ],
                }
            )

    model = Model()
    run_daemon(MemoryService(store, model), ns, [tmp_path], workers=1, once=True)
    output = capsys.readouterr().out
    failure = next(
        json.loads(line) for line in output.splitlines() if json.loads(line)["event"] == "processed"
    )
    assert model.calls == 2  # Existing evidence correction behavior is preserved.
    assert failure["diagnostic"]["code"] == "evidence_quote_mismatch"
    assert failure["diagnostic"]["stage"] == "evidence_validation"
    assert failure["diagnostic"]["location"] == ["facts", 0, "evidence", 0, "quote"]
    assert failure["diagnostic"]["extraction_attempt"] == 2
    assert failure["diagnostic"]["duration_seconds"] >= 0
    assert failure["failed_attempts"] == 1
    assert failure["retry_after"] > 0
    assert "Private candidate text" not in output
    episode = store.episode(ns, failure["episode_id"])
    assert episode["lease_until"] == 0
    assert episode["error"] == "ValueError"
    assert [e["kind"] for e in Journal(store).events(ns)] == ["baseline", "source_saved"]
    assert Journal(store).verify(ns)["verified"]
    assert store.recall(ns, "A")["current"] == []
