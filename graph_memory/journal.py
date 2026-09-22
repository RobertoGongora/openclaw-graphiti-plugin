"""Append-only knowledge changes, historical reconstruction, and isolated replay."""

import hashlib
import json
import os
import zlib
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
    "infra_failures",
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

# Written once and never changed: source_graph.save() and store.stage() set them
# ON CREATE only, and save() refuses a message whose content differs. From event
# version 3 the journal stores their sha256 and reads the text back from the live
# node. MemoryEpisode.extraction_payload is not here: promoting a revision commits
# the same episode again with a new extraction, and history must keep the old one.
#
# A reference is only as good as the check made when it is resolved. Replaying
# events proves the chain and the state hashes, not that a live body is still the
# text that was journaled: an edit made outside the journal is found when the body
# is read through Bodies, and by verify() and verify_live(), which hash every
# live body.
IMMUTABLE = {
    "MemoryEpisode": ("payload",),
    "MemoryMessage": ("content",),
    "MemoryArtifactObservation": ("content",),
}
REF = "$ref"
INLINE = 128  # a shorter body costs less inline than its reference
PART_BYTES = 4 << 20  # uncompressed; bounds what a checkpoint read or write holds
CHECKPOINT_BYTES = 64 << 20

MODULUS = 1 << 256
INTEGRITY = "Journal integrity check failed"
CHECKPOINT = "Journal checkpoint integrity check failed"
REPLAY = "Journal replay integrity check failed"


def element_hash(label, props):
    return int(digest([label, props["id"], props]), 16)


def set_hash(state):
    """Order-independent state digest: a delta updates it without rereading the graph."""
    return sum(element_hash(label, p) for label in LABELS for p in state[label].values()) % MODULUS


def hexhash(value):
    return f"{value % MODULUS:064x}"


