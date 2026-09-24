"""Ranking regressions use independent subjects and preserve evidence status."""

from types import SimpleNamespace

from graph_memory import retrieval as v

from .test_retrieval import fact, raw, retrieve


def search(data, question, **kwargs):
    return v.search(
        SimpleNamespace(recall=lambda *a, **kw: data),
        v.QuestionSearch(namespace="test", question=question, **kwargs),
    )


def test_named_subject_beats_unrelated_validated_api_answer():
    data = raw(
        uncertain=[
            fact(
                "atlas",
                relation="about",
                slot=None,
                target="topic:dispatch",
                summary="The API dispatch endpoint can create a thread.",
                status="uncertain",
            )
        ],
        current=[
            fact(
                "boreal",
                subject="project:boreal",
                relation="about",
                slot=None,
                target="topic:dispatch",
                summary="The API dispatch endpoint can create a thread.",
                validation_message_refs=["source"],
            )
        ],
    )
    result = search(data, "Can Atlas create a thread through its API?")
    assert result["facts"][0]["id"] == "atlas"
    assert result["facts"][0]["lane"] == "uncertain"


def test_word_forms_recover_original_over_long_topic_retelling():
    primary = fact(
        "original",
        relation="about",
        slot=None,
        target="topic:delivery",
        summary="Atlas released an update that reduces uploads.",
        validation_message_refs=["original-tool-output"],
    )
    retelling = fact(
        "retelling",
        relation="about",
        slot=None,
        target="topic:delivery",
        summary="Atlas upload release was discussed alongside "
        + " ".join(f"unrelated{i}" for i in range(150)),
        status="uncertain",
    )
    data = raw(current=[primary], uncertain=[retelling])
    result = search(data, "Atlas upload release")
    assert result["facts"][0]["id"] == "original"
    # Page size must not change the ranking or repeat a fact.
    page = search(data, "Atlas upload release", limit=1)
    next_page = search(data, "Atlas upload release", limit=1, offset=page["next_offset"])
    assert [page["facts"][0]["id"], next_page["facts"][0]["id"]] == [
        f["id"] for f in result["facts"]
    ]


def test_schema_question_prefers_framework_state_to_validated_build_event():
    data = raw(
        current=[
            fact(
                "framework",
                relation="uses_framework",
                target="framework:laravel",
                target_kind="framework",
                summary="Atlas uses Laravel 13 as its application framework.",
            )
        ],
        events=[
            fact(
                "build",
                relation="about",
                target="framework:vite",
                target_kind="framework",
                summary="The Vite build completed.",
                validation_message_refs=["build-log"],
                valid_ts=2,
            )
        ],
    )
    assert (
        retrieve(data, question="What framework does Atlas use?")["facts"][0]["id"] == "framework"
    )


def test_substantial_exact_answer_beats_short_keyword_fragment():
    data = raw(
        uncertain=[
            fact(
                "answer",
                relation="about",
                slot=None,
                target="topic:release",
                status="uncertain",
                summary="Commit abc123 was deployed after the evaluation passed all 24 batches. "
                "The validation rate was 16%, up from 14%, while generic relations fell from 67% "
                "to 1%. The deployment completed successfully with healthy services, zero source "
                "mismatches, verified backups, and the original source evidence retained for audit.",
            ),
            fact(
                "fragment",
                relation="about",
                slot=None,
                target="topic:release",
                status="uncertain",
                summary="A deployment and evaluation are being discussed.",
            ),
        ]
    )
    assert (
        retrieve(data, question="What commit was deployed and what were the evaluation results?")[
            "facts"
        ][0]["id"]
        == "answer"
    )
