from evals.ab import score_session


def test_ab_counts_successful_grep_content_but_not_failed_reads_or_listing():
    case = {"expected": {"live_verified": False}, "source_files": ["work.md"]}
    answer = {"live_verified": False, "evidence_quotes": ["Migration paused."]}
    call = {
        "id": "t1",
        "name": "Grep",
        "input": {"path": "/memory/work.md", "output_mode": "content"},
    }
    events = [
        {
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "Migration paused."}
                ]
            }
        }
    ]
    checks = score_session("native", case, answer, [call], events, "Migration paused.", {})
    assert all(checks.values())
    events[0]["message"]["content"][0]["is_error"] = True
    assert not score_session("native", case, answer, [call], events, "Migration paused.", {})[
        "retrieved_memory"
    ]
    events[0]["message"]["content"][0]["is_error"] = False
    call["input"]["output_mode"] = "files_with_matches"
    assert not score_session("native", case, answer, [call], events, "Migration paused.", {})[
        "retrieved_memory"
    ]


def test_ab_rejects_ungrounded_quotes_even_when_factual_fields_match():
    case = {"expected": {"live_verified": False}, "source_files": ["work.md"]}
    answer = {"live_verified": False, "evidence_quotes": ["Migration completed."]}
    assert not score_session("native", case, answer, [], [], "Migration paused.", {})[
        "quoted_original_evidence"
    ]
