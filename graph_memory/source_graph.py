"""Deterministic source provenance graph, derived only from immutable episode payloads."""

from pathlib import PurePosixPath

from .store import digest

LABELS = ("MemorySession", "MemoryMessage", "MemoryArtifact", "MemoryArtifactObservation")


def caption(text, limit=140):
    return " ".join(text.split())[:limit]


def save(tx, transcript, episode_id):
    if transcript.source_format != "session-records-v1":
        return
    ns = transcript.namespace
    sid = digest([ns, "session", transcript.session_id])
    tx.run(
        "MERGE (s:MemorySession {id:$id}) ON CREATE SET s.namespace=$ns,s.name=$name,s.source_uri=$uri,s.session_id=$session",
        id=sid,
        ns=ns,
        name=transcript.title or "Session",
        uri=transcript.source_uri,
        session=transcript.session_id,
    ).consume()
    mids = []
    for m in transcript.messages:
        mid = digest([ns, transcript.session_id, m.id])
        mids.append(mid)
        stamp = m.timestamp.isoformat() if m.timestamp else None
        name = caption(
            f"{m.source_type.replace('_', ' ')} · {stamp or 'undated'} · {m.tool_name or m.content}"
        )
        props = {
            "id": mid,
            "namespace": ns,
            "name": name,
            "session_ref": sid,
            "message_id": m.id,
            "record_id": m.record_id,
            "role": m.role,
            "source_type": m.source_type,
            "timestamp": stamp,
            "content": m.content,
            "call_id": m.call_id,
            "tool_name": m.tool_name,
            "tool_failed": m.tool_failed,
            "gaps": m.gaps,
        }
        old = tx.run(
            "MATCH (m:MemoryMessage {id:$id}) RETURN m.content AS content", id=mid
        ).single()
        if old and old["content"] != m.content:
            raise ValueError("Source message identity changed; use a reviewed source revision")
        tx.run(
            "MERGE (m:MemoryMessage {id:$id}) ON CREATE SET m=$props", id=mid, props=props
        ).consume()
        for index, touch in enumerate(m.touches):
            aid = digest(
                [
                    ns,
                    "artifact",
                    touch.path,
                    None if touch.path.startswith("/") else transcript.session_id,
                ]
            )
            oid = digest([mid, index, touch.model_dump()])
            tx.run(
                "MERGE (a:MemoryArtifact {id:$id}) ON CREATE SET a.namespace=$ns,a.name=$name,a.path=$path",
                id=aid,
                ns=ns,
                name=PurePosixPath(touch.path).name,
                path=touch.path,
            ).consume()
            props = {
                "id": oid,
                "namespace": ns,
                "name": caption(
                    f"{touch.operation} · {PurePosixPath(touch.path).name} · {stamp or 'undated'}"
                ),
                "artifact_ref": aid,
                "message_ref": mid,
                "operation": touch.operation,
                "captured": touch.captured,
                "content": touch.content,
                "content_hash": digest(touch.content) if touch.content else None,
                "gap": touch.gap,
                "timestamp": stamp,
            }
            tx.run(
                "MERGE (o:MemoryArtifactObservation {id:$id}) ON CREATE SET o=$props",
                id=oid,
                props=props,
            ).consume()
    tx.run(
        "MATCH (e:MemoryEpisode {id:$id}) SET e.session_ref=$session,e.message_refs=$messages",
        id=episode_id,
        session=sid,
        messages=mids,
    ).consume()
    repair(tx, ns)


def repair(tx, ns):
    queries = [
        "MATCH (e:MemoryEpisode {namespace:$ns}),(s:MemorySession {namespace:$ns}) WHERE e.session_ref=s.id MERGE (s)-[:HAS_EPISODE]->(e)",
        "MATCH (e:MemoryEpisode {namespace:$ns}) UNWIND e.message_refs AS mid MATCH (m:MemoryMessage {namespace:$ns,id:mid}) MERGE (e)-[:CONTAINS]->(m)",
        "MATCH (m:MemoryMessage {namespace:$ns}),(s:MemorySession {namespace:$ns}) WHERE m.session_ref=s.id MERGE (s)-[:HAS_MESSAGE]->(m)",
        "MATCH (r:MemoryMessage {namespace:$ns,role:'tool'}),(c:MemoryMessage {namespace:$ns,source_type:'tool_call'}) WHERE r.call_id=c.call_id AND r.session_ref=c.session_ref MERGE (r)-[:RESULT_OF]->(c)",
        "MATCH (o:MemoryArtifactObservation {namespace:$ns}),(m:MemoryMessage {namespace:$ns}),(a:MemoryArtifact {namespace:$ns}) WHERE o.message_ref=m.id AND o.artifact_ref=a.id MERGE (m)-[:TOUCHED_MEMORY]->(o) MERGE (o)-[:VERSION_OF]->(a)",
        "MATCH (f:MemoryFact {namespace:$ns}) UNWIND f.message_refs AS mid MATCH (m:MemoryMessage {namespace:$ns,id:mid}) MERGE (f)-[:CITES]->(m)",
    ]
    queries.append(
        "MATCH (f:MemoryFact {namespace:$ns}) UNWIND f.validation_message_refs AS mid MATCH (m:MemoryMessage {namespace:$ns,id:mid}) MERGE (f)-[:VALIDATED_BY]->(m)"
    )
    for q in queries:
        tx.run(q, ns=ns).consume()
