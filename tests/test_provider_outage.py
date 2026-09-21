"""A provider outage pauses the queue without charging episodes for it."""

import json
import stat
import subprocess
import time

import pytest

from graph_memory.daemon import run_daemon, scan_bank
from graph_memory.feeds import Breaker, worker_tick
from graph_memory.llm import CodexLLM, ModelUnavailable, invocation_reason
from graph_memory.models import Extraction
from graph_memory.service import MemoryService


class Flaky:
    timeout = 600

    def __init__(self, reason="usage_limit"):
        self.reason, self.down, self.calls = reason, True, 0

    def generate(self, instructions, payload, output):
        self.calls += 1
        if self.down:
            raise ModelUnavailable(
                "Codex model invocation failed (exit 1); check CLI authentication/model availability",
                self.reason,
            )
        return Extraction(entities=[], facts=[])


def episodes(store, ns):
    return store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) RETURN e.status AS status,"
            "coalesce(e.attempts,0) AS attempts,e.retry_after AS retry_after ORDER BY e.id",
            ns=ns,
        ).data()
    )


def test_breaker_opens_probes_backs_off_and_recovers():
    now = [0.0]
    breaker = Breaker(threshold=3, cooldown=60, ceiling=200, clock=lambda: now[0])
    assert breaker.failure("network") is None and breaker.failure("network") is None
    assert breaker.admit()  # transient failures below the threshold keep work flowing
    assert breaker.failure("network") == 60
    assert not breaker.admit() and breaker.state()["retry_in"] == 60
    now[0] = 61
    assert breaker.admit() and not breaker.admit()  # exactly one probe
    assert breaker.failure("network") == 120
    now[0] = 61 + 121
    assert breaker.admit() and breaker.failure("network") == 200  # capped
    now[0] += 201
    assert breaker.admit() and breaker.success() is True
    assert breaker.admit() and breaker.state() == {"open": False, "reason": None, "retry_in": 0}
    assert Breaker().failure("usage_limit", persistent=True) == 60  # no point in three tries


def test_outage_pauses_work_without_spending_retry_budget(graph, tmp_path):
    store, ns = graph
    for i in range(3):
        (tmp_path / f"{i}.md").write_text(f"Note {i} holds no durable information.")
    service = MemoryService(store, Flaky())
    service.breaker = Breaker(clock=lambda: clock[0])
    clock = [0.0]
    scan_bank(service, ns, [tmp_path], {})
    first = worker_tick(service, ns)
    # The first persistent failure opens the break; the other episodes are not tried.
    assert service.llm.calls == 1 and len(first["receipts"]) == 1
    receipt = first["receipts"][0]
    assert receipt["diagnostic"]["provider_reason"] == "usage_limit"
    assert receipt["breaker"]["open"] and receipt["failed_attempts"] == 0
    assert worker_tick(service, ns)["receipts"] == [] and service.llm.calls == 1
    rows = episodes(store, ns)
    assert {r["status"] for r in rows} == {"pending"} and {r["attempts"] for r in rows} == {0}
    # Recovery: the probe succeeds and the queue drains normally.
    service.llm.down = False
    clock[0] = 61
    store.transaction(
        lambda tx: tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns}) SET e.retry_after=0", ns=ns
        ).consume()
    )
    while store.pending(ns):
        worker_tick(service, ns)
    assert not service.breaker.open and service.llm.calls == 4


def test_daemon_reports_the_outage_once_and_skips_staging(graph, tmp_path, capsys):
    store, ns = graph
    (tmp_path / "note.md").write_text("No durable information.")
    service = MemoryService(store, Flaky("authentication"))
    run_daemon(service, ns, [tmp_path], workers=2, once=True)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    outages = [e for e in events if e["event"] == "provider_unavailable"]
    assert len(outages) == 1 and outages[0]["reason"] == "authentication"
    assert service.llm.calls == 1
    assert store.pending(ns)[0]["status"] == "pending"


def test_rejected_episode_still_spends_budget_with_growing_backoff(graph, tmp_path):
    store, ns = graph
    (tmp_path / "note.md").write_text("No durable information.")

    class Wrong:
        timeout = 600

        def generate(self, instructions, payload, output):
            raise ValueError("Evidence must quote an exact substring of its source message")

    service = MemoryService(store, Wrong())
    service.breaker = Breaker()
    scan_bank(service, ns, [tmp_path], {})
    waits = []
    for _ in range(3):
        before = time.time()
        receipt = worker_tick(service, ns)["receipts"][0]
        waits.append(round((receipt["retry_after"] - before) / 60))
        store.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) SET e.retry_after=0", ns=ns
            ).consume()
        )
    assert waits == [1, 2, 4] and receipt["failed_attempts"] == 3
    assert not service.breaker.open  # the provider answered every time


def fake_codex(tmp_path, body):
    script = tmp_path / "bin" / "codex"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script.parent)


def test_codex_failures_are_classified_without_leaking_text(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PATH",
        fake_codex(
            tmp_path,
            'echo "user: secret transcript text" >&2\n'
            'echo "ERROR: You\'ve hit your usage limit. Try again later." >&2\nexit 1\n',
        )
        + ":/usr/bin:/bin",
    )
    with pytest.raises(ModelUnavailable) as caught:
        CodexLLM().generate("Extract", {"transcript": "source"}, Extraction)
    assert caught.value.memory_reason == "usage_limit"
    assert "secret" not in str(caught.value)
    assert invocation_reason("ERROR: stream disconnected before completion") == "network"
    assert invocation_reason("something nobody anticipated") == "unknown"


def test_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    monkeypatch.setenv(
        "PATH",
        fake_codex(tmp_path, f"sleep 60 &\necho $! > {marker}\nwait\n") + ":/usr/bin:/bin",
    )
    with pytest.raises(ModelUnavailable) as caught:
        CodexLLM(timeout=1).generate("Extract", {"transcript": "source"}, Extraction)
    assert caught.value.memory_reason == "timeout"
    child = int(marker.read_text())
    time.sleep(0.2)
    alive = subprocess.run(["ps", "-p", str(child)], capture_output=True).returncode == 0
    assert not alive  # the grandchild the CLI launched is gone too
