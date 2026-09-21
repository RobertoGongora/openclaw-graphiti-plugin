"""Source text is journaled by reference, checkpoints are stored in parts, and
journals written by the version 2 engine keep replaying underneath."""

import json
from datetime import UTC, datetime

import pytest

from graph_memory import journal as journal_module
from graph_memory.journal import (
    REF,
    Journal,
    capture,
    difference,
    hexhash,
    live_hash,
    set_hash,
)
from graph_memory.models import Extraction, Transcript, now
from graph_memory.store import digest

from .helpers import MYSQL, PG, PROJECT

BODY = " ".join(["The quick brown fox keeps a long and unmistakable body of source text."] * 100)


def transcript(ns, source, claim="Atlas uses MySQL.", messages=3):
    return Transcript.model_validate(
        {
            "namespace": ns,
            "source_id": source,
            "session_id": source,
            "source_format": "session-records-v1",
            "messages": [
                {
                    "id": "m0",
                    "role": "user",
                    "source_type": "user_assertion",
                    "content": claim,
                    "timestamp": "2026-09-14T10:00:00Z",
                },
                *(
                    {
                        "id": f"m{i}",
                        "role": "tool",
                        "source_type": "memory_read",
                        "content": f"{source} result {i}. {BODY}",
                        "touches": [
                            {
                                "path": f"/memory/{source}-{i}.md",
                                "operation": "read",
                                "captured": "excerpt",
                                "content": f"{source} file {i}. {BODY}",
                            }
                        ],
                    }
                    for i in range(1, messages)
                ),
            ],
        }
    )


def extraction(claim="Atlas uses MySQL.", target=MYSQL, day=10):
    return Extraction.model_validate(
        {
            "entities": [PROJECT, target],
            "facts": [
                {
                    "summary": claim,
                    "evidence": [{"message_id": "m0", "quote": claim}],
                    "subject": PROJECT["key"],
                    "target": target["key"],
                    "relation": "uses_database",
                    "slot": "primary",
                    "valid_at": f"2026-09-{day:02d}T10:00:00Z",
                }
            ],
        }
    )


def query(store, text, **params):
    return store.transaction(lambda tx: tx.run(text, **params).data())


def head(store, ns):
    return query(store, "MATCH (s:MemorySpace {id:$ns}) RETURN properties(s) AS s", ns=ns)[0]["s"]


def payloads(store, ns):
    rows = query(
        store,
        "MATCH (e:MemoryChange {scope:$ns}) RETURN e.payload AS payload ORDER BY e.sequence",
        ns=ns,
    )
    return [row["payload"] for row in rows]


def live(store, ns):
    return store.transaction(lambda tx: capture(tx, ns))


def v2_write(store, ns, monkeypatch, operation=None, checkpoint=False, version=2):
    """One write as the version 2 engine journaled it: every body in the delta,
    and the whole state embedded in a baseline or checkpoint. Version 1 hashed the
    state as a whole and did not mark its checkpoints."""

    def run(tx):
        store.lock(tx, ns)
        before = capture(tx, ns)
        if operation:
            with monkeypatch.context() as patch:
                patch.setattr(
                    store, "mutate", lambda tx, ns, kind, details, op, scoped=False: op(tx)
                )
                operation(tx)
        after = capture(tx, ns)
        changes = difference(before, after)
        for change in changes:
            change.pop("hash", None)
            change["set"] = {k: after[change["label"]][change["id"]][k] for k in change["set"]}
        space = tx.run("MATCH (s:MemorySpace {id:$ns}) RETURN properties(s) AS s", ns=ns).single()[
            "s"
        ]
        sequence = space.get("journal_sequence", -1) + 1
        recorded_us = max(int(now().timestamp() * 1_000_000), space.get("journal_us", 0) + 1)
        event = {
            "version": version,
            "sequence": sequence,
            "scope": ns,
            "kind": "checkpoint" if checkpoint else "write",
            "recorded_us": recorded_us,
            "recorded_at": datetime.fromtimestamp(recorded_us / 1_000_000, UTC).isoformat(),
            "revision": space.get("revision", 0),
            "engine": store.engine,
            "previous_hash": space.get("journal_hash"),
            "state_hash": hexhash(set_hash(after)) if version > 1 else digest(after),
            "details": {},
            "changes": changes,
        }
        if checkpoint:
            event["snapshot"] = after
        tx.run(
            "CREATE (e:MemoryChange {id:$id,namespace:$audit,scope:$ns,sequence:$seq,kind:$kind,"
            "recorded_at:$at,recorded_us:$us,payload:$payload,hash:$hash,checkpoint:$checkpoint}) "
            "WITH e MATCH (s:MemorySpace {id:$ns}) SET s.journal_sequence=$seq,"
            "s.journal_hash=$hash,s.journal_us=$us,s.journal_state_hash=$state,"
            "s.journal_set_hash=$set",
            id=digest(["journal", ns, sequence]),
            audit="audit:" + ns,
            ns=ns,
            seq=sequence,
            kind=event["kind"],
            at=event["recorded_at"],
            us=event["recorded_us"],
            payload=json.dumps(event, sort_keys=True),
            hash=digest(event),
            state=event["state_hash"],
            set=event["state_hash"] if version > 1 else None,
            checkpoint=checkpoint if version > 1 else None,
        ).consume()
        return sequence

    return store.transaction(run)


