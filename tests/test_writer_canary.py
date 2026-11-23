"""The one-file canary in docs/writer-prompt-rollout.md, offline: a one-off process
stages one session from a directory holding only that file, under the key the shared
daemon gives it, and extracts only the episode IDs it staged with the selected writer.
The daemon, back on the app's prompt, then finds the file already fed."""

import json
import shutil
from pathlib import Path

import graph_memory.llm as llm
from graph_memory.feeds import worker_tick
from graph_memory.follow import follow_once
from graph_memory.llm import CodexLLM
from graph_memory.models import EpisodeRequest
from graph_memory.service import MemoryService
from tests.test_feed_identity import drain, feeds
from tests.test_session_sources import claude
from tests.test_writer_prompt import APP, SELECTED, SEPARATOR, sha

SESSION = "-Users-rob/2b7e-canary.jsonl"


def cli(monkeypatch, model):
    """The Codex subprocess replaced by one that records its stdin and finds nothing."""
    stdin = []

    def run_group(command, prompt, cwd, timeout):
        stdin.append(prompt)
        out = Path(command[command.index("--output-last-message") + 1])
        out.write_text('{"entities": [], "facts": []}')
        return 0, f"model: {model}\nreasoning effort: low\n"

    monkeypatch.setattr(llm, "run_group", run_group)
    return stdin


def episodes(store, ns):
    return store.read(
        lambda tx: {
            r["id"]: (r["status"], r["source"], json.loads(r["info"] or "{}").get("writer"))
            for r in tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) "
                "RETURN e.id AS id,e.status AS status,e.source_id AS source,e.model_info AS info",
                ns=ns,
            )
        }
    )


def test_a_one_off_extracts_only_its_staged_file_and_the_daemon_finds_it_fed(
    graph, tmp_path, monkeypatch
):
    store, ns = graph
    production = tmp_path / "host" / "claude"
    backlog = production / "-Users-rob" / "older.jsonl"
    backlog.parent.mkdir(parents=True)
    backlog.write_text(claude("user", "Atlas runs on MySQL.") + claude("assistant", "Noted."))
    drain(MemoryService(store), ns, [production])
    waiting = episodes(store, ns)
    assert waiting and all(status == "pending" for status, _, _ in waiting.values())

    # The canary's session, alone under the same label and relative path.
    stage = tmp_path / "stage" / "claude"
    (stage / SESSION).parent.mkdir(parents=True)
    (stage / SESSION).write_text(
        claude("user", "Atlas moved to PostgreSQL today.") + claude("assistant", "Recorded.")
    )
    stdin = cli(monkeypatch, "gpt-6.1-terra")
    one_off = MemoryService(store, CodexLLM("gpt-6.1-terra", "low", writer="selected-v1"))
    fed = follow_once(one_off, ns, [stage], {}, source_records=True)
    assert len(fed) == 1 and fed[0]["caught_up"]
    assert feeds(store, ns)[fed[0]["feed_id"]]["source_key"] == "claude:" + SESSION
    allowed = sorted(r["episode_id"] for r in fed[0]["receipts"])
    assert allowed and set(allowed).isdisjoint(waiting)
    for episode_id in allowed:
        one_off.extract(EpisodeRequest(namespace=ns, episode_id=episode_id))
    assert len(stdin) == len(allowed)
    assert all(sha(prompt.rpartition(SEPARATOR)[0]) == SELECTED for prompt in stdin)
    after = episodes(store, ns)
    assert {i for i, (_, _, w) in after.items() if w == "selected-v1"} == set(allowed)
    assert {i: after[i] for i in waiting} == waiting

    # The same bytes reach the daemon's root; the daemon resumes on the app's prompt.
    shutil.copy(stage / SESSION, production / SESSION)
    shutil.rmtree(tmp_path / "stage")
    before = feeds(store, ns)
    daemon = MemoryService(store, CodexLLM("gpt-5.6-terra", "low"))
    drain(daemon, ns, [production])
    assert feeds(store, ns).keys() == before.keys() and episodes(store, ns) == after
    stdin = cli(monkeypatch, "gpt-5.6-terra")
    worker_tick(daemon, ns, limit=10)
    assert stdin and all(sha(p.rpartition(SEPARATOR)[0]) == APP for p in stdin)
    final = episodes(store, ns)
    assert {i: final[i] for i in allowed} == {i: after[i] for i in allowed}
    assert all(final[i][2] is None for i in waiting)
