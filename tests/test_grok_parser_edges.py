"""Native malformed-line containment and redaction/re-export regressions."""

import json
import time

import pytest

from graph_memory.importers import redact, redact_v1
from graph_memory.session_sources import records


def transcript(tmp_path, rows):
    path = tmp_path / "desk.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def native(**fields):
    return {"kind": "message", "role": "user", "content": "Atlas uses MySQL.", **fields}


@pytest.mark.parametrize("secret", ["]]]]]]", "}}}}}}", "))))))", "\\" * 6, "}])\\"])
def test_punctuation_only_assignments_keep_v1_coverage(secret):
    text = f"password={secret}"
    assert redact_v1(text) == "password=[REDACTED]"
    assert redact(text) == "password=[REDACTED]"
    assert redact(redact(text)) == redact(text)


@pytest.mark.parametrize(
    "secret", ['alpha " bravo charlie', 'alpha \\" bravo charlie', "alpha\\", "alpha, ; bravo"]
)
@pytest.mark.parametrize("depth", [1, 2])
def test_json_quoted_values_are_escape_aware(secret, depth):
    text = json.dumps({"password": secret, "note": "Atlas uses MySQL."})
    if depth == 2:
        text = json.dumps(text)
    cleaned = redact(text)
    assert "alpha" not in cleaned and "bravo" not in cleaned and "charlie" not in cleaned
    assert "Atlas uses MySQL." in cleaned
    assert redact(cleaned) == cleaned
    value = json.loads(cleaned)
    if depth == 2:
        value = json.loads(value)
    assert value["password"] == "[REDACTED]"


@pytest.mark.parametrize(
    "text", [r"password='alpha \' bravo charlie'", r'password="alpha \" bravo charlie"']
)
def test_escaped_assignment_delimiters_do_not_leak_the_tail(text):
    cleaned = redact(text)
    assert "alpha" not in cleaned and "bravo" not in cleaned and "charlie" not in cleaned
    assert redact(cleaned) == cleaned


def test_long_escaped_values_and_unterminated_quotes_are_bounded():
    for text in (
        json.dumps({"password": '\\"' * 50_000 + " tail"}),
        json.dumps(json.dumps({"password": '\\"' * 50_000 + " tail"})),
        'password="' + "\\" * 100_000 + " secret tail",
    ):
        start = time.monotonic()
        cleaned = redact(text)
        assert time.monotonic() - start < 2
        assert "tail" not in cleaned
        assert redact(cleaned) == cleaned


@pytest.mark.parametrize(
    "bad",
    [
        native(kind=[]),
        native(kind={}),
        native(kind=7),
        native(role=[]),
        native(role={}),
        native(role="unexpected"),
        native(content=[{"type": "text", "text": 7}, {"type": "text", "text": "usable"}]),
        native(content=[{"type": [], "text": "usable"}]),
        native(content={"text": "usable"}),
        {"kind": "send-message", "message": "usable"},
        {"kind": "send-message", "message": ["usable"]},
        {"kind": "send-message", "message": {"type": [], "widget": {"prompt": "usable"}}},
        {"kind": "send-message", "message": {"type": [], "content": "usable"}},
        {
            "kind": "send-message",
            "message": {
                "type": "text",
                "content": [{"type": "text", "text": 7}, {"type": "text", "text": "usable"}],
            },
        },
    ],
)
def test_malformed_native_lines_preserve_context_and_continue(tmp_path, bad):
    path = transcript(tmp_path, [bad, native(content="The next valid claim.")])
    malformed, good = records(path)
    assert malformed.source_type == "context"
    assert malformed.role == "note"
    assert "malformed_native_entry" in malformed.gaps
    assert "usable" in malformed.content or "Atlas uses MySQL." in malformed.content
    assert good.content == "The next valid claim." and good.source_type == "user_assertion"
    assert good.id == "line-2-block-0-0"


def test_malformed_context_is_scrubbed_and_bounded_through_message_construction(tmp_path):
    bad = native(
        content=[
            {"type": "text", "text": 7},
            {"type": "text", "text": "x" * 48_000 + " password=supersecret"},
        ],
        timestamp=[],
        channel="desk " * 300,
        fromAgent={"id": "agent " * 300},
    )
    messages = list(records(transcript(tmp_path, [bad, native()])))
    assert messages[-1].source_type == "user_assertion"
    assert len(messages) >= 3
    for message in messages[:-1]:
        assert message.source_type == "context"
        assert "malformed_native_entry" in message.gaps
        assert len(message.content) <= 24_000
        assert message.timestamp is None
        assert "supersecret" not in message.content
    assert "[REDACTED]" in "".join(m.content for m in messages)


@pytest.mark.parametrize(
    "change",
    [
        {"role": "assistant"},
        {"fromAgent": {"id": "agent-2"}},
        {"author": {"kind": "agent", "id": "agent-2"}},
        {"channel": "voice:call-2"},
    ],
)
def test_native_attribution_changes_are_revisions_and_keep_the_prefix(tmp_path, change):
    original = native(id="same")
    path = transcript(tmp_path, [original])
    first = list(records(path))
    with path.open("a") as out:
        for row in (original, {**original, **change}, {**original, **change}):
            out.write(json.dumps(row) + "\n")
    messages = list(records(path))
    assert messages[:1] == first
    assert len(messages) == 2
    revised = messages[1]
    assert revised.source_type == "context" and revised.role == "note"
    assert revised.content == original["content"]
    assert "entry_revised_after_read" in revised.gaps
    assert revised.id == "line-3-block-0-0"


def test_native_widget_late_response_remains_new_user_evidence(tmp_path):
    widget = {
        "kind": "send-message",
        "id": "widget",
        "message": {"type": "widget", "widget": {"prompt": "Which database?"}},
    }
    path = transcript(tmp_path, [widget])
    before = list(records(path))
    with path.open("a") as out:
        out.write(json.dumps({**widget, "respondedValue": "Postgres"}) + "\n")
    after = list(records(path))
    assert after[:1] == before
    assert len(after) == 2
    assert (after[1].source_type, after[1].content) == ("user_assertion", "Postgres")


def test_widget_answer_attribution_change_is_a_revision(tmp_path):
    widget = {
        "kind": "send-message",
        "id": "widget",
        "message": {"type": "widget", "widget": {"prompt": "Which database?"}},
        "respondedValue": "Postgres",
    }
    messages = list(records(transcript(tmp_path, [widget, {**widget, "widgetSkipped": True}])))
    assert len(messages) == 3
    assert messages[-1].source_type == "context"
    assert "entry_revised_after_read" in messages[-1].gaps


def test_bad_native_content_does_not_poison_reexport_deduplication(tmp_path):
    bad = native(id="same", content=[{"type": "text", "text": 7}])
    good = native(id="same")
    messages = list(records(transcript(tmp_path, [bad, good, good])))
    assert len(messages) == 2
    assert "malformed_native_entry" in messages[0].gaps
    assert messages[1].source_type == "user_assertion"