def mixed_chain(store, ns, monkeypatch):
    """A version 2 journal that the current engine continues. Returns the live
    capture taken after every change, by sequence."""
    seen = {}

    def record(sequence=None):
        seen[head(store, ns)["journal_sequence"] if sequence is None else sequence] = live(
            store, ns
        )

    one = transcript(ns, "one")
    episode = digest(one.model_dump(mode="json"))
    record(v2_write(store, ns, monkeypatch, checkpoint=True))
    record(v2_write(store, ns, monkeypatch, lambda tx: store.stage(one, transaction=tx)))
    record(
        v2_write(
            store,
            ns,
            monkeypatch,
            lambda tx: store.commit(ns, episode, extraction(), transaction=tx),
        )
    )
    boundary = head(store, ns)["journal_sequence"]
    two = transcript(ns, "two", "Atlas uses Postgres.")
    receipt = store.stage(two)
    record()
    committed = store.commit(
        ns, receipt["episode_id"], extraction("Atlas uses Postgres.", PG, day=15)
    )
    record()
    store.retract(ns, committed["fact_ids"][0], "Superseded")
    record()
    Journal(store).checkpoint(ns)
    record()
    store.stage(transcript(ns, "three", "Atlas uses SQLite."))
    record()
    return seen, boundary, committed["fact_ids"][0]


def test_staged_source_text_is_referenced_and_the_hash_is_unchanged(graph):
    store, ns = graph
    source = transcript(ns, "one")
    store.stage(source)
    baseline, delta = payloads(store, ns)
    assert BODY not in delta and "Atlas uses MySQL." in delta  # short bodies stay inline
    assert delta.count(REF) == 5  # the payload, two messages, two file excerpts
    # What is left is identifiers and hashes: about 700 bytes a node.
    assert len(delta) < 8_000 and 4 * len(delta) < len(source.model_dump_json())
    event = json.loads(delta)
    assert event["version"] == 3 and "snapshot" not in event
    assert "parts" in json.loads(baseline) and "snapshot" not in json.loads(baseline)
    # The hash is of the real values: the same one a version 2 engine arrives at.
    state = live(store, ns)
    by_key = {(c["label"], c["id"]): c for c in event["changes"]}
    assert all(
        by_key[label, key]["hash"] == hexhash(journal_module.element_hash(label, props))
        for label in state
        for key, props in state[label].items()
    )
    assert (
        head(store, ns)["journal_set_hash"]
        == hexhash(set_hash(state))
        == store.transaction(lambda tx: live_hash(tx, ns))
    )
    # A commit touches the episode again without carrying its payload again.
    receipt = store.commit(ns, event["details"]["episode_id"], extraction())
    assert receipt["fact_ids"] and BODY not in payloads(store, ns)[-1]
    journal = Journal(store)
    assert journal.verify(ns)["verified"] and journal.verify_live(ns)["verified"]


