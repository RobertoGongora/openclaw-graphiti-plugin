"""The extraction contract is also the tool JSON Schema; nothing executes model output."""

from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
Key = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[\w./:@ -]+$")]


def now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Kind(StrEnum):
    project = "project"
    person = "person"
    organization = "organization"
    framework = "framework"
    language = "language"
    database = "database"
    service = "service"
    habit = "habit"
    event = "event"
    topic = "topic"
    decision = "decision"
    lesson = "lesson"
    issue = "issue"


class Relation(StrEnum):
    uses_framework = "uses_framework"
    uses_language = "uses_language"
    uses_database = "uses_database"
    uses_service = "uses_service"
    implemented_in = "implemented_in"
    part_of = "part_of"
    owned_by = "owned_by"
    has_habit = "has_habit"
    occurred = "occurred"
    resolved = "resolved"
    learned = "learned"
    decided = "decided"
    worked_on = "worked_on"
    related_to = "related_to"
    prefers = "prefers"


# Both ends are validated before opening a write transaction.
TARGETS = {
    Relation.uses_framework: {Kind.framework},
    Relation.uses_language: {Kind.language},
    Relation.uses_database: {Kind.database},
    Relation.uses_service: {Kind.service},
    Relation.implemented_in: {Kind.language},
    Relation.owned_by: {Kind.person, Kind.organization},
    Relation.has_habit: {Kind.habit},
    Relation.occurred: {Kind.event},
    Relation.resolved: {Kind.issue},
    Relation.learned: {Kind.lesson},
    Relation.decided: {Kind.decision},
    Relation.prefers: {Kind.topic, Kind.language, Kind.framework, Kind.service},
}
EVENT_RELATIONS = {Relation.occurred, Relation.resolved, Relation.learned, Relation.worked_on}


class Message(Model):
    id: Key
    role: Literal["user", "assistant", "tool", "note"]
    content: Annotated[str, Field(min_length=1, max_length=120_000)]
    timestamp: AwareDatetime | None = None


class Transcript(Model):
    namespace: Key
    source_id: Key
    session_id: Key
    source_kind: Literal["transcript", "memory_import"] = "transcript"
    source_uri: Annotated[str, Field(max_length=2000)] | None = None
    source_created_at: AwareDatetime | None = None
    source_updated_at: AwareDatetime | None = None
    focus_message_ids: list[Key] = Field(default_factory=list)
    messages: Annotated[list[Message], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def unique_messages(self):
        if len({m.id for m in self.messages}) != len(self.messages):
            raise ValueError("Message IDs must be unique within a source revision")
        if not set(self.focus_message_ids) <= {m.id for m in self.messages}:
            raise ValueError("Focus messages must exist in the transcript")
        if sum(len(m.content) for m in self.messages) > 500_000:
            raise ValueError("Split transcripts into chunks of at most 500000 characters")
        return self


class Entity(Model):
    key: Key
    name: Text
    kind: Kind
    aliases: Annotated[list[Text], Field(max_length=30)] = Field(default_factory=list)


class Evidence(Model):
    message_id: Key
    quote: Annotated[str, Field(min_length=1, max_length=4000)]


class Fact(Model):
    subject: Key
    relation: Relation
    target: Key
    status: Literal["active", "planned", "ended", "uncertain"] = "active"
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    # A slot describes an exclusive role, e.g. production-primary, never a whole
    # predicate by default: projects can legitimately have multiple databases.
    slot: Key | None = None
    valid_at: AwareDatetime | None = None
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=20)]

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        # Model validators are not automatically represented in JSON Schema.
        # Offer only combinations accepted by event_time, including to MCP callers.
        base = handler.resolve_ref_schema(handler(core_schema))
        state, dated, undated = (deepcopy(base) for _ in range(3))
        state["properties"]["relation"] = {
            "type": "string",
            "enum": sorted(r.value for r in Relation if r not in EVENT_RELATIONS),
        }
        for branch in (dated, undated):
            branch["properties"]["relation"] = {
                "type": "string",
                "enum": sorted(r.value for r in EVENT_RELATIONS),
            }
        dated["properties"]["valid_at"] = {"type": "string", "format": "date-time"}
        dated["required"] = [*dated["required"], "valid_at"]
        undated["properties"]["valid_at"] = {"type": "null"}
        undated["properties"]["status"] = {"type": "string", "enum": ["uncertain"]}
        undated["required"] = [*undated["required"], "status"]
        undated["description"] = "An event claim with unknown occurrence time; retain uncertainty."
        return {"title": base.get("title", "Fact"), "anyOf": [state, dated, undated]}

    @model_validator(mode="after")
    def event_time(self):
        if (
            self.relation in EVENT_RELATIONS
            and self.valid_at is None
            and self.status != "uncertain"
        ):
            raise ValueError(
                "Events require an explicit occurrence time; undated event claims must use status=uncertain and valid_at=null. Never use ingestion time"
            )
        return self


