"""Source-grounded extraction instructions, evaluated against recorded failures."""

from copy import deepcopy


def extraction_payload(payload):
    """Keep full durable evidence; omit ineligible opaque text only from model input."""
    reduced = deepcopy(payload)
    transcript = reduced["transcript"]
    if transcript.get("source_format") not in {"session-records-v1", "direct-mcp-v1"}:
        return reduced
    from .recall_provenance import read_results

    reads = read_results(transcript["messages"])
    for message in transcript["messages"]:
        if message.get("source_type") == "context":
            message["content"] = (
                "[Context text omitted: not eligible claim or validation evidence.]"
            )
        elif message.get("id") in reads and message.get("source_type") == "tool_result":
            # Memory and delegation results never corroborate a claim. Label them
            # for the model the way the validator judges them, not on a retry.
            message["source_type"] = "memory_read"
    if transcript.get("memory_origins"):
        # The model needs which reports repeat memory and which reads they follow.
        # The recalled fact IDs are provenance for the stored payload, not input.
        present = {message.get("id") for message in transcript["messages"]}
        transcript["memory_origins"] = {
            mid: {"result_ids": [rid for rid in origin.get("result_ids", []) if rid in present]}
            for mid, origin in transcript["memory_origins"].items()
        }
    return reduced


CANDIDATE_SOURCE = """Evidence policy for session-records-v1 AND direct-mcp-v1.
Apply this decision procedure to EACH fact before choosing a date or status.

1. Choose a durable conversational claim. evidence contains only exact quotes from
messages whose source_type is user_assertion or assistant_report. A message's
source_type is authoritative; do not reclassify it based on its content or role.
Quoted memories, examples, pasted assistant statements and citation blocks within
a user message are context unless the user explicitly adopts or corrects them.
When focus_message_ids is present, each fact must cite a focus message in evidence
or validation_evidence. Older messages may explain a new claim, not create new facts.

2. Classify support before choosing dates:
 - A user's own assertion can support that assertion. It does not prove an action
   requested by the user happened. Distinguish requests, plans and observations.
 - An assistant-only claim can be confirmed ONLY when validation_evidence quotes
   a message with source_type exactly tool_result and tool_failed not true, whose
   content corroborates that SAME claim. Cite the assistant in evidence as well.
 - ALL other assistant-only claims MUST have BOTH status="uncertain" AND
   valid_at=null, even with timestamps, confident wording, code, test output,
   reported deployment, or an apparent successful command nearby. Retain these
   useful claims with attribution; don't drop them just to avoid uncertainty.
 - context, memory_read, memory_write and tool_call NEVER confirm a claim.
   In particular a message labeled context stays context even if it contains
   convincing command output. Do not cite those types as validation_evidence.
 - Tool results may only corroborate conversational claims, never originate facts.
   With no durable conversational claim, return empty entities and facts.
 - transcript.memory_origins identifies assistant reports made from recalled
   memory or delegated summaries. Repeating, paraphrasing or analyzing an existing
   memory is not a new independent claim, even in a different agent/session.
   Omit such facts unless a fresh non-memory tool result corroborates the specific
   new finding. Preserve new user corrections and independently observed changes.
   A separate support checker will verify the entire claim against the cited fresh
   evidence, including subject, status, date and qualifiers. An unrelated successful
   command is never support. Omit unsupported memory-derived claims, even uncertain
   ones, rather than attaching a nearby tool result to make them pass.
   Original read-message and fact IDs remain in source provenance; do not create
   another fact merely to remember that the memory was recalled.
 - In direct-mcp-v1, every claim and validation quote must have a verified entry
   in transcript.verified_source_refs. Unsourced text is context only; do not
   turn it into a fact, even uncertain. Original session ingestion preserves new
   user assertions without depending on a caller's claimed role.

3. Only AFTER step 2, apply the general rules for planned/active/ended and dates
to supported claims. The uncertain/null requirement above takes precedence over
ALL general timestamp rules, including timestamped present-tense state and plans.
Do not turn a supported user assertion or genuinely corroborated assistant claim
into uncertain merely to pass validation. A report date alone does not date an event.

4. Copy each evidence quote verbatim from ONE message's content. Use a short,
contiguous passage that supports the specific claim. Preserve accents, punctuation,
case, Markdown and whitespace. Do not translate, summarize, fix a typo, add ellipses,
join separate spans or copy from the rejected candidate. JSON escaping is fine;
the decoded quote must literally occur in the message with the cited ID.

Examples (illustrative only, never extract these examples):
 assistant_report says "Atlas uses MySQL." at a timestamp, with context showing
 a successful database command -> retain claim as uncertain, valid_at null.
 assistant_report says "Atlas uses MySQL." and tool_result confirms MySQL ->
 cite both in their separate fields; active with supported observation time.
 user_assertion says "Migrate next month; MySQL is still live." -> preserve
 the migration plan AND currently used MySQL, not an already-completed migration.

Before returning, check every fact, not just the first: correct source_type in
each evidence field, exact substring for every quote, uncertain AND null when
required, correct relation endpoint kinds, and focus coverage. implemented_in is
ONLY framework -> language; a project using Python is uses_language, not implemented_in.
Keep people distinct from their assistants. A successful tool result proves only
the observed operation; an accepted job is not completed deployment. Respect gaps,
failed tools, partial artifacts and historical dates. Do not read current files
to reconstruct historical evidence. Preserve every explicit durable claim.
"""


def source_instructions(base):
    # Put the source decision before generic date guidance, and repeat precedence
    # at the end so those general rules cannot implicitly renew an assistant claim.
    return (
        """Output a SELF-CONTAINED extraction. Every fact.subject and fact.target must
exactly equal a key in YOUR returned entities array, including keys reused from
existing_entities. Existing context is a lookup aid, NOT an implicit declaration.
Include those reused entities with their existing key/name/kind. Check all endpoints
before returning; never leave entities empty when facts is nonempty.

Extract at claim level, not as a broad session summary. For a multi-part report,
cover each distinct durable item: implementation or configuration changes, current
and planned dependencies, operational status, relevant measurements and diagnosed
causes, decisions, corrections and unresolved work. Preserve concrete values and
qualifiers in the fact summary (such as version, count, elapsed time, configuration
value, local-only/not-deployed). Do not replace these with vague 'reported results'
or 'work happened'. Group tightly related details, but do not skip later items.
Retain important assistant claims as attributed uncertain facts when unsupported;
uncertainty changes their status and time, not whether their useful details survive.
There is NO five-fact or top-N budget. For a numbered or bulleted report, consider
EVERY item separately and retain every distinct durable assertion, including later
items. Measurements and their diagnosed bottleneck, test/evaluation outcomes,
operational restrictions, pending requirements and known limitations are durable
information too. Before returning, check the last items as carefully as the first;
do not stop once the major project changes are covered. Attribute an action or
decision to a named person only when the claim explicitly names that person as
the actor. A report addressed to a person does not mean they made its decisions.

"""
        + CANDIDATE_SOURCE
        + "\n"
        + base
        + (
            "\nFINAL CHECK: assistant-only facts without eligible corroborating tool_result "
            'quotes must retain status="uncertain" and valid_at=null. '
            "Never promote context to tool_result. Copy literal quotes, not paraphrases.\n"
        )
    )
