from graph_memory.models import Extraction, Transcript


def ingest(store, namespace, source, text, entities, facts, timestamp="2026-09-14T10:00:00Z"):
    transcript = Transcript.model_validate(
        {
            "namespace": namespace,
            "source_id": source,
            "session_id": source,
            "messages": [{"id": "m1", "role": "user", "content": text, "timestamp": timestamp}],
        }
    )
    extraction = Extraction.model_validate(
        {
            "entities": entities,
            "facts": [
                {"summary": text, "evidence": [{"message_id": "m1", "quote": text}], **f}
                for f in facts
            ],
        }
    )
    receipt = store.stage(transcript)
    return store.commit(namespace, receipt["episode_id"], extraction), transcript, extraction


PROJECT = {"key": "project:atlas", "name": "Atlas", "kind": "project"}
MYSQL = {"key": "database:mysql", "name": "MySQL", "kind": "database"}
PG = {"key": "database:postgres", "name": "Postgres", "kind": "database", "aliases": ["PostgreSQL"]}
