import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from graph_memory import cli, relay
from graph_memory.mcp import READ_ONLY, session_server
from graph_memory.relay import relayable
from graph_memory.service import MemoryService

INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def sessions():
    # AF_UNIX paths are capped near 104 bytes on macOS; pytest's tmp_path is longer.
    directory = tempfile.mkdtemp(prefix="gm", dir="/tmp")
    path = os.path.join(directory, "s.sock")
    server = session_server(MemoryService(None), path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, path
    server.shutdown()
    server.server_close()
    thread.join()
    shutil.rmtree(directory, ignore_errors=True)


def spawn(path, *argv):
    return subprocess.Popen(
        [sys.executable, "-m", "graph_memory.relay", *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # As in the image, which changes what sys.stdin and sys.stdout wrap.
        env={
            **os.environ,
            relay.ENV: path,
            "NEO4J_URI": "bolt://127.0.0.1:1",
            "PYTHONUNBUFFERED": "1",
        },
    )


def exchange(child, *messages):
    payload = b"".join(json.dumps(m).encode() + b"\n" for m in messages)
    out, _ = child.communicate(payload, timeout=20)
    return [json.loads(line) for line in out.splitlines()]


def settle(server, expected=0):
    deadline = time.monotonic() + 10
    while server.sessions != expected and time.monotonic() < deadline:
        time.sleep(0.02)
    return server.sessions


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["serve"], ("personal", False)),
        (["--namespace", "t", "serve"], ("t", False)),
        (["--namespace=t", "--debug", "serve", "--transport", "stdio"], ("t", False)),
        (["serve", "--transport=stdio", "--read-only"], ("personal", True)),
        (["serve", "--transport", "http"], None),
        (["serve", "--port", "1"], None),
        (["--namespace"], None),
        (["recall", "x"], None),
        (["health", "--role", "mcp"], None),
        ([], None),
    ],
)
def test_only_plain_stdio_serve_is_relayed(monkeypatch, argv, expected):
    monkeypatch.delenv("MEMORY_NAMESPACE", raising=False)
    assert relayable(argv) == expected


def test_relayed_session_answers_like_stdio_and_keeps_its_bindings(sessions):
    server, path = sessions
    init, listed = exchange(spawn(path, "--namespace", "t", "serve"), INIT, LIST)
    assert init["result"]["serverInfo"]["name"] == "graph-memory"
    tools = {t["name"] for t in listed["result"]["tools"]}
    assert "memory_retract" in tools
    # Namespace stays bound: a call naming another one is refused, not served.
    foreign = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "memory_recall", "arguments": {"namespace": "x", "entity": "A"}},
    }
    (denied,) = exchange(spawn(path, "--namespace", "t", "serve"), foreign)
    assert denied["error"]["code"] == -32001
    (_, listed) = exchange(spawn(path, "--namespace", "t", "serve", "--read-only"), INIT, LIST)
    assert {t["name"] for t in listed["result"]["tools"]} <= READ_ONLY
    assert settle(server) == 0


def test_many_short_lived_clients_leave_no_sessions_behind(sessions):
    server, path = sessions
    for _ in range(3):
        children = [spawn(path, "--namespace", "t", "serve") for _ in range(20)]
        for child in children:
            assert len(exchange(child, INIT, LIST)) == 2
            assert child.returncode == 0
        assert settle(server) == 0
    assert threading.active_count() < 10


def test_a_burst_of_clients_is_relayed_not_served_in_process(sessions):
    server, path = sessions
    children = [spawn(path, "--namespace", "t", "serve") for _ in range(60)]
    held = []
    for child in children:
        assert child.stdin and child.stdout
        child.stdin.write(json.dumps(INIT).encode() + b"\n")
        child.stdin.flush()
        held.append(child)
    for child in held:
        assert json.loads(child.stdout.readline())["id"] == 0
    # Every client is a session on the server; a fallback would not be counted.
    assert settle(server, 60) == 60
    for child in held:
        child.stdin.close()
        child.wait(timeout=20)
    assert settle(server) == 0


def test_a_killed_relay_releases_its_session(sessions):
    server, path = sessions
    child = spawn(path, "--namespace", "t", "serve")
    assert child.stdin and child.stdout
    child.stdin.write(json.dumps(INIT).encode() + b"\n")
    child.stdin.flush()
    assert json.loads(child.stdout.readline())["id"] == 0
    assert settle(server, 1) == 1
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=10)
    assert settle(server) == 0


def test_a_bad_greeting_is_closed_without_a_session(sessions):
    server, path = sessions
    for greeting in (b"nope\n", b'{"namespace": "", "read_only": false}\n', b"[]\n"):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(path)
            client.sendall(greeting)
            assert client.recv(64) == b""
    assert server.sessions == 0


def test_without_a_session_server_the_relay_serves_in_process(monkeypatch):
    monkeypatch.setattr(relay, "WAIT", 0.2)
    calls = []
    monkeypatch.setattr(cli, "main", lambda: calls.append(sys.argv[1:]))
    monkeypatch.setenv(relay.ENV, "/tmp/graph-memory-absent.sock")
    monkeypatch.setattr(sys, "argv", ["graph-memory", "--namespace", "t", "serve"])
    relay.main()
    monkeypatch.setattr(sys, "argv", ["graph-memory", "recall", "x"])
    relay.main()
    monkeypatch.delenv(relay.ENV)
    monkeypatch.setattr(sys, "argv", ["graph-memory", "serve"])
    relay.main()
    assert calls == [["--namespace", "t", "serve"], ["recall", "x"], ["serve"]]


def test_a_relay_started_before_the_server_waits_for_its_socket():
    # Clients reconnect as soon as a restarted container runs, before the server
    # has created its socket; they must not each load an engine meanwhile.
    directory = tempfile.mkdtemp(prefix="gm", dir="/tmp")
    path = os.path.join(directory, "s.sock")
    try:
        child = spawn(path, "--namespace", "t", "serve")
        time.sleep(1)
        server = session_server(MemoryService(None), path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # A fallback would answer too; only a relayed session is counted here.
            assert settle(server, 1) == 1
            assert exchange(child, INIT)[0]["id"] == 0
            assert child.returncode == 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_session_socket_replaces_a_stale_socket_but_never_a_file():
    directory = tempfile.mkdtemp(prefix="gm", dir="/tmp")
    try:
        path = os.path.join(directory, "s.sock")
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(path)
        stale.close()
        server = session_server(MemoryService(None), path)
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        server.server_close()
        assert not os.path.exists(path)
        with open(path, "w") as handle:
            handle.write("keep")
        with pytest.raises(ValueError, match="not a socket"):
            session_server(MemoryService(None), path)
        with open(path) as handle:
            assert handle.read() == "keep"
    finally:
        shutil.rmtree(directory, ignore_errors=True)
