"""MEMORY_WRITER_PROMPT: the selected session-records-v1 writer prompt reaches the
Codex CLI byte for byte, only when switched on, only with the model it was tested
with; everything else keeps the app's own instructions."""

import hashlib
import json
from pathlib import Path

import pytest

import graph_memory.llm as llm
from graph_memory.feeds import worker_tick
from graph_memory.llm import CodexLLM, configured_llm, extraction_instructions
from graph_memory.models import Extraction, Ingest, Transcript
from graph_memory.service import MemoryService
from tests.helpers import MYSQL, PROJECT
from tests.test_quarantine_rollout import Model, quarantine, upgraded  # noqa: F401
from tests.test_retry_budget import stage

SELECTED = "f154a2e3910b880e8ad0f0921af6efb97e5894ab9f10e70342e1d6881a023d9c"
# The app's session-records-v1 instructions before this change (5899353), unchanged.
APP = "8b2f9f5bc02606a84850eeef22aab6ae8ddff1f3fe0d908f9a161bbee14269dc"
GUIDANCE = "c68ffb9cfaf14fe160f090a7647d366edc823ce05ead42d8a111e2435e7f689b"
SEPARATOR = "\nINPUT DATA:\n"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def transcript(fmt="session-records-v1", namespace="x"):
    return Transcript(
        namespace=namespace,
        source_id="writer-prompt",
        session_id="writer-prompt",
        source_format=fmt,
        messages=[
            {
                "id": "u1",
                "role": "user",
                "source_type": "user_assertion" if fmt else "legacy",
                "timestamp": "2026-11-02T09:00:00Z",
                "content": "Atlas usa MySQL en producción.",
            }
        ],
    )


def test_the_selected_prompt_is_the_apps_then_the_guidance_verbatim():
    app = extraction_instructions(transcript())
    selected = extraction_instructions(transcript(), "selected-v1")
    guidance = Path(llm.__file__).parent / llm.WRITER_PROMPTS["selected-v1"]["file"]
    assert hashlib.sha256(guidance.read_bytes()).hexdigest() == GUIDANCE
    assert sha(app) == APP and sha(selected) == SELECTED
    assert selected == app + "\n\n" + guidance.read_bytes().decode()


@pytest.mark.parametrize("fmt", ["direct-mcp-v1", None, "openclaw-v1"])
def test_other_sources_keep_their_instructions_whatever_the_writer(fmt):
    assert extraction_instructions(transcript(fmt), "selected-v1") == extraction_instructions(
        transcript(fmt)
    )


def test_a_changed_prompt_text_is_refused(tmp_path, monkeypatch):
    changed = tmp_path / "guidance.txt"
    changed.write_bytes(
        (Path(llm.__file__).parent / "prompts/session-records-writer-v1.txt").read_bytes() + b" "
    )
    selected = {**llm.WRITER_PROMPTS["selected-v1"], "file": str(changed)}
    monkeypatch.setitem(llm.WRITER_PROMPTS, "selected-v1", selected)
    with pytest.raises(ValueError, match="no longer the tested prompt"):
        extraction_instructions(transcript(), "selected-v1")


TESTED = {"MEMORY_LLM": "codex", "MEMORY_MODEL": "gpt-6.1-terra", "MEMORY_REASONING_EFFORT": "low"}