def test_reconstruction_recall_and_evidence_across_the_version_boundary(graph, monkeypatch):
    store, ns = graph
    seen, boundary, fact = mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    versions = [json.loads(p)["version"] for p in payloads(store, ns)]
    assert versions == [2] * (boundary + 1) + [3] * (len(versions) - boundary - 1)
    assert len(seen) == len(versions)
    for sequence, expected in seen.items():
        snapshot = journal.snapshot(ns, sequence=sequence)
        assert journal.resolve(ns, snapshot["state"]) == expected
    # Version 2 state carries its bodies; what the current engine adds is a reference
    # that reads as the body when a caller asks for it.
    state = journal.snapshot(ns)["state"]
    raw = [dict.get(e, "payload") for e in state["MemoryEpisode"].values()]
    assert [isinstance(p, dict) for p in raw] == [True] * 3  # all behind a checkpoint
    mixed = journal.snapshot(ns, sequence=boundary + 1)["state"]["MemoryEpisode"]
    assert sorted(isinstance(dict.get(e, "payload"), dict) for e in mixed.values()) == [False, True]
    assert all(BODY in e["payload"] and BODY in e.get("payload") for e in mixed.values())

    # Historical recall and evidence on both sides of the boundary.
    def current(**cutoff):
        return [f["target"] for f in store.recall(ns, "Atlas", **cutoff)["current"]]

    assert current(at_change=boundary) == [MYSQL["key"]]
    assert current(at_change=boundary + 2) == [PG["key"]]
    assert current() == current(at_change=max(seen)) == [MYSQL["key"]]  # Postgres retracted
    from graph_memory import retrieval

    past = retrieval.evidence(
        store,
        retrieval.EvidenceRequest(namespace=ns, fact_ids=[fact], at_change=boundary + 2),
    )
    assert not past["facts"][0]["fact"]["retracted"]
    assert past["facts"][0]["claims"][0]["quote"] == "Atlas uses Postgres."
    assert past["facts"][0]["claims"][0]["message_available"]


def test_all_three_versions_replay_in_one_chain(graph, monkeypatch):
    store, ns = graph
    first = transcript(ns, "first")
    query(store, "MERGE (s:MemorySpace {id:$ns}) ON CREATE SET s.revision=0", ns=ns)
    with monkeypatch.context() as patch:
        patch.setattr(store, "mutate", lambda tx, ns, kind, details, op, scoped=False: op(tx))
        store.stage(first)
    seen = {v2_write(store, ns, monkeypatch, checkpoint=True, version=1): live(store, ns)}
    second = transcript(ns, "second", "Atlas uses Postgres.")
    wrote = v2_write(store, ns, monkeypatch, lambda tx: store.stage(second, transaction=tx))
    seen[wrote] = live(store, ns)
    store.stage(transcript(ns, "third", "Atlas uses SQLite."))
    seen[wrote + 1] = live(store, ns)
    journal = Journal(store)
    assert [json.loads(p)["version"] for p in payloads(store, ns)] == [1, 2, 3]
    for sequence, expected in seen.items():
        assert journal.resolve(ns, journal.snapshot(ns, sequence=sequence)["state"]) == expected
    assert journal.verify(ns)["verified"]
    journal.checkpoint(ns)
    assert journal.resolve(ns, journal.snapshot(ns)["state"]) == seen[wrote + 1]
    assert journal.verify(ns)["verified"]


def test_verify_streams_a_mixed_chain(graph, monkeypatch):
    store, ns = graph
    mixed_chain(store, ns, monkeypatch)
    real = journal_module.capture

    def scoped_only(tx, namespace, ids=None):
        assert ids is not None, "verify captured the whole namespace"
        return real(tx, namespace, ids)

    monkeypatch.setattr(journal_module, "capture", scoped_only)
    monkeypatch.setattr(
        journal_module, "set_hash", lambda state: pytest.fail("hashed a whole state")
    )
    monkeypatch.setattr(
        journal_module, "list", lambda *a: pytest.fail("materialised events"), raising=False
    )
    verified = Journal(store).verify(ns)
    assert verified["verified"] and verified["records"] == sum(
        map(len, Journal(store).snapshot(ns)["state"].values())
    )