def body_hash(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def is_ref(value):
    return isinstance(value, dict) and REF in value


def referenced(label, props):
    """props with each write-once body replaced by {"$ref": sha256}; the same
    object when there is nothing to replace."""
    for key in IMMUTABLE.get(label, ()):
        value = props.get(key)
        if isinstance(value, str) and len(value) >= INLINE:
            props = {**props, key: {REF: body_hash(value)}}
    return props


def holds_ref(label, node):
    return any(is_ref(dict.get(node, key)) for key in IMMUTABLE.get(label, ()))


# Written in front of the head's state hash. An engine that predates references
# compares that field with its own idea of the state, finds a mismatch and stops,
# instead of appending changes this journal could not replay.
MISMATCH = "Graph differs from its journal; investigate an untracked write before continuing"
FENCE = "v3:"
CHECKPOINT_EVENTS = 2000


def migrated(head):
    """The head carries the per-node state hash (with or without the fence)."""
    current = head.get("journal_set_hash")
    return (
        "journal_sequence" in head
        and current is not None
        and head.get("journal_state_hash") in (current, FENCE + current)
    )


class Bodies:
    """Write-once bodies read back from the live graph and checked against their refs."""

    def __init__(self, store, namespace):
        self.store, self.namespace = store, namespace

    def resolved(self, tx, label, nodes, size=100):
        """Plain copies of reconstructed nodes with their bodies, a batch at a time."""
        keys = IMMUTABLE.get(label, ())
        batch = []
        for node in nodes:
            batch.append(node)
            if len(batch) == size:
                yield from self._batch(tx, label, keys, batch)
                batch = []
        yield from self._batch(tx, label, keys, batch)

    def _batch(self, tx, label, keys, batch):
        wanted = [dict.get(n, "id") for n in batch if holds_ref(label, n)]
        live = {}
        if wanted:
            fields = ",".join(f"n.{key} AS {key}" for key in keys)
            live = {
                row["id"]: row
                for row in tx.run(
                    f"MATCH (n:{label}) WHERE n.id IN $ids AND n.namespace=$ns "
                    f"RETURN n.id AS id,{fields}",
                    ids=wanted,
                    ns=self.namespace,
                )
            }
        for node in batch:
            props = dict(node)
            for key in keys:
                if is_ref(props.get(key)):
                    body = live.get(props["id"], {}).get(key)
                    if not isinstance(body, str) or body_hash(body) != props[key][REF]:
                        raise ValueError(INTEGRITY)
                    props[key] = body
            yield props

    def one(self, label, node, key):
        return self.store.read(
            lambda tx: next(
                self.resolved(tx, label, [{"id": dict.get(node, "id"), key: dict.get(node, key)}])
            )
        )[key]

    def plain(self, label, node):
        """The node with its bodies, for the rare reader that must hash it again."""
        return self.store.read(lambda tx: next(self.resolved(tx, label, [dict(node)])))


class Node(dict):
    """A reconstructed node. Reading a referenced body fetches and checks it, so
    callers written for embedded bodies keep working; items() and equality see
    the reference."""

    __slots__ = ("bodies", "label", "fetched")

    def __init__(self, label, bodies):
        super().__init__()
        self.label, self.bodies, self.fetched = label, bodies, {}

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if not is_ref(value):
            return value
        # Write-once, so a body fetched and checked once stays right for this node.
        if value[REF] not in self.fetched:
            self.fetched[value[REF]] = self.bodies.one(self.label, self, key)
        return self.fetched[value[REF]]

    def get(self, key, default=None):
        return self[key] if key in self else default


def flatten(state):
    for label in LABELS:
        for props in state[label].values():
            yield label, props


def elements(tx, namespace, ids=None):
    """Yield (label, journaled properties) for a namespace, or for {label: ids}."""
    for label in LABELS:
        if ids is None:
            rows = tx.run(
                f"MATCH (n:{label} {{namespace:$ns}}) "
                "RETURN labels(n) AS labels,properties(n) AS props",
                ns=namespace,
            )
        elif ids.get(label):
            rows = tx.run(
                f"MATCH (n:{label}) WHERE n.id IN $ids AND n.namespace=$ns "
                "RETURN labels(n) AS labels,properties(n) AS props",
                ids=sorted(ids[label]),
                ns=namespace,
            )
        else:
            continue
        for row in rows:
            # A node belongs to its first journaled label only.
            if next(name for name in LABELS if name in row["labels"]) != label:
                continue
            props = {k: v for k, v in row["props"].items() if k not in VOLATILE}
            # Operational retries/leases aren't changes to knowledge. A staged source
            # remains pending until a validated extraction commits.
            if label == "MemoryEpisode" and props["status"] != "complete":
                props["status"] = "pending"
            if label == "MemoryDream" and props["status"] not in ("completed", "applied"):
                props["status"] = "pending"
            yield label, props


def capture(tx, namespace, ids=None):
    state = {label: {} for label in LABELS}
    for label, props in elements(tx, namespace, ids):
        state[label][props["id"]] = props
    return state


def live_hash(tx, namespace):
    """The state hash of the live graph, one node at a time: holding a large
    namespace in memory only to add up its hashes costs gigabytes."""
    return hexhash(sum(element_hash(label, props) for label, props in elements(tx, namespace)))


def audited(sequence, changed):
    """MEMORY_JOURNAL_AUDIT=1 checks every scoped write against the whole graph;
    N>1 checks the writes whose sequence is a multiple of N. A sampled audit skips
    writes that changed nothing: a run of them would otherwise repeat the audit."""
    try:
        every = int(os.environ.get("MEMORY_JOURNAL_AUDIT", "0"))
    except ValueError:
        return False
    return every == 1 or (every > 1 and changed and sequence % every == 0)


class Scope:
    """Before-images of the nodes one journaled write declares it will change."""

    def __init__(self, tx, namespace):
        self.tx, self.namespace = tx, namespace
        self.ids = {label: set() for label in LABELS}
        self.before = {label: {} for label in LABELS}

    def touch(self, label, ids):
        new = set(ids) - self.ids[label]
        if new:
            self.before[label].update(capture(self.tx, self.namespace, {label: new})[label])
            self.ids[label] |= new


def difference(before, after):
    changes = []
    for label in LABELS:
        for key in sorted(before[label].keys() | after[label].keys()):
            old, new = before[label].get(key), after[label].get(key)
            if old == new:
                continue
            change = {
                "label": label,
                "id": key,
                "deleted": new is None,
                "set": referenced(
                    label,
                    {k: v for k, v in (new or {}).items() if old is None or old.get(k) != v},
                ),
                "unset": sorted((old or {}).keys() - (new or {}).keys()),
            }
            # The node's hash after the change, taken here where its bodies are in
            # hand: replay keeps the running state hash without reading them.
            if new is not None:
                change["hash"] = hexhash(element_hash(label, new))
                shape = referenced(label, new)
                if shape != new:
                    # The hash above cannot be recomputed without the bodies. This one
                    # can, so replay still proves it rebuilt the node the writer saw.
                    change["shape"] = digest([label, key, shape])
            changes.append(change)
    return changes


def apply(state, changes, bodies=None):
    for change in changes:
        label = change["label"]
        bucket = state[label]
        if change["deleted"]:
            bucket.pop(change["id"], None)
        else:
            node = bucket.get(change["id"])
            if node is None:
                node = bucket[change["id"]] = Node(label, bodies) if label in IMMUTABLE else {}
            node.update(change["set"])
            for key in change["unset"]:
                node.pop(key, None)


def advance(state, sealed, running, event, bodies=None):
    """Apply one delta and check its state hash; returns the running hash sum.
    sealed holds the hash of every node that carries a reference, which cannot be
    hashed again without its body."""
    if event.get("version", 1) < 2:
        apply(state, event["changes"], bodies)
        if digest(state) != event["state_hash"]:
            raise ValueError(REPLAY)
        return None
    if running is None:
        running = set_hash(state)
    for change in event["changes"]:
        label, key = change["label"], change["id"]
        if key in state[label]:
            old = sealed.pop((label, key), None)
            running -= element_hash(label, state[label][key]) if old is None else old
    apply(state, event["changes"], bodies)
    for change in event["changes"]:
        label, key = change["label"], change["id"]
        if change["deleted"]:
            continue
        node = state[label][key]
        if holds_ref(label, node):
            # Every engine that writes references also writes the shape; only a change
            # from an older engine, on a node that gained a reference later, lacks it.
            if "shape" not in change and event.get("version", 1) >= 3:
                raise ValueError(REPLAY)
            if "shape" in change and digest([label, key, dict(node)]) != change["shape"]:
                raise ValueError(REPLAY)
            if "hash" in change:
                new = int(change["hash"], 16)
            elif bodies is not None:
                # Written by an engine that knew no references: hash it the long way.
                new = element_hash(label, bodies.plain(label, node))
            else:
                raise ValueError(REPLAY)
            sealed[(label, key)] = new
        else:
            new = element_hash(label, node)
            if change.get("hash", hexhash(new)) != hexhash(new):
                raise ValueError(REPLAY)
        running += new
    if hexhash(running) != event["state_hash"]:
        raise ValueError(REPLAY)
    return running


class Journal:
    def __init__(self, store):
        self.store = store

    def _head(self, tx, namespace):
        return tx.run(
            "MATCH (s:MemorySpace {id:$ns}) RETURN properties(s) AS s", ns=namespace
        ).single()["s"]

    def _append(self, tx, namespace, kind, details, state_hash, changes=None, parts=None):
        head = self._head(tx, namespace)
        sequence = head.get("journal_sequence", -1) + 1
        recorded_us = max(int(now().timestamp() * 1_000_000), head.get("journal_us", 0) + 1)
        event = {
            "version": 3,
            "sequence": sequence,
            "scope": namespace,
            "kind": kind,
            "recorded_us": recorded_us,
            "recorded_at": datetime.fromtimestamp(recorded_us / 1_000_000, UTC).isoformat(),
            "revision": head.get("revision", 0),
            "engine": self.store.engine,
            "previous_hash": head.get("journal_hash"),
            "state_hash": state_hash,
            "details": details,
            "changes": changes or [],
        }
        # Only a baseline or a checkpoint carries state, as separate part nodes. Their
        # hashes are listed here, so the chain hash covers them.
        if parts is not None:
            event["parts"] = parts
        event_hash = digest(event)
        payload = json.dumps(event, sort_keys=True)
        tx.run(
            "CREATE (e:MemoryChange {id:$id,namespace:$audit,scope:$ns,sequence:$seq,"
            "kind:$kind,recorded_at:$at,recorded_us:$us,payload:$payload,hash:$hash,"
            "checkpoint:$checkpoint}) "
            "WITH e MATCH (s:MemorySpace {id:$ns}) SET s.journal_sequence=$seq,"
            "s.journal_hash=$hash,s.journal_us=$us,s.journal_state_hash=$fence+$state,"
            "s.journal_set_hash=$state,s.journal_checkpoint_sequence="
            "CASE WHEN $checkpoint THEN $seq ELSE s.journal_checkpoint_sequence END,"
            "s.journal_checkpoint_bytes="
            "CASE WHEN $checkpoint THEN $size ELSE s.journal_checkpoint_bytes END,"
            "s.journal_delta_bytes="
            "CASE WHEN $checkpoint THEN 0 ELSE coalesce(s.journal_delta_bytes,0)+$size END",
            id=digest(["journal", namespace, sequence]),
            audit="audit:" + namespace,
            ns=namespace,
            fence=FENCE,
            seq=sequence,
            kind=kind,
            at=event["recorded_at"],
            us=recorded_us,
            payload=payload,
            hash=event_hash,
            state=event["state_hash"],
            checkpoint=parts is not None,
            size=sum(p["bytes"] for p in parts) if parts is not None else len(payload),
        ).consume()
        if parts:
            tx.run(
                "MATCH (e:MemoryChange {id:$id}),(p:MemorySnapshotPart {scope:$ns,sequence:$seq}) "
                "CREATE (e)-[:PART {i:p.i}]->(p)",
                id=digest(["journal", namespace, sequence]),
                ns=namespace,
                seq=sequence,
            ).consume()
        return event

    def _parts(self, tx, namespace, nodes):
        """Write (label, props) pairs as the parts of the next event, one part in
        memory at a time. Returns the part list, the state hash sum and the count."""
        sequence = self._head(tx, namespace).get("journal_sequence", -1) + 1
        # Parts past the head belong to no event: a journal that was removed by hand
        # leaves them behind, and they would be linked to the new event.
        tx.run(
            "MATCH (p:MemorySnapshotPart {scope:$ns}) WHERE p.sequence>=$seq DETACH DELETE p",
            ns=namespace,
            seq=sequence,
        ).consume()
        parts, lines = [], []
        total = records = size = 0
        current = None

        def flush():
            raw = "\n".join(lines).encode()
            data = zlib.compress(raw)
            tx.run(
                # namespace lets a namespace-wide delete take the parts with it. No
                # journaled label is involved, so capture never sees them.
                "CREATE (:MemorySnapshotPart {id:$id,namespace:$ns,scope:$ns,sequence:$seq,"
                "i:$i,label:$label,data:$data})",
                id=digest(["journal-part", namespace, sequence, len(parts)]),
                ns=namespace,
                seq=sequence,
                i=len(parts),
                label=current,
                data=data,
            ).consume()
            parts.append(
                {
                    "label": current,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(raw),
                    "records": len(lines),
                }
            )

        for label, props in nodes:
            value = element_hash(label, props)
            slim = referenced(label, props)
            line = json.dumps(
                {"p": slim, "h": hexhash(value)} if slim is not props else {"p": slim},
                sort_keys=True,
            )
            if lines and (label != current or size + len(line) > PART_BYTES):
                flush()
                lines, size = [], 0
            current = label
            lines.append(line)
            size += len(line) + 1
            total += value
            records += 1
        if lines:
            flush()
        return parts, total, records

    def _baseline(self, tx, namespace, details, nodes):
        parts, total, records = self._parts(tx, namespace, nodes)
        return self._append(
            tx,
            namespace,
            "baseline",
            {**details, "preexisting_records": records},
            hexhash(total),
            parts=parts,
        )

    def _restore(self, tx, namespace, event, bodies):
        """The state a checkpoint event carries, the hashes of its nodes that hold
        references, and its hash sum (None for the whole-state digest of version 1)."""
        state = {label: {} for label in LABELS}
        sealed = {}
        if "parts" not in event:
            if "snapshot" not in event:
                raise ValueError(CHECKPOINT)
            state.update(event["snapshot"])
            if event.get("version", 1) < 2:
                if digest(state) != event["state_hash"]:
                    raise ValueError(CHECKPOINT)
                return state, sealed, None
            total = set_hash(state)
        else:
            total = 0
            for i, part in enumerate(event["parts"]):
                row = tx.run(
                    "MATCH (:MemoryChange {id:$id})-[:PART {i:$i}]->(p:MemorySnapshotPart) "
                    "RETURN p.data AS data",
                    id=digest(["journal", namespace, event["sequence"]]),
                    i=i,
                ).single()
                try:
                    if not row or hashlib.sha256(row["data"]).hexdigest() != part["sha256"]:
                        raise ValueError("Invalid snapshot part")
                    label = part["label"]
                    inflater = zlib.decompressobj()
                    raw = inflater.decompress(row["data"], part["bytes"] + 1)
                    if len(raw) != part["bytes"] or inflater.unconsumed_tail:
                        raise ValueError("Snapshot part size differs from its event")
                    lines = raw.decode().split("\n") if raw else []
                    if len(lines) != part["records"]:
                        raise ValueError("Snapshot part count differs from its event")
                    for line in lines:
                        record = json.loads(line)
                        node = Node(label, bodies) if "h" in record else {}
                        node.update(record["p"])
                        state[label][node["id"]] = node
                        if "h" in record:
                            total += int(record["h"], 16)
                            sealed[(label, node["id"])] = int(record["h"], 16)
                        else:
                            total += element_hash(label, node)
                except (ValueError, TypeError, KeyError, zlib.error) as exc:
                    raise ValueError(CHECKPOINT) from exc
        if hexhash(total) != event["state_hash"]:
            raise ValueError(CHECKPOINT)
        return state, sealed, total

    def initialize(self, namespace):
        def run(tx):
            self.store.lock(tx, namespace)
            head = self._head(tx, namespace)
            if "journal_sequence" not in head:
                self._baseline(tx, namespace, {}, elements(tx, namespace))
            return self._head(tx, namespace)

        head = self.store.transaction(run)
        return {
            "namespace": namespace,
            "sequence": head["journal_sequence"],
            "hash": head["journal_hash"],
        }

    def mutate(self, tx, namespace, kind, details, operation, scoped=False):
        """Journal one write. A scoped operation declares the nodes it changes with
        store.touch() before writing them, so the cost follows the change and not
        the graph; every other operation is diffed against a full capture."""
        self.store.lock(tx, namespace)
        head = self._head(tx, namespace)
        if head.get("replay_read_only"):
            raise ValueError(
                "Historical replay is read-only; choose an experimental namespace for new work"
            )
        mismatch = (
            "Graph differs from its journal; investigate an untracked write before continuing"
        )
        current = migrated(head)
        if scoped and current:
            scope = Scope(tx, namespace)
            scopes = self.store.journal_scopes()
            scopes[(id(tx), namespace)] = scope
            try:
                result = operation(tx)
            finally:
                del scopes[(id(tx), namespace)]
            after = capture(tx, namespace, scope.ids)
            changes = difference(scope.before, after)
            state_hash = hexhash(
                int(head["journal_set_hash"], 16) - set_hash(scope.before) + set_hash(after)
            )
            if changes:
                self._append(tx, namespace, kind, details, state_hash, changes)
            # Untracked writes are found by verify() and sampled audits, not on
            # every scoped write.
            if audited(head["journal_sequence"] + 1, bool(changes)) and state_hash != live_hash(
                tx, namespace
            ):
                raise ValueError(mismatch)
            return result
        before = capture(tx, namespace)
        if "journal_sequence" not in head:
            self._baseline(tx, namespace, {}, flatten(before))
        elif not current:
            # Journals written before the set hash carry a digest of the whole state.
            # Both head hashes move together, so a process still running the older
            # code stops on its own state check and cannot append to this journal.
            if digest(before) != head["journal_state_hash"]:
                raise ValueError(mismatch)
            tx.run(
                "MATCH (s:MemorySpace {id:$ns}) "
                "SET s.journal_set_hash=$hash,s.journal_state_hash=$fence+$hash",
                fence=FENCE,
                ns=namespace,
                hash=hexhash(set_hash(before)),
            ).consume()
        elif hexhash(set_hash(before)) != head["journal_set_hash"]:
            raise ValueError(mismatch)
        result = operation(tx)
        after = capture(tx, namespace)
        changes = difference(before, after)
        if changes:
            self._append(tx, namespace, kind, details, hexhash(set_hash(after)), changes)
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

    REQUIRED = {
        "sequence",
        "scope",
        "previous_hash",
        "recorded_us",
        "recorded_at",
        "state_hash",
        "revision",
        "changes",
    }

    def _events(self, tx, namespace, first, last, previous):
        """Hash-checked (event, hash) pairs first..last, a batch in memory at a time;
        previous is the hash before first."""
        count = 0
        for low in range(first, last + 1, 25):
            rows = tx.run(
                "MATCH (e:MemoryChange {scope:$ns}) WHERE e.sequence>=$low AND e.sequence<=$high "
                "RETURN e.payload AS payload,e.hash AS hash ORDER BY e.sequence",
                ns=namespace,
                low=low,
                high=min(low + 24, last),
            ).data()
            for row in rows:
                try:
                    event = json.loads(row["payload"])
                    if not isinstance(event, dict) or not self.REQUIRED <= event.keys():
                        raise ValueError("Invalid journal event")
                except (ValueError, TypeError) as exc:
                    raise ValueError(INTEGRITY) from exc
                if (
                    event["sequence"] != first + count
                    or event["scope"] != namespace
                    or event["previous_hash"] != previous
                    or digest(event) != row["hash"]
                ):
                    raise ValueError(INTEGRITY)
                previous = row["hash"]
                count += 1
                yield event, previous
        if count != last - first + 1:
            raise ValueError(INTEGRITY)

    def snapshot(self, namespace, *, known_at=None, sequence=None, transaction=None):
        if known_at is not None and sequence is not None:
            raise ValueError("Choose known_at or at_change, not both")
        if sequence is not None and sequence < 0:
            raise ValueError("at_change must be nonnegative")
        if known_at is not None and known_at.utcoffset() is None:
            raise ValueError("known_at requires a timezone")
        cutoff = int(known_at.timestamp() * 1_000_000) if known_at else None

        def read(tx):
            # The journal is append-only, so events up to the head read here are
            # stable without the namespace write lock.
            head = self._head_or_none(tx, namespace)
            if not head or "journal_sequence" not in head:
                raise ValueError("No journal exists for this namespace")
            target = head["journal_sequence"]
            if sequence is not None:
                if sequence > target:
                    raise ValueError("Requested change does not exist")
                target = sequence
            if cutoff is not None:
                target = tx.run(
                    "MATCH (e:MemoryChange {scope:$ns}) WHERE e.recorded_us<=$cutoff "
                    "RETURN max(e.sequence) AS sequence",
                    ns=namespace,
                    cutoff=cutoff,
                ).single()["sequence"]
                if target is None:
                    raise ValueError(
                        "Requested time predates journal coverage; earlier knowledge cannot be reconstructed"
                    )
            # Older journals checkpointed every 100th event without marking the node.
            start = tx.run(
                "MATCH (e:MemoryChange {scope:$ns}) WHERE e.sequence<=$target AND "
                "(e.checkpoint=true OR (e.checkpoint IS NULL AND e.sequence % 100 = 0)) "
                "RETURN max(e.sequence) AS sequence",
                ns=namespace,
                target=target,
            ).single()["sequence"]
            edges = {
                row["sequence"]: row
                for row in tx.run(
                    "MATCH (e:MemoryChange {scope:$ns}) WHERE e.sequence IN $wanted "
                    "RETURN e.sequence AS sequence,e.hash AS hash,e.recorded_at AS recorded_at",
                    ns=namespace,
                    wanted=[0, (start or 0) - 1],
                ).data()
            }
            if start is None or 0 not in edges or (start and start - 1 not in edges):
                raise ValueError(INTEGRITY)
            previous = edges[start - 1]["hash"] if start else None
            # One event at a time: a long run of deltas never sits in memory, and
            # the checkpoint's state is changed in place.
            bodies = Bodies(self.store, namespace)
            events = self._events(tx, namespace, start, target, previous)
            event, tip = next(events)
            state, sealed, running = self._restore(tx, namespace, event, bodies)
            for following in events:
                event, tip = following
                running = advance(state, sealed, running, event, bodies)
            if target == head["journal_sequence"] and tip != head["journal_hash"]:
                raise ValueError("Journal head does not match its history")
            return {
                "state": state,
                "sequence": event["sequence"],
                "revision": event["revision"],
                "known_at": event["recorded_at"],
                "coverage_started_at": edges[0]["recorded_at"],
                "hash": tip,
            }

        return read(transaction) if transaction is not None else self.store.transaction(read)

    def resolve(self, namespace, state, transaction=None):
        """A reconstructed state with every referenced body read back and checked.
        Holds all of them at once: for a bounded state, not a large namespace."""

        def run(tx):
            bodies = Bodies(self.store, namespace)
            return {
                label: {n["id"]: n for n in bodies.resolved(tx, label, state[label].values())}
                for label in LABELS
            }

        return run(transaction) if transaction is not None else self.store.transaction(run)

    def checkpoint_due(self, namespace, threshold=CHECKPOINT_BYTES):
        """Whether enough change has piled up since the last checkpoint: by size, or
        by count so that a historical read never replays more than a few thousand
        events. A checkpoint reads the namespace under its lock, so the write path
        never asks; the daemon does, between scans."""
        head = self.store.read(lambda tx: self._head_or_none(tx, namespace)) or {}
        if "journal_sequence" not in head:
            return False
        events = head["journal_sequence"] - head.get("journal_checkpoint_sequence", 0)
        return events >= CHECKPOINT_EVENTS or head.get("journal_delta_bytes", 0) >= max(
            threshold, head.get("journal_checkpoint_bytes") or 0
        )

    def verify_live(self, namespace):
        """The live graph against the journal head, streamed: seconds and constant
        memory, where verify() reads the whole history. Finds untracked writes."""

        def run(tx):
            self.store.lock(tx, namespace)
            head = self._head(tx, namespace)
            if not migrated(head):
                raise ValueError(
                    "Journal predates the streamed state hash; write once or checkpoint"
                )
            if live_hash(tx, namespace) != head["journal_set_hash"]:
                raise ValueError("Live graph differs from journal reconstruction")
            return {"namespace": namespace, "verified": True, "sequence": head["journal_sequence"]}

        return self.store.transaction(run)

    def checkpoint(self, namespace, accept_live=False):
        """Record the current state as parts so historical reads replay from here.

        Streams the whole namespace under its lock: a maintenance action. With
        accept_live, a graph that no longer matches its journal is recorded as the
        new truth, which is the only way forward after an untracked write."""

        def run(tx):
            self.store.lock(tx, namespace)
            head = self._head(tx, namespace)
            if "journal_sequence" not in head:
                raise ValueError("No journal exists for this namespace")
            if migrated(head):
                # Compared before anything is written: a mismatch must cost one read of
                # the namespace, not a full set of parts that is then rolled back.
                matches = live_hash(tx, namespace) == head["journal_set_hash"]
                if not matches and not accept_live:
                    raise ValueError(MISMATCH)
                parts, total, records = self._parts(tx, namespace, elements(tx, namespace))
            else:
                # A journal from before the per-node hash digests the state as a whole.
                state = capture(tx, namespace)
                matches = digest(state) == head["journal_state_hash"]
                parts, total, records = self._parts(tx, namespace, flatten(state))
            # Raising rolls the parts back with the transaction.
            if not matches and not accept_live:
                raise ValueError(MISMATCH)
            event = self._append(
                tx,
                namespace,
                "checkpoint",
                {"accepted_untracked_state": not matches},
                hexhash(total),
                parts=parts,
            )
            return {
                "namespace": namespace,
                "sequence": event["sequence"],
                "records": records,
                "accepted_untracked_state": not matches,
            }

        return self.store.transaction(run)

    def _head_or_none(self, tx, namespace):
        row = tx.run(
            "MATCH (s:MemorySpace {id:$ns}) RETURN properties(s) AS s", ns=namespace
        ).single()
        return row["s"] if row else None

    def verify(self, namespace):
        """Full audit: the whole hash chain, then the live graph against its journal.

        Holds the namespace lock and reads every event, so it pauses writers on a
        large namespace. This is where an untracked write is detected."""

        def run(tx):
            self.store.lock(tx, namespace)
            head = self._head(tx, namespace)
            if "journal_sequence" not in head:
                raise ValueError("No journal exists for this namespace")
            final = None
            for pair in self._events(tx, namespace, 0, head["journal_sequence"], None):
                final = pair[1]
            if final != head["journal_hash"]:
                raise ValueError("Journal head does not match its history")
            snapshot = self.snapshot(namespace, transaction=tx)
            state = snapshot["state"]
            # The live graph a node at a time against the reconstruction. A
            # referenced body is hashed here, which is where an edit to one shows.
            total = records = 0
            for label, props in elements(tx, namespace):
                expected = state[label].get(props["id"])
                if expected is None or expected.keys() != props.keys():
                    raise ValueError("Live graph differs from journal reconstruction")
                for key, value in dict.items(expected):
                    live = props[key]
                    if (
                        body_hash(live) != value[REF]
                        if is_ref(value) and isinstance(live, str)
                        else live != value
                    ):
                        raise ValueError("Live graph differs from journal reconstruction")
                total += element_hash(label, props)
                records += 1
            if records != sum(map(len, state.values())) or head.get(
                "journal_set_hash", hexhash(total)
            ) != hexhash(total):
                raise ValueError("Live graph differs from journal reconstruction")
            return {
                "namespace": namespace,
                "verified": True,
                "sequence": snapshot["sequence"],
                "hash": snapshot["hash"],
                "records": records,
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
            from .recall_provenance import latest_report_time

            s = state["MemoryEntity"].get(f["subject_id"])
            t = state["MemoryEntity"].get(f["target_id"])
            e = state["MemoryEpisode"].get(f["episode_id"])
            if s and t and e and e["status"] == "complete":
                return {
                    **f,
                    "reported_at": f.get("reported_at")
                    or latest_report_time(
                        state["MemoryMessage"][mid]["timestamp"]
                        for mid in f.get("message_refs", [])
                        if state["MemoryMessage"].get(mid, {}).get("timestamp")
                    ),
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
            bodies = Bodies(self.store, namespace)
            for label, nodes in state.items():
                # A replay is a graph of its own, so it gets the real bodies.
                for original in bodies.resolved(tx, label, nodes.values()):
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
            self._baseline(
                tx,
                target,
                {"replay_origin": namespace, "replay_sequence": snapshot["sequence"]},
                elements(tx, target),
            )
            self.store.repair(target, transaction=tx)
            return {
                "namespace": target,
                "source": namespace,
                "at_change": snapshot["sequence"],
                "read_only": True,
            }

        return self.store.transaction(run)
