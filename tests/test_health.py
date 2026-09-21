"""Liveness is observable: a heartbeat, a health verdict, and status that reports both."""

import os

import pytest

from graph_memory import health, settings, status
from graph_memory.daemon import run_daemon
from graph_memory.feeds import claude_config
from graph_memory.models import Extraction, Scope
from graph_memory.service import MemoryService
from graph_memory.version import ENGINE_FILES, engine_fingerprint


class Empty:
    timeout = 600

    def generate(self, instructions, payload, output):
        return Extraction(entities=[], facts=[])


def test_daemon_heartbeat_feeds_health_and_status(graph, tmp_path, monkeypatch):
    store, ns = graph
    monkeypatch.setenv("NEO4J_URI", os.environ["MEMORY_TEST_NEO4J_URI"])
    assert health.check("worker", ns) == (False, {"role": "worker", "reason": "no_heartbeat"})
    (tmp_path / "note.md").write_text("No durable information.")
    service = MemoryService(store, Empty())
    run_daemon(service, ns, [tmp_path], workers=1, once=True)
    ok, detail = health.check("worker", ns)
    assert ok and detail["heartbeat_age"] < 60
    worker = status.status(store, Scope(namespace=ns))["workers"][0]
    assert worker["alive"] and worker["same_engine"] and worker["provider_unavailable"] is None
    store.transaction(
        lambda tx: tx.run(
            "MATCH (w:MemoryWorker {namespace:$ns}) SET w.heartbeat_at=w.heartbeat_at-3600", ns=ns
        ).consume()
    )
    assert health.check("worker", ns)[0] is False
    assert status.status(store, Scope(namespace=ns))["workers"][0]["alive"] is False
    store.transaction(
        lambda tx: tx.run("MATCH (w:MemoryWorker {namespace:$ns}) DELETE w", ns=ns).consume()
    )
    # A half-written or older worker record must not break status or health.
    store.transaction(
        lambda tx: tx.run(
            "CREATE (:MemoryWorker {id:$id,namespace:$ns})", id=ns + ":old", ns=ns
        ).consume()
    )
    assert status.status(store, Scope(namespace=ns))["workers"] == []
    with pytest.raises(ValueError, match="role must be"):
        health.check("scheduler", ns)


def test_limits_come_from_the_environment_with_bounds(monkeypatch):
    assert settings.summary() == {"intake_queue": 32, "intake_files": 4, "llm_timeout": 420}
    monkeypatch.setenv("MEMORY_INTAKE_QUEUE", "64")
    monkeypatch.setenv("MEMORY_LLM_TIMEOUT", "300")
    assert settings.intake_queue() == 64
    assert settings.lease_seconds(None) == 300 * 4 + 60
    monkeypatch.setenv("MEMORY_INTAKE_FILES", "0")
    with pytest.raises(ValueError, match="MEMORY_INTAKE_FILES must be between"):
        settings.intake_files()


def test_client_config_never_copies_secrets(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "hunter2")
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", "token-value")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "key-value")
    monkeypatch.setenv("MEMORY_LLM", "codex")
    env = claude_config("personal")["mcp"]["mcpServers"]["graph-memory"]["env"]
    assert env["NEO4J_PASSWORD"] == "${NEO4J_PASSWORD}" and env["MEMORY_LLM"] == "caller"
    assert not {"hunter2", "token-value", "key-value"} & set(env.values())
    assert "MEMORY_HTTP_TOKEN" not in env and "MEMORY_LLM_API_KEY" not in env


def test_engine_identity_ignores_operational_code():
    assert "cli.py" not in ENGINE_FILES and "status.py" not in ENGINE_FILES
    assert {"models.py", "llm.py", "extraction_policy.py", "store.py"} <= set(ENGINE_FILES)
    assert engine_fingerprint() == engine_fingerprint(fresh=True)