class Extraction(Model):
    entities: Annotated[list[Entity], Field(max_length=500)]
    facts: Annotated[list[Fact], Field(max_length=1000)]

    @model_validator(mode="after")
    def relationships(self):
        entities = {e.key: e for e in self.entities}
        if len(entities) != len(self.entities):
            raise ValueError("Entity keys must be unique")
        for f in self.facts:
            if f.subject not in entities or f.target not in entities:
                raise ValueError("Every relationship endpoint must be declared")
            if f.relation in TARGETS and entities[f.target].kind not in TARGETS[f.relation]:
                raise ValueError(f"Invalid target kind for {f.relation}")
            if f.relation == Relation.implemented_in and entities[f.subject].kind != Kind.framework:
                raise ValueError("implemented_in links a framework to a language")
        return self

    def validate_evidence(self, transcript: Transcript):
        messages = {m.id: m for m in transcript.messages}
        for fact in self.facts:
            if transcript.focus_message_ids and not any(
                e.message_id in transcript.focus_message_ids for e in fact.evidence
            ):
                raise ValueError("A feed fact must cite at least one new focus message")
            for evidence in fact.evidence:
                message = messages.get(evidence.message_id)
                if message is None or evidence.quote not in message.content:
                    raise ValueError("Evidence must quote an exact substring of its source message")
            if fact.valid_at and fact.valid_at > now() and fact.status == "active":
                raise ValueError("Future facts must be planned, not active")
        return self


class Scope(Model):
    namespace: Key


class Remember(Model):
    transcript: Transcript


class Ingest(Remember):
    extract: bool = False


class EpisodeRequest(Scope):
    episode_id: Key


class Commit(EpisodeRequest):
    extraction: Extraction


class HistoricalScope(Scope):
    known_at: AwareDatetime | None = Field(
        default=None,
        description="What the graph knew at this time; distinct from the date a fact was true.",
    )
    at_change: Annotated[int, Field(ge=0)] | None = Field(
        default=None, description="Exact journal change number, instead of known_at."
    )

    @model_validator(mode="after")
    def one_history_cutoff(self):
        if self.known_at is not None and self.at_change is not None:
            raise ValueError("Choose known_at or at_change, not both")
        return self


class Recall(HistoricalScope):
    query: Text
    as_of: AwareDatetime | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 30


class Latest(HistoricalScope):
    entity: Text
    relation: Relation | None = None
    as_of: AwareDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_habit_query(cls, value):
        if isinstance(value, dict) and "habit" in value and "entity" not in value:
            value = {**value, "entity": value["habit"]}
            value.pop("habit")
            value.setdefault("relation", "occurred")
        return value


class Pending(Scope):
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class Merge(Scope):
    source_key: Key
    target_key: Key
    reason: Text


class Retract(Scope):
    fact_id: Key
    reason: Text


class Empty(Model):
    pass


class Insight(Model):
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    entity_keys: Annotated[list[Key], Field(min_length=1, max_length=10)]
    supporting_fact_ids: Annotated[list[Key], Field(min_length=1, max_length=20)]
    confidence: Annotated[float, Field(ge=0, le=1)]


class DreamOutput(Model):
    insights: Annotated[list[Insight], Field(max_length=50)]
    observations: Annotated[list[Text], Field(max_length=50)]


class DreamCreate(Scope):
    query: Text
    episode_ids: Annotated[list[Key], Field(min_length=1, max_length=100)]
    instructions: Annotated[str, Field(max_length=4096)] = (
        "Find durable patterns, conflicts, and useful connections."
    )


class DreamRequest(Scope):
    dream_id: Key
