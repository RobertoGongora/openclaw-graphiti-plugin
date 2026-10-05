import json

import pytest

from graph_memory.models import Transcript
from graph_memory.session_sources import records
from tests.test_session_sources import cursor, extraction


@pytest.mark.parametrize("stamp", [None, "2026-09-20T15:00:00", "invalid", 10**100])
def test_cursor_missing_or_invalid_time_keeps_the_claim_without_dating_it(tmp_path, stamp):
    path = tmp_path / "cursor.jsonl"
    path.write_text(cursor("user", "Atlas uses MySQL.", stamp))
    messages = list(records(path))
    assert len(messages) == 1 and messages[0].timestamp is None
    transcript = Transcript(
        namespace="test",
        source_id="cursor:s",
        session_id="s",
        source_format="session-records-v1",
        messages=messages,
    )
    extraction(messages[0].id, status="uncertain", valid_at=None).validate_evidence(transcript)
    for props in [{"status": "active"}, {"status": "active", "valid_at": "2020-01-01T00:00:00Z"}]:
        with pytest.raises(ValueError, match="without source timestamps"):
            extraction(messages[0].id, **props).validate_evidence(transcript)


def test_a_bad_timestamp_does_not_prevent_the_next_dated_cursor_claim(tmp_path):
    path = tmp_path / "cursor.jsonl"
    path.write_text(
        cursor("user", "Atlas uses MySQL.", "2026-09-20T15:00:00")
        + cursor("user", "Atlas uses MySQL.", "2026-09-20T15:00:00Z")
    )
    messages = list(records(path))
    assert messages[0].timestamp is None and messages[1].timestamp is not None
    transcript = Transcript(
        namespace="test",
        source_id="cursor:s",
        session_id="s",
        source_format="session-records-v1",
        messages=messages,
    )
    extraction(messages[1].id, valid_at="2026-09-20T15:00:00Z").validate_evidence(transcript)


def test_cursor_epoch_milliseconds_are_a_source_timestamp(tmp_path):
    path = tmp_path / "cursor.jsonl"
    path.write_text(
        json.dumps(
            {"role": "user", "content": "Atlas uses MySQL.", "timestampMs": 1_759_053_720_000}
        )
        + "\n"
    )
    assert next(records(path)).timestamp is not None