def test_chunked_checkpoint_round_trip_and_tamper_detection(graph, monkeypatch):
    store, ns = graph
    monkeypatch.setattr(journal_module, "PART_BYTES", 2_000)
    for i in range(6):
        store.stage(transcript(ns, f"s{i}", f"Note number {i}."))
    journal = Journal(store)
    checkpoint = journal.checkpoint(ns)
    sequence = checkpoint["sequence"]
    assert checkpoint["records"] == sum(map(len, live(store, ns).values()))
    store.stage(transcript(ns, "later", "A later note."))
    event = json.loads(payloads(store, ns)[sequence])
    parts = query(
        store,
        "MATCH (:MemoryChange {scope:$ns,sequence:$seq})-[r:PART]->(p:MemorySnapshotPart) "
        "RETURN r.i AS i,p.i AS pi,p.label AS label,p.data AS data,p.scope AS scope,"
        "p.namespace AS namespace ORDER BY r.i",
        ns=ns,
        seq=sequence,
    )
    assert len(parts) == len(event["parts"]) > len({p["label"] for p in parts}) >= 4
    assert [p["label"] for p in parts] == [p["label"] for p in event["parts"]]
    assert all(p["i"] == p["pi"] == i and p["scope"] == ns for i, p in enumerate(parts))
    assert all(isinstance(p["data"], bytes) for p in parts)  # a byte array, not base64 text
    assert all(p["bytes"] < 3_500 for p in event["parts"])
    assert sum(p["records"] for p in event["parts"]) == checkpoint["records"]
    assert journal.resolve(ns, journal.snapshot(ns)["state"]) == live(store, ns)
    assert journal.verify(ns)["verified"]

    def tampered(statement, message, broken, healed):
        """The statement breaks the journal, which says so; undoing it heals it."""
        for value, error in ((broken, message), (healed, None)):
            query(store, statement, ns=ns, seq=sequence, value=value)
            if error:
                with pytest.raises(ValueError, match=error):
                    journal.snapshot(ns)
                with pytest.raises(ValueError, match=error):
                    journal.verify(ns)
        assert journal.verify(ns)["verified"]

    part = "MATCH (:MemoryChange {scope:$ns,sequence:$seq})-[r:PART]->(p {i:1}) "
    tampered(part + "SET p.data=$value", "checkpoint integrity", parts[0]["data"], parts[1]["data"])
    tampered(part + "SET r.i=$value", "checkpoint integrity", -1, 1)  # a part goes missing
    # A part hash in the event is covered by the event hash, and so by the chain.
    forged = dict(event, parts=[dict(event["parts"][0], sha256="0" * 64), *event["parts"][1:]])
    tampered(
        "MATCH (e:MemoryChange {scope:$ns,sequence:$seq}) SET e.payload=$value",
        "Journal integrity check failed",
        json.dumps(forged, sort_keys=True),
        json.dumps(event, sort_keys=True),
    )


def test_an_edited_body_is_found_when_it_is_read_and_by_verify(graph):
    store, ns = graph
    store.stage(transcript(ns, "one"))
    journal = Journal(store)
    journal.checkpoint(ns)
    message = query(
        store,
        "MATCH (m:MemoryMessage {namespace:$ns}) WHERE m.content CONTAINS 'result 1' "
        "SET m.content=m.content+' edited' RETURN m.id AS id",
        ns=ns,
    )[0]["id"]
    # Replaying events proves the chain, not the bodies outside it.
    state = journal.snapshot(ns)["state"]
    with pytest.raises(ValueError, match="Journal integrity check failed"):
        state["MemoryMessage"][message]["content"]
    with pytest.raises(ValueError, match="Journal integrity check failed"):
        journal.resolve(ns, state)
    with pytest.raises(ValueError, match="Journal integrity check failed"):
        journal.replay(ns, "replay:" + ns)
    with pytest.raises(ValueError, match="differs from journal"):
        journal.verify(ns)
    with pytest.raises(ValueError, match="differs from journal"):
        journal.verify_live(ns)
    query(store, "MATCH (m:MemoryMessage {id:$id}) DETACH DELETE m", id=message)
    with pytest.raises(ValueError, match="Journal integrity check failed"):
        state["MemoryMessage"][message]["content"]
    with pytest.raises(ValueError, match="differs from journal"):
        journal.verify(ns)


