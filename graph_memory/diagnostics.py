"""Bounded error metadata for logs; never emit source text or raw exceptions."""

import re

from pydantic import ValidationError

# Exact engine-owned messages only. Pydantic's msg/input/ctx can contain private
# model output, so arbitrary exception text must never reach the error stream.
REASONS = {
    "Facts must cite a conversational claim in evidence; tool outputs belong only in validation_evidence, and memory artifacts are context only": "invalid_claim_source",
    "An unvalidated assistant claim requires status=uncertain and valid_at=null. To validate it, cite an exact corroborating tool-result quote in validation_evidence AND keep the assistant quote in evidence. Memory reads/writes cannot validate it.": "unvalidated_assistant_claim",
    "Evidence must quote an exact substring of its source message": "evidence_quote_mismatch",
    "A feed fact must cite at least one new focus message": "evidence_focus_missing",
    "Future facts must be planned, not active": "future_fact_active",
    "Entity keys must be unique": "duplicate_entity_keys",
    "Every relationship endpoint must be declared": "undeclared_endpoint",
    "implemented_in links a framework to a language": "invalid_framework_source",
    "Events require an explicit occurrence time; undated event claims must use status=uncertain and valid_at=null. Never use ingestion time": "undated_event_active",
    "Engine files changed during this process; restart before committing": "engine_changed",
    "Episode not found in namespace": "episode_not_found",
    "Episode already committed with different extraction; retract incorrect facts explicitly": "extraction_conflict",
    "Graph differs from its journal; investigate an untracked write before continuing": "journal_state_mismatch",
    "Codex extraction timed out; durable input can be retried": "model_timeout",
}
# Faults of the namespace or the process, not of the episode that met them: the
# next episode would fail the same way, so none of them is charged or quarantined.
SYSTEMIC = {"journal_state_mismatch", "engine_changed"}
FIELDS = {
    "entities",
    "facts",
    "key",
    "name",
    "kind",
    "aliases",
    "subject",
    "relation",
    "target",
    "status",
    "summary",
    "slot",
    "valid_at",
    "confidence",
    "evidence",
    "validation_evidence",
    "message_id",
    "quote",
    "namespace",
    "source_id",
    "session_id",
    "source_kind",
    "source_uri",
    "source_created_at",
    "source_updated_at",
    "focus_message_ids",
    "messages",
    "id",
    "role",
    "content",
    "timestamp",
    "insights",
    "observations",
    "entity_keys",
    "supporting_fact_ids",
}


def reason(message):
    message = message.removeprefix("Value error, ")
    if message in REASONS:
        return REASONS[message]
    if message.startswith("Invalid target kind for Relation."):
        return "invalid_relationship_target"
    if message.startswith("Invalid target kind for "):
        return "invalid_relationship_target"
    if message.startswith("Ambiguous identity for ") and message.endswith(
        "; merge or disambiguate explicitly"
    ):
        return "ambiguous_identity"
    return None


def diagnostic(exc, stage="worker"):
    result = {"stage": stage, "code": "unclassified_error"}
    if isinstance(exc, ValidationError):
        result["code"] = "schema_validation"
        result["issue_count"] = exc.error_count()
        result["issues"] = []
        for issue in exc.errors(include_input=False, include_context=False, include_url=False)[:10]:
            result["issues"].append(
                {
                    "type": issue["type"],
                    "location": [
                        p if isinstance(p, int) or p in FIELDS else "<field>"
                        for p in issue["loc"][:12]
                    ],
                    "code": reason(issue["msg"]) or "schema_constraint",
                }
            )
        return result
    message = str(exc)
    result["code"] = reason(message) or result["code"]
    location = getattr(exc, "memory_location", None)
    if isinstance(location, (tuple, list)):
        result["location"] = [
            p if isinstance(p, int) or (isinstance(p, str) and p in FIELDS) else "<field>"
            for p in location[:12]
        ]
    locations = getattr(exc, "memory_locations", None)
    if isinstance(locations, list) and len(locations) > 1:
        result["locations"] = [
            [
                p if isinstance(p, int) or (isinstance(p, str) and p in FIELDS) else "<field>"
                for p in loc[:12]
            ]
            for loc in locations[:10]
        ]
    invocation = re.fullmatch(
        r"Codex model invocation failed \(exit (-?\d+)\); check CLI authentication/model availability",
        message,
    )
    if invocation:
        result.update(code="model_invocation_failed", exit_code=int(invocation[1]))
    elif message.startswith(("Model endpoint failed (HTTP ", "Model endpoint unreachable;")):
        result["code"] = "model_invocation_failed"
    # A closed vocabulary from the model adapter, never provider text.
    provider = getattr(exc, "memory_reason", None)
    if isinstance(provider, str) and re.fullmatch(r"[a-z_]{1,32}", provider):
        result["provider_reason"] = provider
    return result