def configure(monkeypatch, **env):
    for key in (*TESTED, "MEMORY_WRITER_PROMPT", "MEMORY_LLM_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return configured_llm()


def test_unset_changes_nothing(monkeypatch):
    model = configure(monkeypatch, MEMORY_LLM="codex")
    assert (model.model, model.effort, model.writer) == ("gpt-5.6-terra", "low", None)


def test_switched_on_only_with_the_tested_model(monkeypatch):
    model = configure(monkeypatch, MEMORY_WRITER_PROMPT="selected-v1", **TESTED)
    assert (model.model, model.effort, model.writer) == ("gpt-6.1-terra", "low", "selected-v1")


@pytest.mark.parametrize(
    "env",
    [
        {"MEMORY_LLM": "codex"},  # the default model, gpt-5.6-terra
        {**TESTED, "MEMORY_REASONING_EFFORT": "medium"},
        {**TESTED, "MEMORY_LLM": "compatible", "MEMORY_LLM_URL": "http://127.0.0.1:1"},
        {**TESTED, "MEMORY_LLM": "caller"},
        {**TESTED, "MEMORY_WRITER_PROMPT": "neon"},
    ],
)
def test_any_other_combination_stops_at_startup(monkeypatch, env):
    with pytest.raises(ValueError, match="MEMORY_WRITER_PROMPT"):
        configure(monkeypatch, **{"MEMORY_WRITER_PROMPT": "selected-v1", **env})


# Through the app, to the CLI's stdin ------------------------------------------------


def extraction():
    return Extraction(
        entities=[PROJECT, MYSQL],
        facts=[
            {
                "subject": PROJECT["key"],
                "target": MYSQL["key"],
                "relation": "uses_database",
                "summary": "Atlas uses MySQL in production.",
                "valid_at": "2026-11-02T09:00:00Z",
                "evidence": [{"message_id": "u1", "quote": "Atlas usa MySQL en producción."}],
            }
        ],
    )


def cli(monkeypatch, header=("gpt-6.1-terra", "low")):
    """The Codex subprocess replaced; everything before it is the real adapter."""
    stdin = []

    def run_group(command, prompt, cwd, timeout):
        stdin.append(prompt)
        out = Path(command[command.index("--output-last-message") + 1])
        out.write_text(extraction().model_dump_json())
        return 0, f"model: {header[0]}\nreasoning effort: {header[1]}\n"

    monkeypatch.setattr(llm, "run_group", run_group)
    return stdin


@pytest.mark.parametrize("writer,expected", [("selected-v1", SELECTED), (None, APP)])
def test_the_cli_receives_exactly_the_selected_instructions_and_the_source(
    graph, monkeypatch, writer, expected
):
    store, ns = graph
    stdin = cli(monkeypatch)
    service = MemoryService(store, CodexLLM("gpt-6.1-terra", "low", writer=writer))
    source = transcript(namespace=ns)
    receipt = service.ingest(Ingest(transcript=source, extract=True))
    assert receipt["status"] == "complete" and len(stdin) == 1
    segment, _, data = stdin[0].rpartition(SEPARATOR)
    assert sha(segment) == expected
    sent = json.loads(data)["transcript"]["messages"]
    assert [(m["id"], m["role"], m["content"]) for m in sent] == [
        (m.id, m.role, m.content) for m in source.messages
    ]
    info = json.loads(store.episode(ns, receipt["episode_id"])["model_info"])
    assert info.get("writer") == writer
    assert ("writer" in info) == (writer is not None)


def test_a_quarantine_from_the_old_engine_stays_held_under_the_selected_writer(
    upgraded,  # noqa: F811
    monkeypatch,
):
    """The new engine with the writer switched on does not send an episode held
    under the previous engine back to the model; only retry-quarantined does."""
    old, new, ns = upgraded
    held = stage(old, ns)
    quarantine(MemoryService(old, Model("invented quote")), ns, held)
    before = old.episode(ns, held)
    old.transaction(
        lambda tx: tx.run("MATCH (e:MemoryEpisode {id:$id}) SET e.retry_after=0", id=held).consume()
    )
    stdin = cli(monkeypatch)
    service = MemoryService(new, CodexLLM("gpt-6.1-terra", "low", writer="selected-v1"))
    assert worker_tick(service, ns)["receipts"] == []
    assert stdin == []
    after = new.episode(ns, held)
    assert after["quarantine_engine"] == before["quarantine_engine"] != new.engine
    assert after["status"] != "complete"
