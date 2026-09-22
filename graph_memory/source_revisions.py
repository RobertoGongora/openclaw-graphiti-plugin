"""Reviewed continuation of a rewritten source; old knowledge is never rewritten.

Feed metadata is operational state. A revision archives that cursor and starts a
new session at the first changed chunk. Episodes and their immutable payloads,
facts, messages and journal history stay attached to their original session.
Stop the intake worker before applying and restart it to refresh its feed cache.
"""

import hashlib
import json
from pathlib import Path

from .models import now
from .session_sources import FORMAT, before_shell_results, records
from .store import digest


def feed(tx, namespace, feed_id):
    row = tx.run(
        "MATCH (f:MemoryFeed {namespace:$ns,id:$id}) RETURN properties(f) AS f",
        ns=namespace,
        id=feed_id,
    ).single()
    if not row:
        raise ValueError("Source feed not found")
    return row["f"]


def read_source(path):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("Source has an incomplete trailing record")
    messages = [m.model_dump(mode="json") for m in records(path)]
    if path.read_bytes() != raw:
        raise ValueError("Source changed while planning its revision")
    return path, messages, hashlib.sha256(raw).hexdigest()


def plan(store, namespace, feed_id, path, reason, *, transaction=None):
    if not reason or not reason.strip():
        raise ValueError("A source revision requires a reason")
    path, messages, raw_hash = read_source(path)

    def inspect(tx):
        old = feed(tx, namespace, feed_id)
        if old.get("superseded_by"):
            raise ValueError("Source feed was already superseded")
        if old.get("source_format") != FORMAT or not old.get("source_key"):
            raise ValueError("Revision requires a named source-record feed")
        if str(path) != old["source_uri"]:
            raise ValueError("Read the source through its stored mount path")
        count = old["message_count"]
        prefix = messages[:count]
        if count <= len(messages) and old["prefix_hash"] in (
            digest(prefix),
            digest([before_shell_results(m) for m in prefix]),
        ):
            raise ValueError("Source prefix is unchanged; no revision is needed")
        rows = tx.run(
            "MATCH (e:MemoryEpisode {namespace:$ns,session_id:$session}) "
            "WHERE e.source_id STARTS WITH $prefix RETURN e.id AS id,e.payload AS payload",
            ns=namespace,
            session=old["session_id"],
            prefix=f"records:{feed_id}:",
        ).data()
        if any(digest(json.loads(r["payload"])) != r["id"] for r in rows):
            raise ValueError("Stored source payload identity differs")
        batches = sorted(
            [json.loads(r["payload"]) for r in rows],
            key=lambda p: int(p["source_id"].rsplit(":", 1)[1]),
        )
        original = []
        for batch in batches:
            # A second rewrite needs a full-history review of its ancestor too.
            start = int(batch["source_id"].rsplit(":", 1)[1])
            by_id = {m["id"]: m for m in batch["messages"]}
            if not original and start:
                raise ValueError("Nested source revisions require an explicit full-history review")
            if start != len(original):
                raise ValueError("Stored source batches are not contiguous")
            original.extend(by_id[mid] for mid in batch["focus_message_ids"])
        # Immutable payloads can contain older parser classifications than the
        # latest cursor hash, so their concatenation need not hash to that cursor.
        if len(original) != count:
            raise ValueError("Stored evidence does not reconstruct the source cursor")
        common = 0
        for a, b in zip(original, messages, strict=False):
            if before_shell_results(a) != before_shell_results(b):
                break
            common += 1
        body = {
            "kind": "source_revision_v1",
            "namespace": namespace,
            "feed_id": feed_id,
            "old_session_id": old["session_id"],
            "source_uri": str(path),
            "source_key": old["source_key"],
            "old_count": count,
            "old_prefix_hash": old["prefix_hash"],
            "original_evidence_hash": digest(original),
            "source_sha256": raw_hash,
            "current_count": len(messages),
            "common_prefix_count": common,
            "common_prefix_hash": digest(messages[:common]),
            "retained_old_tail_chunks": count - common,
            "new_tail_chunks": len(messages) - common,
            "reason": reason.strip(),
            "effect": "Retain all original knowledge; ingest the changed tail under a new session identity",
        }
        return {**body, "digest": digest(body)}

    return inspect(transaction) if transaction is not None else store.read(inspect)


def apply(store, namespace, reviewed):
    if reviewed.get("namespace") != namespace:
        raise ValueError("Source revision namespace differs")
    body = {k: v for k, v in reviewed.items() if k != "digest"}
    if reviewed.get("digest") != digest(body):
        raise ValueError("Source revision plan digest differs")

    def publish(tx):
        store.lock(tx, namespace)
        old = feed(tx, namespace, reviewed["feed_id"])
        if old.get("superseded_by"):
            if old.get("revision_digest") != reviewed["digest"]:
                raise ValueError("Source feed was superseded by a different revision")
            return {"applied": True, "replayed": True, "feed_id": old["superseded_by"]}

        current = plan(
            store,
            namespace,
            reviewed["feed_id"],
            reviewed["source_uri"],
            reviewed["reason"],
            transaction=tx,
        )
        if current != reviewed:
            raise ValueError("Source or cursor changed after review; prepare a new plan")
        fid = digest(["source-revision-v1", namespace, reviewed["digest"]])
        session = "source-revision:" + fid
        tx.run(
            "CREATE (f:MemoryFeed) SET f=$props",
            props={
                "id": fid,
                "namespace": namespace,
                "session_id": session,
                "source_uri": old["source_uri"],
                "source_key": old["source_key"],
                "source_format": FORMAT,
                "name": old.get("name", "Revised source"),
                "session_uid": old.get("session_uid"),
                "message_count": reviewed["common_prefix_count"],
                "prefix_hash": reviewed["common_prefix_hash"],
                "revision_of": old["id"],
                "revision_digest": reviewed["digest"],
            },
        ).consume()
        tx.run(
            "MATCH (f:MemoryFeed {namespace:$ns,id:$id}) "
            "SET f.superseded_by=$next,f.superseded_at=$at,f.revision_digest=$digest,f.revision_plan=$plan",
            ns=namespace,
            id=old["id"],
            next=fid,
            at=now().isoformat(),
            digest=reviewed["digest"],
            plan=json.dumps(reviewed),
        ).consume()
        return {
            "applied": True,
            "replayed": False,
            "feed_id": fid,
            "session_id": session,
            "common_prefix_count": reviewed["common_prefix_count"],
            "new_tail_chunks": reviewed["new_tail_chunks"],
            "restart_worker": True,
        }

    return store.transaction(publish)
