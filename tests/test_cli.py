import io
import json
import os
import signal
import threading

import pytest
from neo4j.exceptions import ServiceUnavailable

from graph_memory import cli
from graph_memory.service import MemoryService


class Store:
    closed = False

    def close(self):
        self.closed = True


def invoke(monkeypatch, *argv, stdin=""):
    monkeypatch.setattr(cli.sys, "argv", ["graph-memory", *argv])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin))
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


@pytest.fixture
def offline(monkeypatch):
    """Any attempt to reach the database fails the way a stopped Neo4j does."""
    calls = []

    def build():
        calls.append(1)
        raise ServiceUnavailable("bolt://secret-host refused")

    monkeypatch.setattr(cli, "build_service", build)
    return calls


@pytest.fixture
def service(monkeypatch):
    built = MemoryService(Store())
    monkeypatch.setattr(cli, "build_service", lambda: built)
    return built


def test_version_prints_package_and_engine_without_a_database(monkeypatch, capsys, offline):
    from graph_memory import __version__
    from graph_memory.version import engine_fingerprint

    assert invoke(monkeypatch, "--version") == 0
    assert capsys.readouterr().out.split() == [
        "graph-memory",
        __version__,
        "engine",
        engine_fingerprint(),
    ]
    assert not offline


@pytest.mark.parametrize(
    "argv",
    [
        ("work", "--limit", "0"),
        ("work", "--limit", "101"),
        ("work", "--interval", "0.5"),
        ("work",),
        ("daemon", "--workers", "0"),
        ("daemon", "--workers", "17"),
        ("daemon", "--interval", "0"),
        ("daemon",),
        ("follow", "/nonexistent/transcript.jsonl"),
    ],
)
def test_arguments_are_rejected_before_connecting(monkeypatch, capsys, offline, argv):
    monkeypatch.setenv("MEMORY_LLM", "caller")
    assert invoke(monkeypatch, *argv) == 2
    assert not offline
    assert "Traceback" not in capsys.readouterr().err


def test_database_outage_exits_69_in_one_line_and_debug_shows_the_traceback(
    monkeypatch, capsys, offline
):
    assert invoke(monkeypatch, "repair") == 69
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "ServiceUnavailable" in err and "secret-host" not in err
    with pytest.raises(ServiceUnavailable):
        invoke(monkeypatch, "--debug", "repair")


def test_call_reads_arguments_from_a_file_and_closes_the_store(
    monkeypatch, capsys, service, tmp_path
):
    seen = []
    tools = service.tools()
    schema = tools["memory_status"][0]
    tools["memory_status"] = (schema, lambda r: seen.append(r.namespace) or {"ok": True}, "")
    monkeypatch.setattr(service, "tools", lambda: tools)
    arguments = tmp_path / "arguments.json"
    arguments.write_text(json.dumps({"namespace": "from-file"}))
    assert invoke(monkeypatch, "call", "memory_status", f"@{arguments}") == 0
    assert seen == ["from-file"] and json.loads(capsys.readouterr().out) == {"ok": True}
    assert service.store.closed


def test_call_failures_map_to_exit_codes(monkeypatch, capsys, service, tmp_path):
    assert invoke(monkeypatch, "call", "memory_nope", "{}") == 2
    assert "Unknown tool" in capsys.readouterr().err
    assert invoke(monkeypatch, "call", "memory_status", "{not json") == 2
    assert invoke(monkeypatch, "call", "memory_status", '{"namespace": 5, "x": "private"}') == 2
    err = capsys.readouterr().err
    assert "invalid input" in err and "private" not in err and "Traceback" not in err
    assert invoke(monkeypatch, "call", "memory_status", f"@{tmp_path}/missing.json") == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "FileNotFoundError" in err


HOOK = {"hook_event_name": "UserPromptSubmit", "session_id": "s"}


def test_hook_emits_instructions_even_when_the_database_is_down(
    monkeypatch, capsys, offline, tmp_path
):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    payload = json.dumps({**HOOK, "transcript_path": str(transcript)})
    assert invoke(monkeypatch, "--namespace", "ns1", "hook", stdin=payload) == 0
    captured = capsys.readouterr()
    context = json.loads(captured.out)["hookSpecificOutput"]["additionalContext"]
    assert "Memory namespace: ns1." in context
    assert offline and captured.err.count("\n") == 1 and "ServiceUnavailable" in captured.err
    stop = json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript)})
    assert invoke(monkeypatch, "hook", stdin=stop) == 0
    assert json.loads(capsys.readouterr().out)["continue"] is True
    assert invoke(monkeypatch, "hook", stdin="not json") == 0


