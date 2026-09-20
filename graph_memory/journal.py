"""Append-only knowledge changes, historical reconstruction, and isolated replay."""

import copy
import json
from collections import Counter
from datetime import UTC, datetime

from .models import now
from .source_graph import LABELS as SOURCE_LABELS
from .store import digest, normalized
from .temporal import project

LABELS = (
    "MemoryEpisode",
    "MemoryEntity",
    "MemoryFact",
    "MemoryDream",
    "MemoryInsight",
) + SOURCE_LABELS
VOLATILE = {
    "worker",
    "worker_lock",
    "lease_until",
    "retry_after",
    "attempts",
    "error",
    "retry_feedback",
    "cached_extraction",
    "cached_engine",
    "cached_model",
    "validation_failures",
    "validation_engine",
    "quarantine_engine",
    "quarantine_reason",
}


def capture(tx, namespace):
    state = {label: {} for label in LABELS}
    rows = tx.run(
        "MATCH (n {namespace:$ns}) WHERE any(label IN labels(n) WHERE label IN $labels) "
        "RETURN labels(n) AS labels,properties(n) AS props",
        ns=namespace,
        labels=list(LABELS),
    ).data()
    for row in rows:
        label = next(label for label in LABELS if label in row["labels"])
        props = {k: v for k, v in row["props"].items() if k not in VOLATILE}
        # Operational retries/leases aren't changes to knowledge. A staged source
        # remains pending until a validated extraction commits.
        if label == "MemoryEpisode" and props["status"] != "complete":
            props["status"] = "pending"
        if label == "MemoryDream" and props["status"] not in ("completed", "applied"):
            props["status"] = "pending"
        state[label][props["id"]] = props
    return state


def difference(before, after):
    changes = []
    for label in LABELS:
        for key in sorted(before[label].keys() | after[label].keys()):
            old, new = before[label].get(key), after[label].get(key)
            if old == new:
                continue
            changes.append(
                {
                    "label": label,
                    "id": key,
                    "deleted": new is None,
                    "set": {k: v for k, v in (new or {}).items() if old is None or old.get(k) != v},
                    "unset": sorted((old or {}).keys() - (new or {}).keys()),
                }
            )
    return changes


def apply(state, changes):
    for change in changes:
        bucket = state[change["label"]]
        if change["deleted"]:
            bucket.pop(change["id"], None)
        else:
            node = bucket.setdefault(change["id"], {})
            node.update(change["set"])
            for key in change["unset"]:
                node.pop(key, None)


