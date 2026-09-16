"""The model-facing contract must encode guards already enforced at commit."""

from graph_memory.llm import strict_schema
from graph_memory.models import EVENT_RELATIONS, Extraction, Relation


def test_exported_schema_cannot_generate_an_active_undated_event():
    schema = strict_schema(Extraction.model_json_schema())
    fact_ref = schema["properties"]["facts"]["items"]["$ref"].split("/")[-1]
    alternatives = schema["$defs"][fact_ref]["anyOf"]
    # Each alternative is a complete object, compatible with strict structured output.
    assert all(a["additionalProperties"] is False for a in alternatives)
    assert all(set(a["required"]) == set(a["properties"]) for a in alternatives)
    covered = set()
    for branch in alternatives:
        properties = branch["properties"]
        relations = set(properties["relation"]["enum"])
        covered |= relations
        if relations & EVENT_RELATIONS:
            assert relations <= EVENT_RELATIONS
            time_schema = properties["valid_at"]
            if time_schema["type"] == "null":
                assert properties["status"]["enum"] == ["uncertain"]
            else:
                assert time_schema == {"type": "string", "format": "date-time"}
    assert covered == set(Relation)