def test_replay_materialises_real_bodies_and_cleanup_takes_the_parts(graph, monkeypatch):
    store, ns = graph
    seen, boundary, _ = mixed_chain(store, ns, monkeypatch)
    journal = Journal(store)
    target = "replay:" + ns
    wipe = (
        "MATCH (n) WHERE n.namespace=$ns OR (n:MemoryChange AND n.scope=$ns) "
        "OR (n:MemorySpace AND n.id=$ns) DETACH DELETE n"
    )
    try:
        journal.replay(ns, target, sequence=boundary + 2)
        bodies = query(
            store,
            "MATCH (n {namespace:$ns}) WHERE n:MemoryMessage OR n:MemoryArtifactObservation "
            "RETURN n.content AS body UNION ALL "
            "MATCH (n:MemoryEpisode {namespace:$ns}) RETURN n.payload AS body",
            ns=target,
        )
        expected = seen[boundary + 2]
        assert len(bodies) == sum(
            len(expected[label])
            for label in ("MemoryEpisode", "MemoryMessage", "MemoryArtifactObservation")
        )
        assert all(isinstance(b["body"], str) for b in bodies)
        assert sum(BODY in b["body"] for b in bodies) == 10
        assert journal.verify(target)["verified"]
        assert store.recall(target, "Atlas")["current"][0]["target"] == PG["key"]
        count = "MATCH (p:MemorySnapshotPart {scope:$ns}) RETURN count(p) AS parts"
        assert query(store, count, ns=target)[0]["parts"] > 0
        assert query(store, count, ns=ns)[0]["parts"] > 0
        # The cleanup the test fixture runs.
        for namespace in (target, ns):
            query(store, wipe, ns=namespace)
            assert query(store, count, ns=namespace)[0]["parts"] == 0
    finally:
        query(store, wipe, ns=target)


def test_journal_growth_is_a_small_constant_per_episode(graph):
    store, ns = graph
    journal = Journal(store)
    assert journal.checkpoint_due(ns) is False  # nothing journaled yet

    def delta_bytes():
        return head(store, ns)["journal_delta_bytes"]

    sizes = []
    for i in range(24):
        store.stage(transcript(ns, f"s{i}", f"Note number {i}.", messages=6))
        sizes.append(delta_bytes())
    stored = query(
        store,
        "MATCH (e:MemoryChange {scope:$ns}) WHERE e.sequence>0 "
        "RETURN sum(size(e.payload)) AS bytes",
        ns=ns,
    )[0]["bytes"]
    assert stored == sizes[-1]  # the head counts what was appended
    per_episode = [b - a for a, b in zip(sizes, sizes[1:], strict=False)]
    assert max(per_episode) - min(per_episode) < 200  # linear: no episode pays for the graph
    assert sizes[-1] / 24 < 14_000  # 18 nodes, against 78 KB of text
    content = query(
        store,
        "MATCH (m:MemoryMessage {namespace:$ns}) RETURN sum(size(m.content)) AS bytes",
        ns=ns,
    )[0]["bytes"]
    assert content > 24 * 5 * len(BODY) > 2 * sizes[-1]  # the text dwarfs its journal
    assert journal.checkpoint_due(ns, threshold=sizes[-1]) is True
    assert journal.checkpoint_due(ns, threshold=sizes[-1] + 1) is False
    assert journal.checkpoint_due(ns) is False  # 64 MB by default
    sequence = journal.checkpoint(ns)["sequence"]
    event = json.loads(payloads(store, ns)[sequence])
    uncompressed = sum(p["bytes"] for p in event["parts"])
    at_rest = query(
        store,
        "MATCH (p:MemorySnapshotPart {scope:$ns,sequence:$seq}) RETURN sum(size(p.data)) AS bytes",
        ns=ns,
        seq=sequence,
    )[0]["bytes"]
    assert at_rest < uncompressed < content / 4
    assert len(payloads(store, ns)[sequence]) < 2_000
    after = head(store, ns)
    assert after["journal_delta_bytes"] == 0 and after["journal_checkpoint_bytes"] == uncompressed
    # Due again once the deltas outweigh the checkpoint they would replace.
    store.stage(transcript(ns, "later", "A later note.", messages=6))
    assert journal.checkpoint_due(ns, threshold=0) is False
    assert 0 < delta_bytes() < uncompressed
    assert journal.verify(ns)["verified"]
