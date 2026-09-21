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
    first = breaker.admit()
    assert breaker.failure(first, "network") is None and breaker.failure(first, "network") is None
    assert breaker.admit() == first  # transient failures below the threshold keep work flowing
    assert breaker.failure(first, "network") == 60
    # Calls already in flight when it opened neither extend the wait nor close it.
    assert breaker.failure(first, "network") is None and breaker.success(first) is False
    assert breaker.admit() is None and breaker.state()["retry_in"] == 60
    now[0] = 61
    probe = breaker.admit()
    assert probe is not None and breaker.admit() is None  # exactly one probe
    assert breaker.failure(probe, "network") == 120  # only a failed probe doubles the wait
    now[0] = 61 + 121
    assert breaker.failure(breaker.admit(), "network") == 200  # capped
    now[0] += 201
    assert breaker.success(breaker.admit()) is True
    assert breaker.admit() is not None
    assert breaker.state() == {"open": False, "reason": None, "retry_in": 0}
    fresh = Breaker()
    assert fresh.failure(fresh.admit(), "usage_limit", persistent=True) == 60


def test_a_probe_that_makes_no_model_call_frees_the_probe(graph, tmp_path):
    store, ns = graph
    clock = [0.0]
    service = MemoryService(store, Flaky())
    service.breaker = Breaker(clock=lambda: clock[0])
    service.breaker.failure(service.breaker.admit(), "usage_limit", persistent=True)
    clock[0] = 61
    # Nothing is due: the probe finds no episode, and must not stay taken forever.
    assert worker_tick(service, ns)["receipts"] == []
    clock[0] += 6
    (tmp_path / "note.md").write_text("Note: Atlas uses MySQL.")
    scan_bank(service, ns, [tmp_path], {})
    service.llm.down = False
    assert worker_tick(service, ns)["receipts"][0]["status"] == "complete"
    assert not service.breaker.open


def test_an_episode_that_needs_no_model_cannot_close_the_break(graph, tmp_path):
    store, ns = graph
    from graph_memory.session_sources import feed_records

    from .test_session_sources import claude

    call = {"type": "tool_use", "id": "r1", "name": "Bash", "input": {"command": "ls"}}
    path = tmp_path / "s.jsonl"
    path.write_text(claude("assistant", [call]))
    clock = [0.0]
    service = MemoryService(store, Flaky())
    service.breaker = Breaker(clock=lambda: clock[0])
    feed_records(service, ns, path, "s")
    service.breaker.failure(service.breaker.admit(), "usage_limit", persistent=True)
    clock[0] = 61
    receipt = worker_tick(service, ns)["receipts"][0]
    assert receipt["skipped"] and "breaker" not in receipt
    assert service.breaker.open and service.llm.calls == 0


def test_a_failure_that_follows_one_episode_is_set_aside(graph, tmp_path):
    store, ns = graph
    (tmp_path / "note.md").write_text("Note: Atlas uses MySQL.")
    service = MemoryService(store, Flaky("timeout"))
    service.breaker = Breaker(threshold=99)  # the provider serves everyone else
    scan_bank(service, ns, [tmp_path], {})
    for _ in range(3):
        receipt = worker_tick(service, ns)["receipts"][0]
        store.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) SET e.retry_after=0", ns=ns
            ).consume()
        )
    assert receipt["quarantined"] and receipt["failed_attempts"] == 0
    assert worker_tick(service, ns)["receipts"] == []


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


def test_a_lone_crashing_episode_cannot_hold_the_break_open(graph, tmp_path):
    store, ns = graph
    (tmp_path / "note.md").write_text("Note: Atlas uses MySQL.")
    clock = [0.0]
    service = MemoryService(store, Flaky("timeout"))
    service.breaker = Breaker(clock=lambda: clock[0])
    scan_bank(service, ns, [tmp_path], {})
    quarantined = False
    for _ in range(8):  # two failures, the break opens, then every probe is this episode
        receipts = worker_tick(service, ns)["receipts"]
        quarantined = quarantined or any(r.get("quarantined") for r in receipts)
        clock[0] += 1000
        store.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) SET e.retry_after=0", ns=ns
            ).consume()
        )
    assert quarantined
    # With the episode set aside, healthy work closes the break.
    (tmp_path / "other.md").write_text("Another note: Atlas uses Postgres.")
    service.llm.down = False
    service.breaker = Breaker()
    scan_bank(service, ns, [tmp_path], {})
    assert [r["status"] for r in worker_tick(service, ns)["receipts"]] == ["complete"]
