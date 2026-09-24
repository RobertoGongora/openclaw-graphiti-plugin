import os
import subprocess
import sys
import tempfile

import pytest

from graph_memory.admission import HistoryBusy, historical_read
from graph_memory.mcp import BUSY, Protocol
from graph_memory.models import HistoricalScope
from graph_memory.service import MemoryService

from .test_contracts import rpc


@pytest.mark.parametrize("killed", [False, True])
def test_global_history_admission_releases_after_process_exit(tmp_path, monkeypatch, killed):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    code = "from graph_memory.admission import historical_read\nwith historical_read():\n print('ready',flush=True)\n input()\n"
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(HistoryBusy), historical_read():
            pass
        if killed:
            child.kill()
        else:
            child.stdin.write("\n")
            child.stdin.flush()
        child.wait(timeout=5)
        with historical_read():
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_protocol_busy_does_not_run_handler_and_live_calls_work(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    server = Protocol(MemoryService(None), "test")
    called = []
    server.tools["memory_recall"] = (HistoricalScope, lambda r: called.append(r) or {}, "")
    past = rpc("tools/call", name="memory_recall", arguments={"at_change": 0})
    live = rpc("tools/call", name="memory_recall", arguments={})
    with historical_read():
        status, response = server.dispatch(past)
        assert status == 503 and response["error"]["code"] == BUSY
        assert not called
        assert server.dispatch(live)[0] == 200
    assert server.dispatch(past)[0] == 200
    assert len(called) == 2


def test_admission_released_after_handler_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    server = Protocol(MemoryService(None), "test")

    def fail(_):
        raise ValueError("test")

    server.tools["memory_recall"] = (HistoricalScope, fail, "")
    reply = server.dispatch(rpc("tools/call", name="memory_recall", arguments={"at_change": 0}))
    assert reply[1]["result"]["isError"]
    with historical_read():
        pass