class Journal:
    def __init__(self, store):
        self.store = store

    def _head(self, tx, namespace):
        return tx.run(
            "MATCH (s:MemorySpace {id:$ns}) RETURN properties(s) AS s", ns=namespace
        ).single()["s"]

    def _append(self, tx, namespace, kind, details, state, changes=None):
        head = self._head(tx, namespace)
        sequence = head.get("journal_sequence", -1) + 1
        recorded_us = max(int(now().timestamp() * 1_000_000), head.get("journal_us", 0) + 1)
        event = {
            "version": 1,
            "sequence": sequence,
            "scope": namespace,
            "kind": kind,
            "recorded_us": recorded_us,
            "recorded_at": datetime.fromtimestamp(recorded_us / 1_000_000, UTC).isoformat(),
            "revision": head.get("revision", 0),
            "engine": self.store.engine,
            "previous_hash": head.get("journal_hash"),
            "state_hash": digest(state),
            "details": details,
            "changes": changes or [],
        }
        # Baseline plus occasional checkpoints. Deltas don't duplicate unchanged
        # source text; historical reads never rerun an LLM.
        if sequence % 100 == 0:
            event["snapshot"] = state
        event_hash = digest(event)
        tx.run(
            "CREATE (e:MemoryChange {id:$id,namespace:$audit,scope:$ns,sequence:$seq,"
            "kind:$kind,recorded_at:$at,recorded_us:$us,payload:$payload,hash:$hash}) "
            "WITH e MATCH (s:MemorySpace {id:$ns}) SET s.journal_sequence=$seq,"
            "s.journal_hash=$hash,s.journal_us=$us,s.journal_state_hash=$state",
            id=digest(["journal", namespace, sequence]),
            audit="audit:" + namespace,
            ns=namespace,
            seq=sequence,
            kind=kind,
            at=event["recorded_at"],
            us=recorded_us,
            payload=json.dumps(event, sort_keys=True),
            hash=event_hash,
            state=event["state_hash"],
        ).consume()
        return event

    def initialize(self, namespace):
        def run(tx):
            self.store.lock(tx, namespace)
            head = self._head(tx, namespace)
            if "journal_sequence" not in head:
                state = capture(tx, namespace)
                self._append(
                    tx,
                    namespace,
                    "baseline",
                    {"preexisting_records": sum(map(len, state.values()))},
                    state,
                )
            return self._head(tx, namespace)

        head = self.store.transaction(run)
        return {
            "namespace": namespace,
            "sequence": head["journal_sequence"],
            "hash": head["journal_hash"],
        }

    def mutate(self, tx, namespace, kind, details, operation):
        self.store.lock(tx, namespace)
        head = self._head(tx, namespace)
        if head.get("replay_read_only"):
            raise ValueError(
                "Historical replay is read-only; choose an experimental namespace for new work"
            )
        before = capture(tx, namespace)
        if "journal_sequence" not in head:
            self._append(
                tx,
                namespace,
                "baseline",
                {"preexisting_records": sum(map(len, before.values()))},
                before,
            )
        elif digest(before) != head["journal_state_hash"]:
            raise ValueError(
                "Graph differs from its journal; investigate an untracked write before continuing"
            )
        result = operation(tx)
        after = capture(tx, namespace)
        changes = difference(before, after)
        if changes:
            self._append(tx, namespace, kind, details, after, changes)
        return result

    def events(self, namespace, after=-1, limit=100):
        if after < -1 or not 1 <= limit <= 1000:
            raise ValueError("after must be >= -1 and limit 1..1000")
        return self.store.transaction(
            lambda tx: tx.run(
                "MATCH (e:MemoryChange {scope:$ns}) WHERE e.sequence>$after "
                "RETURN e.sequence AS sequence,e.kind AS kind,e.recorded_at AS recorded_at,"
                "e.hash AS hash ORDER BY e.sequence LIMIT $limit",
                ns=namespace,
                after=after,
                limit=limit,
            ).data()
        )

    def snapshot(self, namespace, *, known_at=None, sequence=None, transaction=None):
        if known_at is not None and sequence is not None:
            raise ValueError("Choose known_at or at_change, not both")
        if sequence is not None and sequence < 0:
            raise ValueError("at_change must be nonnegative")
        if known_at is not None and known_at.utcoffset() is None:
            raise ValueError("known_at requires a timezone")

        def read(tx):
            self.store.lock(tx, namespace)
            rows = tx.run(
                "MATCH (e:MemoryChange {scope:$ns}) RETURN e.payload AS payload,e.hash AS hash "
                "ORDER BY e.sequence",
                ns=namespace,
            ).data()
            return rows, self._head(tx, namespace)

        rows, head = read(transaction) if transaction is not None else self.store.transaction(read)
        if not rows:
            raise ValueError("No journal exists for this namespace")
        cutoff = int(known_at.timestamp() * 1_000_000) if known_at else None
        events, previous = [], None
        for index, row in enumerate(rows):
            try:
                event = json.loads(row["payload"])
                required = {
                    "sequence",
                    "scope",
                    "previous_hash",
                    "recorded_us",
                    "recorded_at",
                    "state_hash",
                    "revision",
                    "changes",
                }
                if not isinstance(event, dict) or not required <= event.keys():
                    raise ValueError("Invalid journal event")
            except (ValueError, TypeError) as exc:
                raise ValueError("Journal integrity check failed") from exc
            if (
                event["sequence"] != index
                or event["scope"] != namespace
                or event["previous_hash"] != previous
                or digest(event) != row["hash"]
            ):
                raise ValueError("Journal integrity check failed")
            previous = row["hash"]
            events.append(event)
        if head.get("journal_hash") != previous or head.get("journal_sequence") != len(events) - 1:
            raise ValueError("Journal head does not match its history")
        selected = [
            e
            for e in events
            if (sequence is None or e["sequence"] <= sequence)
            and (cutoff is None or e["recorded_us"] <= cutoff)
        ]
        if not selected:
            raise ValueError(
                "Requested time predates journal coverage; earlier knowledge cannot be reconstructed"
            )
        if sequence is not None and selected[-1]["sequence"] != sequence:
            raise ValueError("Requested change does not exist")
        checkpoint = max(i for i, e in enumerate(selected) if "snapshot" in e)
        state = copy.deepcopy(selected[checkpoint]["snapshot"])
        for label in LABELS:
            state.setdefault(label, {})
        if digest(state) != selected[checkpoint]["state_hash"]:
            raise ValueError("Journal checkpoint integrity check failed")
        for event in selected[checkpoint + 1 :]:
            apply(state, event["changes"])
            if digest(state) != event["state_hash"]:
                raise ValueError("Journal replay integrity check failed")
        return {
            "state": state,
            "sequence": selected[-1]["sequence"],
            "revision": selected[-1]["revision"],
            "known_at": selected[-1]["recorded_at"],
            "coverage_started_at": events[0]["recorded_at"],
            "hash": digest(selected[-1]),
        }

    def verify(self, namespace):
        def run(tx):
            snapshot = self.snapshot(namespace, transaction=tx)
            state = capture(tx, namespace)
            if snapshot["state"] != state:
                raise ValueError("Live graph differs from journal reconstruction")
            return {
                "namespace": namespace,
                "verified": True,
                "sequence": snapshot["sequence"],
                "hash": snapshot["hash"],
                "records": sum(map(len, state.values())),
            }

        return self.store.transaction(run)

    def recall(
        self,
        namespace,
        query,
        as_of=None,
        limit=30,
        *,
        known_at=None,
        sequence=None,
        complete=False,
    ):
        snapshot = self.snapshot(namespace, known_at=known_at, sequence=sequence)
        state = snapshot["state"]
        at = as_of or datetime.fromisoformat(snapshot["known_at"])
        needle = normalized(query)
        candidates = [
            e
            for e in state["MemoryEntity"].values()
            if not e.get("merged_into")
            and (e["key"] == needle or any(needle in a for a in e["aliases"]))
        ]
        candidates.sort(key=lambda e: (0 if needle in e["aliases"] else 1, e["key"]))
        candidates = candidates[:21]
        exact = [e for e in candidates if needle in e["aliases"]]
        selected = exact or candidates
        ids = {e["id"] for e in selected}

        def grounded(f):
            s = state["MemoryEntity"].get(f["subject_id"])
            t = state["MemoryEntity"].get(f["target_id"])
            e = state["MemoryEpisode"].get(f["episode_id"])
            if s and t and e and e["status"] == "complete":
                return {
                    **f,
                    "subject_name": s["name"],
                    "subject_kind": s["kind"],
                    "target_name": t["name"],
                    "target_kind": t["kind"],
                }

        stored = [
            f
            for f in state["MemoryFact"].values()
            if f["subject_id"] in ids or f["target_id"] in ids
        ]
        facts = [g for f in stored if (g := grounded(f))]
        projection = project(facts, at)
        framework_ids = {
            f["target_id"] for f in projection["current"] if f["relation"] == "uses_framework"
        }
        related = [
            g
            for f in state["MemoryFact"].values()
            if f["subject_id"] in framework_ids
            and f["relation"] == "implemented_in"
            and (g := grounded(f))
        ]
        second = project(related, at)
        inferences = [
            {
                "subject": root["subject"],
                "relation": "uses_language",
                "target": neighbor["target"],
                "inferred": True,
                "rule": "uses_framework + implemented_in",
                "supporting_fact_ids": [root["id"], neighbor["id"]],
            }
            for root in projection["current"]
            for neighbor in second["current"]
            if root["relation"] == "uses_framework" and root["target_id"] == neighbor["subject_id"]
        ]
        current = {f["id"] for lane in ("current", "events") for f in projection[lane]}
        insights = [
            i
            for i in state["MemoryInsight"].values()
            if not i.get("retired")
            and ids.intersection(i["entity_ids"])
            and set(i["supporting_fact_ids"]) <= current
        ]
        counts = Counter(e["status"] for e in state["MemoryEpisode"].values())
        return {
            "query": query,
            "as_of": at.isoformat(),
            "revision": snapshot["revision"],
            "entities": selected[:20],
            "ambiguous": len(selected) > 1,
            "entity_matches_truncated": len(selected) > 20,
            **{k: v if complete else v[:limit] for k, v in projection.items()},
            "inferred": inferences[:limit],
            "insights": insights[:limit],
            "totals": {k: len(v) for k, v in projection.items()},
            "freshness": {
                "complete_episodes": counts.get("complete", 0),
                "pending_episodes": counts.get("pending", 0),
                "failed_episodes": 0,
                "excluded_ungrounded_facts": len(stored) - len(facts),
                "coverage": "historical knowledge; processing retries are not journaled",
            },
            "knowledge_history": {k: v for k, v in snapshot.items() if k != "state"},
        }

    def replay(self, namespace, target, *, known_at=None, sequence=None):
        if not target.startswith("replay:") or target == namespace or len(target) > 240:
            raise ValueError("Choose a new replay: namespace")
        snapshot = self.snapshot(namespace, known_at=known_at, sequence=sequence)
        state = snapshot["state"]
        mapping = {
            key: digest(["replay", target, key]) for nodes in state.values() for key in nodes
        }

        def run(tx):
            self.store.lock(tx, target)
            head = self._head(tx, target)
            occupied = tx.run(
                "MATCH (n) WHERE n.namespace=$ns OR n.scope=$ns RETURN count(n) AS count", ns=target
            ).single()["count"]
            if occupied or head.get("replay_origin") or head.get("journal_sequence") is not None:
                raise ValueError("Replay target already exists; it will not be overwritten")
            for label, nodes in state.items():
                for original in nodes.values():
                    props = {**original, "id": mapping[original["id"]], "namespace": target}
                    for key in (
                        "subject_id",
                        "target_id",
                        "episode_id",
                        "merged_into",
                        "dream_id",
                        "session_ref",
                        "message_ref",
                        "artifact_ref",
                    ):
                        if props.get(key) in mapping:
                            props[key] = mapping[props[key]]
                    for key in (
                        "entity_ids",
                        "supporting_fact_ids",
                        "message_refs",
                        "validation_message_refs",
                    ):
                        if key in props:
                            props[key] = [mapping.get(v, v) for v in props[key]]
                    # Dream snapshots retain their original evidence IDs for audit;
                    # replay graphs are read-only and cannot rerun these dreams.
                    tx.run(f"CREATE (n:{label}) SET n=$props", props=props).consume()
            tx.run(
                "MATCH (s:MemorySpace {id:$ns}) SET s.replay_read_only=true,s.replay_origin=$origin,"
                "s.replay_sequence=$sequence,s.revision=$revision",
                ns=target,
                origin=namespace,
                sequence=snapshot["sequence"],
                revision=snapshot["revision"],
            ).consume()
            self._append(
                tx,
                target,
                "baseline",
                {"replay_origin": namespace, "replay_sequence": snapshot["sequence"]},
                capture(tx, target),
            )
            self.store.repair(target, transaction=tx)
            return {
                "namespace": target,
                "source": namespace,
                "at_change": snapshot["sequence"],
                "read_only": True,
            }

        return self.store.transaction(run)