def test_hook_prints_instructions_before_staging_and_never_connects_without_a_transcript(
    monkeypatch, capsys, service, tmp_path
):
    order = []
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")

    def feed(built, namespace, path, session_id):
        order.append((capsys.readouterr().out != "", namespace, path, session_id))
        return {"receipts": [{}]}

    monkeypatch.setattr("graph_memory.feeds.feed", feed)
    payload = json.dumps({**HOOK, "transcript_path": str(transcript)})
    assert invoke(monkeypatch, "hook", stdin=payload) == 0
    assert order == [(True, "personal", transcript, "s")]
    assert service.store.closed
    stop = json.dumps(
        {"hook_event_name": "Stop", "session_id": "s", "transcript_path": str(transcript)}
    )
    assert invoke(monkeypatch, "hook", stdin=stop) == 0
    assert "queued 1 transcript" in json.loads(capsys.readouterr().out)["systemMessage"]
    monkeypatch.setattr(cli, "build_service", lambda: 1 / 0)
    assert invoke(monkeypatch, "hook", stdin=json.dumps(HOOK)) == 0
    assert capsys.readouterr().err == ""


def test_serve_http_wires_env_lists_and_sigterm_shuts_down_and_closes_the_store(
    monkeypatch, service
):
    monkeypatch.setenv("MEMORY_HTTP_ORIGINS", "http://localhost:8766, https://app.test")
    monkeypatch.setenv("MEMORY_HTTP_HOSTS", "memory:8765")
    monkeypatch.delenv("MEMORY_HTTP_TOKEN", raising=False)
    wired = {}
    real = cli.http_server

    def server(protocol, host, port, token, origins, hosts):
        wired.update(origins=origins, hosts=hosts)
        wired["server"] = real(protocol, host, port, token, origins, hosts)
        serve = wired["server"].serve_forever

        def serving():
            # Deliver the signal only once the loop it must interrupt is running.
            threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM)).start()
            serve()

        wired["server"].serve_forever = serving
        return wired["server"]

    monkeypatch.setattr(cli, "http_server", server)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert invoke(monkeypatch, "serve", "--transport", "http", "--port", "0") == 0
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert wired["origins"] == ["http://localhost:8766", "https://app.test"]
    assert wired["hosts"] == ["memory:8765"]
    assert wired["server"].socket.fileno() == -1 and service.store.closed


def test_work_watch_stops_cleanly_on_sigterm(monkeypatch, service):
    service.llm = object()
    monkeypatch.setenv("MEMORY_LLM", "codex")
    ticks = []

    def tick(*_):
        ticks.append(1)
        os.kill(os.getpid(), signal.SIGTERM)
        return {"receipts": []}

    monkeypatch.setattr("graph_memory.feeds.worker_tick", tick)
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert invoke(monkeypatch, "work", "--watch", "--interval", "30") == 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    assert ticks == [1] and service.store.closed


def test_health_prints_one_json_line_and_never_builds_the_service(monkeypatch, capsys, offline):
    import sys
    import types

    outcome = {}

    def check(role, namespace):
        if "raise" in outcome:
            raise ConnectionError("bolt://secret-host")
        return outcome["ok"], {"ok": outcome["ok"], "role": role, "namespace": namespace}

    monkeypatch.setitem(sys.modules, "graph_memory.health", types.SimpleNamespace(check=check))
    for ok, code in ((True, 0), (False, 1)):
        outcome["ok"] = ok
        assert invoke(monkeypatch, "--namespace", "ns1", "health", "--role", "worker") == code
        out = capsys.readouterr().out
        assert out.count("\n") == 1
        assert json.loads(out) == {"ok": ok, "role": "worker", "namespace": "ns1"}
    outcome["raise"] = True
    assert invoke(monkeypatch, "health", "--role", "mcp") == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": False, "error": "ConnectionError"}
    assert captured.err == "" and not offline
    assert invoke(monkeypatch, "health", "--role", "other") == 2
