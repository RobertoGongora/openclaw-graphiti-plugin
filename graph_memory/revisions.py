"""Overlay revisions: dry-run extractions, explicit diffs, one scoped atomic promotion."""

import json
import uuid

from .mcp import READ_ONLY
from .models import DreamRequest, EpisodeRequest, Extraction, Transcript, now
from .store import GraphStore, digest
from .version import engine_fingerprint

FORMAT = "overlay"
LEGACY = (
    "Revision predates overlay revisions (it holds a namespace snapshot and a candidate "
    "namespace); it can only be read. Create a new revision"
)
SUPERSEDED = "Superseded by validated engine revision"
SIGNATURE = (
    "subject",
    "relation",
    "target",
    "status",
    "slot",
    "valid_at",
    "time_basis",
    "documented_at",
    "evidence",
    "summary",
    "confidence",
    "retracted",
)
CONFIRMATION = (
    "confirmed_at",
    "confirmed_by",
    "confirmation_note",
    "confirmed_valid_at",
    "confirmed_valid_ts",
)


# memory_render opens its own session, so it would read the graph without the preview.
CHECK_TOOLS = READ_ONLY - {"memory_render"}


class Unbuildable(ValueError):
    """A build failure that building again cannot cure."""


class _Rollback(Exception):
    def __init__(self, value):
        self.value = value


class _Pinned(GraphStore):
    """The store as seen from inside one open transaction, so ordinary tools can
    read a promotion that is never committed."""

    def __init__(self, store, tx):
        self.__dict__.update(store.__dict__)
        self._tx = tx

    def read(self, fn, *args):
        return fn(self._tx, *args)

    def transaction(self, fn, *args):
        return fn(self._tx, *args)

    def close(self):
        pass


def signature(fact):
    return {"id": fact["id"], **{k: fact.get(k) for k in SIGNATURE}}


def identity(fact):
    return (fact["subject_id"], fact["relation"], fact["target_id"], fact.get("slot"))


def manual(fact):
    return bool(fact.get("retracted")) and not fact.get("replaced_by_revision")


def carry_over(before, after):
    """Which new fact takes each superseded confirmation, and why the rest are lost.
    A person confirmed a doubt: only a candidate that still doubts the same claim can
    take it, and each takes one, the most recent first."""
    new = {f["id"] for f in after}
    waiting, doubted = {}, set()
    for fact in after:
        if fact["status"] == "uncertain":
            doubted.add(identity(fact))
            if not fact.get(CONFIRMATION[0]):
                waiting.setdefault(identity(fact), []).append(fact["id"])
    claimed = {identity(f) for f in after}
    carried, lost = {}, {}
    for fact in sorted(
        (
            f
            for f in before
            if f.get(CONFIRMATION[0]) and not f.get("retracted") and f["id"] not in new
        ),
        key=lambda f: (f[CONFIRMATION[0]], f["id"]),
        reverse=True,
    ):
        key = identity(fact)
        if waiting.get(key):
            carried[fact["id"]] = waiting[key].pop(0)
        elif key not in claimed:
            lost[fact["id"]] = "no candidate makes this claim"
        elif key not in doubted:
            lost[fact["id"]] = (
                "the matching candidate is not uncertain, so it takes no confirmation"
            )
        else:
            lost[fact["id"]] = "another confirmation of the same claim was carried over"
    return carried, lost


def compare(before, after, lost):
    live = {f["id"]: f for f in before if not f.get("retracted")}
    new = {f["id"]: f for f in after}
    same = sorted(live.keys() & new.keys())
    changed = [
        {"before": signature(live[k]), "after": signature(new[k])}
        for k in same
        if signature(live[k]) != signature(new[k])
    ]
    unchanged = [k for k in same if signature(live[k]) == signature(new[k])]
    fresh = {}
    for fid in sorted(new.keys() - live.keys()):
        fresh.setdefault(identity(new[fid]), []).append(fid)
    removed = []
    for fid in sorted(live.keys() - new.keys()):
        match = fresh.get(identity(live[fid]))
        if match:
            changed.append({"before": signature(live[fid]), "after": signature(new[match.pop(0)])})
        else:
            removed.append(signature(live[fid]))
    added = [signature(new[fid]) for ids in fresh.values() for fid in ids]
    identities = {identity(f) for f in after}
    dropped, revived = [], []
    for fact in sorted(before, key=lambda f: f["id"]):
        confirmation = {k: fact.get(k) for k in CONFIRMATION} if fact.get(CONFIRMATION[0]) else None
        if fact["id"] in lost:
            dropped.append(
                {
                    "fact_id": fact["id"],
                    "decision": "confirmed",
                    "reason": lost[fact["id"]],
                    "detail": confirmation,
                    "fact": signature(fact),
                }
            )
        elif manual(fact) and (fact["id"] in new or identity(fact) in identities):
            dropped.append(
                {
                    "fact_id": fact["id"],
                    "decision": "retracted",
                    "reason": "the candidate brings this claim back",
                    "detail": {
                        "retraction_reason": fact.get("retraction_reason"),
                        "retracted_at": fact.get("retracted_at"),
                    },
                    "fact": signature(fact),
                }
            )
        if fact.get("retracted") and fact["id"] in new:
            # The same node goes live again, so say what comes back with it. A person
            # who retracted a claim withdrew their confirmation of it too.
            revived.append(
                {
                    "fact_id": fact["id"],
                    "retracted_by": "person" if manual(fact) else "revision",
                    "retraction_reason": fact.get("retraction_reason"),
                    "replaced_by_revision": fact.get("replaced_by_revision"),
                    "confirmation": confirmation,
                    "confirmation_kept": bool(confirmation) and not manual(fact),
                }
            )
    return {
        "added": sorted(added, key=lambda f: f["id"]),
        "removed": removed,
        "changed": sorted(changed, key=lambda c: c["after"]["id"]),
        # Ids only: their claims and evidence are on the live facts already.
        "unchanged": unchanged,
        "unchanged_count": len(unchanged),
        "revived": revived,
        "dropped_decisions": dropped,
        # What the live side looked like: a later human or engine change to these
        # facts must invalidate a validation even where the claims read the same.
        "live_digest": digest(
            sorted(
                [
                    f["id"],
                    bool(f.get("retracted")),
                    f.get("retraction_reason"),
                    f.get(CONFIRMATION[0]),
                ]
                for f in before
            )
        ),
    }


def behavior_changed(diff):
    return any(
        diff[k] for k in ("added", "removed", "changed", "dropped_decisions", "affected_dreams")
    )


class Revisions:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def get(self, namespace, revision_id):
        def run(tx):
            row = tx.run(
                # A legacy snapshot is hundreds of megabytes; it never leaves the database.
                "MATCH (r:MemoryRevision {id:$id,namespace:$ns}) "
                "RETURN r {.*,snapshot:null} AS r,"
                "[(r)-[:CANDIDATE]->(c) | c {.episode_id,.live_revision,.created_at}] AS built",
                id=revision_id,
                ns=namespace,
            ).single()
            if not row:
                raise ValueError("Revision not found in namespace")
            r = {k: v for k, v in row["r"].items() if k != "snapshot"}
            for key in ("episode_map", "dream_map", "diff", "validation", "checks", "build_error"):
                if r.get(key):
                    r[key] = json.loads(r[key])
            r["legacy"] = r.get("format") != FORMAT
            r["candidates"] = {c["episode_id"]: c for c in row["built"]}
            r["built_episodes"] = sorted(r["candidates"])
            return r

        return self.store.read(run)

    def _current(self, namespace, revision_id):
        revision = self.get(namespace, revision_id)
        if revision["legacy"]:
            raise ValueError(LEGACY)
        # Every step of a revision still in progress passes here: what one engine and
        # model extracted is never compared, checked or published as another's.
        if revision["status"] != "promoted":
            if revision["engine"] != engine_fingerprint():
                raise ValueError("Engine changed; create a new revision")
            if (revision["model"], revision.get("effort")) != self._model():
                raise ValueError("Model settings changed; create a new revision")
        return revision

    def _model(self):
        return getattr(self.service.llm, "model", "caller"), getattr(
            self.service.llm, "effort", None
        )

    def create(self, namespace, episode_ids, reason=None):
        episode_ids = list(dict.fromkeys(episode_ids or []))
        if not episode_ids or len(episode_ids) > 100:
            raise ValueError("Select 1..100 source episodes per revision")
        revision_id = str(uuid.uuid4())
        model, effort = self._model()

        def run(tx):
            revision = self.store.lock(tx, namespace)
            complete = tx.run(
                "MATCH (e:MemoryEpisode) WHERE e.id IN $ids AND e.namespace=$ns "
                "AND e.status='complete' RETURN count(e) AS count",
                ids=episode_ids,
                ns=namespace,
            ).single()["count"]
            if complete != len(episode_ids):
                raise ValueError("Only complete episodes can be replayed")
            dreams = self._affected(
                tx, namespace, [f["id"] for f in self._facts(tx, namespace, episode_ids)]
            )
            tx.run(
                "CREATE (r:MemoryRevision {id:$id,namespace:$ns,format:$format,status:'created',"
                "base_revision:$revision,engine:$engine,model:$model,effort:$effort,"
                "episode_ids:$episodes,affected_dreams:$dreams,reason:$reason,created_at:$at})",
                id=revision_id,
                ns=namespace,
                format=FORMAT,
                revision=revision,
                engine=engine_fingerprint(),
                model=model,
                effort=effort,
                episodes=episode_ids,
                dreams=dreams,
                reason=reason,
                at=now().isoformat(),
            ).consume()
            return {
                "revision_id": revision_id,
                # No candidate namespace exists; the key stays for older callers.
                "candidate": None,
                "affected_dreams": dreams,
                "base_revision": revision,
                "status": "created",
            }

        return self.store.transaction(run)

    @staticmethod
    def _affected(tx, namespace, fact_ids):
        """The dreams in force that cite any of these facts."""
        return [
            r["id"]
            for r in tx.run(
                "MATCH (d:MemoryDream {namespace:$ns}) WHERE d.status IN ['completed','applied'] "
                "AND coalesce(d.review_status,'')<>'superseded' "
                "AND any(fid IN $facts WHERE d.output CONTAINS fid) "
                "RETURN d.id AS id ORDER BY id",
                ns=namespace,
                facts=fact_ids,
            ).data()
        ]

    @staticmethod
    def _facts(tx, namespace, episode_ids, ids=None):
        return [
            r["f"]
            for r in tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) WHERE "
                + ("f.id IN $ids " if ids is not None else "f.episode_id IN $episodes ")
                + "RETURN properties(f) AS f ORDER BY f.id",
                ns=namespace,
                episodes=episode_ids,
                ids=ids,
            ).data()
        ]

    def _propose(self, namespace, episode_id):
        """The ingest path's own extraction, against the live graph, without its writes."""
        packet = self.service.prepare(EpisodeRequest(namespace=namespace, episode_id=episode_id))
        if packet["status"] != "complete":
            raise ValueError("Only complete episodes can be replayed")
        if not Transcript.model_validate(packet["transcript"]).can_yield_facts():
            return (
                Extraction(entities=[], facts=[]),
                {"provider": "none", "skipped": "no_claim_in_focus"},
                0,
            )
        if self.service.llm is None:
            raise ValueError("Revision replay requires a configured LLM")
        model, effort = self._model()
        info = {"provider": type(self.service.llm).__name__, "model": model, "effort": effort}
        extraction, calls = self.service.propose(packet)
        return extraction, info, calls

    def _set(self, revision_id, **props):
        self.store.transaction(
            lambda tx: tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r += $props", id=revision_id, props=props
            ).consume()
        )

    @staticmethod
    def _explain(error):
        where = next(
            (
                f"{kind} {error[key]}"
                for kind, key in (("episode", "episode_id"), ("dream", "dream_id"))
                if error.get(key)
            ),
            "the promotion preview",
        )
        return (
            f"Revision build failed at {where}: {error['error']}: {error['message']}. "
            "Nothing was written to the live graph; "
            + (
                "building again cannot cure this: create a new revision"
                if error["permanent"]
                else "build again to retry from here"
            )
        )

    def _fail(self, revision_id, where, exc):
        error = {
            **where,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
            "permanent": isinstance(exc, Unbuildable),
        }
        self._set(
            revision_id,
            build_error=json.dumps(error),
            **({"status": "failed"} if error["permanent"] else {}),
        )
        raise ValueError(self._explain(error)) from exc

    def build(self, namespace, revision_id):
        revision = self._current(namespace, revision_id)
        if revision["status"] == "promoted":
            return revision
        if revision["status"] == "failed":
            raise ValueError(self._explain(revision["build_error"]))
        dreams = self.store.read(
            lambda tx: self._affected(
                tx,
                namespace,
                [f["id"] for f in self._facts(tx, namespace, revision["episode_ids"])],
            )
        )
        if revision["status"] in ("built", "validated") and dreams == revision["affected_dreams"]:
            return revision

        def begin(tx):
            # Dreams are found again at every build: one applied since, or one another
            # revision has replaced, changes what this revision must revalidate.
            tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r.status='building',"
                "r.affected_dreams=$dreams,r.validation=null,r.checks=null,r.diff=null "
                "WITH r MATCH (r)-[:REVALIDATED]->(d:MemoryRevisionDream) "
                "WHERE NOT d.dream_id IN $dreams DETACH DELETE d",
                id=revision_id,
                dreams=dreams,
            ).consume()

        self.store.transaction(begin)
        revision["affected_dreams"] = dreams
        calls = 0
        for eid in revision["episode_ids"]:
            # A rebuild pays only for the episodes an earlier attempt did not finish. A
            # finished candidate is kept even if the live graph has moved since: the
            # graph only hints at names during extraction, while identity, the diff and
            # the checks are always worked out against the graph as it is now.
            if eid in revision["built_episodes"]:
                continue
            live = self.store.read(
                lambda tx: tx.run(
                    "MATCH (s:MemorySpace {id:$ns}) RETURN s.revision AS revision", ns=namespace
                ).single()
            )["revision"]
            try:
                extraction, info, used = self._propose(namespace, eid)
            except Exception as exc:
                self._fail(revision_id, {"episode_id": eid}, exc)
            calls += used
            self.store.transaction(
                lambda tx, eid=eid, extraction=extraction, info=info, live=live: tx.run(
                    # Anchored on the revision: the first stored candidate wins a race.
                    "MATCH (r:MemoryRevision {id:$id,namespace:$ns}) "
                    "MERGE (r)-[:CANDIDATE]->(c:MemoryRevisionCandidate {episode_id:$episode}) "
                    "ON CREATE SET c.id=$cid,c.revision_id=$id,c.namespace=$ns,"
                    "c.extraction=$extraction,c.model_info=$info,c.live_revision=$live,"
                    "c.created_at=$at",
                    id=revision_id,
                    ns=namespace,
                    episode=eid,
                    cid=f"{revision_id}:{eid}",
                    extraction=extraction.model_dump_json(),
                    info=json.dumps(info),
                    live=live,
                    at=now().isoformat(),
                ).consume()
            )
        calls += self._build_dreams(namespace, revision)
        self._set(
            revision_id,
            status="built",
            build_error=None,
            built_at=now().isoformat(),
            model_calls=calls + (revision.get("model_calls") or 0),
        )
        return self.diff(namespace, revision_id)

    def _build_dreams(self, namespace, revision):
        done = self.store.read(lambda tx: self._dreams(tx, revision["id"]))
        pending = [d for d in revision["affected_dreams"] if d not in done]
        if not pending:
            return 0
        if self.service.llm is None:
            raise ValueError("Revision replay requires a configured LLM")
        old = {
            d: self.service.dream_get(DreamRequest(namespace=namespace, dream_id=d))["snapshot"]
            for d in pending
        }

        def contexts(tx):
            self._promote(tx, namespace, revision, dreams=False)
            view = _Pinned(self.store, tx)
            return {d: view.recall(namespace, old[d]["graph"]["query"], limit=100) for d in pending}

        # The dream must see the graph as promotion would leave it; that graph
        # exists only inside this transaction, and the model is called after it.
        try:
            self._verify_support(namespace, revision)
            previews = self._preview(namespace, contexts)
        except Exception as exc:
            self._fail(revision["id"], {}, exc)
        for dream_id, context in previews.items():
            try:
                if (
                    any(n > 100 for n in context["totals"].values())
                    or context["entity_matches_truncated"]
                ):
                    # The promoted graph is what it is; the same query stays too broad.
                    raise Unbuildable(
                        "Dream focus is too broad for the graph this revision produces"
                    )
                snapshot = {**old[dream_id], "graph": context}
                output = self.service.dream_generate(snapshot)
            except Exception as exc:
                self._fail(revision["id"], {"dream_id": dream_id}, exc)
            self.store.transaction(
                lambda tx, dream_id=dream_id, snapshot=snapshot, output=output: tx.run(
                    "MATCH (r:MemoryRevision {id:$id,namespace:$ns}) "
                    "MERGE (r)-[:REVALIDATED]->(d:MemoryRevisionDream {dream_id:$dream}) "
                    "ON CREATE SET d.id=$did,d.revision_id=$id,d.namespace=$ns,d.new_id=$new,"
                    "d.snapshot=$snapshot,d.output=$output,d.created_at=$at",
                    id=revision["id"],
                    ns=namespace,
                    dream=dream_id,
                    did=f"{revision['id']}:{dream_id}",
                    new=str(uuid.uuid4()),
                    snapshot=json.dumps(snapshot),
                    output=output.model_dump_json(),
                    at=now().isoformat(),
                ).consume()
            )
        return len(pending)

    @staticmethod
    def _dreams(tx, revision_id):
        return {
            r["d"]["dream_id"]: r["d"]
            for r in tx.run(
                "MATCH (:MemoryRevision {id:$id})-[:REVALIDATED]->(d:MemoryRevisionDream) "
                "RETURN properties(d) AS d",
                id=revision_id,
            ).data()
        }

    def _preview(self, namespace, fn):
        """Run fn inside a journaled promotion that is always rolled back."""

        def run(tx):
            raise _Rollback(fn(tx))

        try:
            self.store.transaction(run)
        except _Rollback as done:
            return done.value
        raise AssertionError("unreachable")

    def _promote(self, tx, namespace, revision, dreams=True):
        """The promotion itself, as one scoped journaled write. Returns the diff."""
        revision_id, episodes = revision["id"], revision["episode_ids"]

        def touch(label, ids):
            self.store.touch(tx, namespace, label, ids)

        def run(tx):
            base = self.store.lock(tx, namespace)
            candidates = {
                r["c"]["episode_id"]: r["c"]
                for r in tx.run(
                    "MATCH (:MemoryRevision {id:$id})-[:CANDIDATE]->(c:MemoryRevisionCandidate) "
                    "RETURN properties(c) AS c",
                    id=revision_id,
                ).data()
            }
            complete = tx.run(
                "MATCH (e:MemoryEpisode) WHERE e.id IN $ids AND e.namespace=$ns "
                "AND e.status='complete' RETURN count(e) AS count",
                ids=episodes,
                ns=namespace,
            ).single()["count"]
            if set(candidates) != set(episodes) or complete != len(episodes):
                raise ValueError("Candidate extraction is incomplete")
            extractions = {
                eid: Extraction.model_validate_json(candidates[eid]["extraction"])
                for eid in episodes
            }
            new_ids = sorted(
                {
                    digest([eid, f.model_dump(mode="json")])
                    for eid in episodes
                    for f in extractions[eid].facts
                }
            )
            before = self._facts(tx, namespace, episodes)
            current = self._affected(tx, namespace, [f["id"] for f in before])
            if current != revision["affected_dreams"]:
                # One applied since the build, or one another revision has replaced:
                # publishing the stored revalidations would miss it or replace it twice.
                raise ValueError(
                    "The dreams resting on these facts changed since the build "
                    f"(then {revision['affected_dreams']}, now {current}); "
                    "build the revision again to revalidate them"
                )
            # A manual retraction keeps its own reason; only live facts are superseded.
            old = [f["id"] for f in before if not f.get("retracted") and f["id"] not in new_ids]
            touch("MemoryEpisode", episodes)
            touch("MemoryFact", old + new_ids)
            # Old facts survive as retracted evidence. The live source is immutable.
            tx.run(
                "MATCH (f:MemoryFact) WHERE f.id IN $ids SET f.retracted=true,"
                "f.retraction_reason=$reason,f.replaced_by_revision=$revision",
                ids=old,
                reason=SUPERSEDED,
                revision=revision_id,
            ).consume()
            tx.run(
                "MATCH (e:MemoryEpisode) WHERE e.id IN $ids SET e.status='pending'", ids=episodes
            ).consume()
            for eid in episodes:
                # Re-entrant under this journaled write: it declares the entities and
                # facts it creates into the same scope.
                self.store.commit(
                    namespace,
                    eid,
                    extractions[eid],
                    transaction=tx,
                    model_info=json.loads(candidates[eid]["model_info"]),
                )
            # commit's `SET f += props` revives a retracted fact with the same id
            # but cannot remove what the retraction left on it.
            tx.run(
                "MATCH (f:MemoryFact) WHERE f.id IN $ids SET f.retraction_reason=null,"
                "f.replaced_by_revision=null,f.retracted_at=null",
                ids=new_ids,
            ).consume()
            # A person who retracted a claim no longer vouches for it; store.retract
            # clears this itself, and a fact retracted before it did is cleared here.
            tx.run(
                "MATCH (f:MemoryFact) WHERE f.id IN $ids "
                "SET f += $cleared,f.confirmation_carried_from=null",
                ids=[f["id"] for f in before if manual(f) and f["id"] in new_ids],
                cleared=dict.fromkeys(CONFIRMATION),
            ).consume()
            after = self._facts(tx, namespace, episodes, new_ids)
            carried, lost = carry_over(before, after)
            confirmations = {f["id"]: f for f in before}
            for old_id, new_id in carried.items():
                tx.run(
                    "MATCH (f:MemoryFact {id:$id}) SET f += $props",
                    id=new_id,
                    props={
                        **{
                            k: confirmations[old_id][k]
                            for k in CONFIRMATION
                            if confirmations[old_id].get(k) is not None
                        },
                        "confirmation_carried_from": old_id,
                    },
                ).consume()
            diff = compare(before, after, lost)
            revalidated = self._dreams(tx, revision_id) if dreams else {}
            if dreams:
                for dream_id in revision["affected_dreams"]:
                    if dream_id not in revalidated:
                        raise ValueError("An affected dream has not been successfully revalidated")
                    self._publish(tx, namespace, revision_id, revalidated[dream_id], base, touch)
            # One promotion is one change of knowledge, however many episodes it replays.
            tx.run(
                "MATCH (s:MemorySpace {id:$ns}) SET s.revision=$revision",
                ns=namespace,
                revision=base + 1,
            ).consume()
            diff.update(
                affected_dreams=revision["affected_dreams"],
                revalidated_dreams={k: v["new_id"] for k, v in sorted(revalidated.items())},
                dream_outputs={k: json.loads(v["output"]) for k, v in sorted(revalidated.items())},
            )
            diff["digest"] = digest(diff)
            return diff

        return self.store.mutate(
            tx,
            namespace,
            "revision_promoted",
            {"revision_id": revision_id, "episodes": len(episodes)},
            run,
            scoped=True,
        )

    def _publish(self, tx, namespace, revision_id, dream, base, touch):
        old_id, new_id = dream["dream_id"], dream["new_id"]
        output = json.loads(dream["output"])
        retired = [
            r["id"]
            for r in tx.run(
                "MATCH (i:MemoryInsight {dream_id:$old,namespace:$ns}) RETURN i.id AS id",
                old=old_id,
                ns=namespace,
            ).data()
        ]
        created = [f"{revision_id}:{i}:{new_id}" for i in range(len(output["insights"]))]
        touch("MemoryDream", [old_id, new_id])
        touch("MemoryInsight", retired + created)
        tx.run(
            "MATCH (o:MemoryDream {id:$old,namespace:$ns}) "
            "SET o.review_status='superseded',o.superseded_by=$new "
            "CREATE (d:MemoryDream {id:$new,namespace:$ns,status:'applied',name:o.name,"
            "snapshot:$snapshot,output:$output,base_revision:$base,engine:$engine,"
            "revision_id:$revision,created_at:$at,completed_at:$at}) "
            "WITH o MATCH (i:MemoryInsight) WHERE i.id IN $retired SET i.retired=true",
            old=old_id,
            new=new_id,
            ns=namespace,
            snapshot=dream["snapshot"],
            output=dream["output"],
            base=base + 1,
            engine=engine_fingerprint(),
            revision=revision_id,
            at=dream["created_at"],
            retired=retired,
        ).consume()
        for iid, insight in zip(created, output["insights"], strict=True):
            support = sorted(set(insight["supporting_fact_ids"]))
            live = tx.run(
                "MATCH (f:MemoryFact) WHERE f.id IN $ids AND f.namespace=$ns "
                "AND coalesce(f.retracted,false)=false RETURN count(f) AS count",
                ids=support,
                ns=namespace,
            ).single()["count"]
            if live != len(support):
                raise ValueError(
                    "Live graph changed since the dream was revalidated; create a fresh revision"
                )
            entities = tx.run(
                "MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.key IN $keys "
                "AND e.merged_into IS NULL RETURN e.id AS id",
                ns=namespace,
                keys=insight["entity_keys"],
            ).data()
            tx.run(
                "CREATE (i:MemoryInsight {id:$id,namespace:$ns,summary:$summary,name:$summary,"
                "entity_ids:$entities,supporting_fact_ids:$facts,confidence:$confidence,"
                "inferred:true,dream_id:$dream,revision_id:$revision}) "
                "WITH i UNWIND $facts AS fid MATCH (f:MemoryFact {id:fid}) "
                "MERGE (i)-[:DERIVED_FROM]->(f)",
                id=iid,
                ns=namespace,
                summary=insight["summary"],
                entities=[e["id"] for e in entities],
                facts=insight["supporting_fact_ids"],
                confidence=insight["confidence"],
                dream=new_id,
                revision=revision_id,
            ).consume()

    def _verify_support(self, namespace, revision):
        # External model calls must finish before a promotion takes its write lock.
        # Re-check durable candidates after a process restart; approvals are not
        # accepted from serialized extraction JSON or an earlier engine.
        rows = self.store.read(
            lambda tx: tx.run(
                "MATCH (:MemoryRevision {id:$id,namespace:$ns})-[:CANDIDATE]->(c) "
                "MATCH (e:MemoryEpisode {id:c.episode_id,namespace:$ns}) "
                "RETURN c.extraction AS extraction,e.payload AS payload",
                id=revision["id"],
                ns=namespace,
            ).data()
        )
        for row in rows:
            Extraction.model_validate_json(row["extraction"]).validate_evidence(
                Transcript.model_validate_json(row["payload"]), support=self.store.support_verifier
            )

    def diff(self, namespace, revision_id):
        revision = self._current(namespace, revision_id)
        if revision["status"] not in ("built", "validated", "promoted"):
            raise ValueError("Build the revision before comparing it")
        if revision["status"] == "promoted":
            # The live facts it was compared with are superseded now.
            diff = revision["diff"]
        else:
            self._verify_support(namespace, revision)
            diff = self._preview(namespace, lambda tx: self._promote(tx, namespace, revision))
            self._set(revision_id, diff=json.dumps(diff))
        return {"revision_id": revision_id, "candidate": None, "diff": diff}

    @staticmethod
    def _checkable(checks):
        for check in checks:
            if check.get("tool") not in CHECK_TOOLS:
                raise ValueError(
                    f"{check.get('tool')} cannot be used in a revision check: checks read the "
                    f"promotion before it is committed, which only {sorted(CHECK_TOOLS)} can do"
                )

    def _check(self, tx, namespace, checks):
        """The checks, reading the namespace as the promotion in tx leaves it."""
        from evals.run import check_context

        self._checkable(checks)
        view = type(self.service)(_Pinned(self.store, tx), self.service.llm)
        return check_context(view, namespace, checks)

    def validate(self, namespace, revision_id, eval_report, checks):
        from evals.run import load_cases, suite_fingerprint

        revision = self._current(namespace, revision_id)
        if revision["status"] not in ("built", "validated"):
            raise ValueError("Only built revisions can be validated")
        if (
            not eval_report.get("passed")
            or eval_report.get("engine") != engine_fingerprint()
            or eval_report.get("suite") != suite_fingerprint()
        ):
            raise ValueError(
                "A passing golden eval report for this exact engine and suite is required"
            )
        if not eval_report.get("results") or any(
            not row.get("passed") for row in eval_report["results"]
        ):
            raise ValueError("Golden report contains missing or failed outcomes")
        if {row["case"] for row in eval_report["results"]} != {case["id"] for case in load_cases()}:
            raise ValueError("Golden report does not cover every pinned case")
        if not eval_report.get("deterministic_passed"):
            raise ValueError("Deterministic and Neo4j integration checks must pass for this engine")
        if eval_report.get("model") != getattr(self.service.llm, "model", None) or eval_report.get(
            "reasoning_effort"
        ) != getattr(self.service.llm, "effort", None):
            raise ValueError("Eval model and reasoning effort must match the revision worker")
        if not checks:
            raise ValueError("Nonempty project-specific expectations are required")
        self._checkable(checks)

        def run(tx):
            return self._promote(tx, namespace, revision), self._check(tx, namespace, checks)

        self._verify_support(namespace, revision)
        diff, results = self._preview(namespace, run)
        if not all(r["passed"] for r in results):
            return {"passed": False, "checks": results}
        validation = {
            "engine": engine_fingerprint(),
            "suite": suite_fingerprint(),
            "eval_digest": digest(eval_report),
            "checks": results,
            "diff_digest": diff["digest"],
            "live_digest": diff["live_digest"],
            "passed": True,
        }
        self._set(
            revision_id,
            diff=json.dumps(diff),
            validation=json.dumps(validation),
            checks=json.dumps(checks),
            status="validated",
        )
        return validation

    def promote(self, namespace, revision_id, accepted_diff=None):
        from evals.run import suite_fingerprint

        revision = self._current(namespace, revision_id)
        if revision["status"] == "promoted":
            return {"revision_id": revision_id, "status": "promoted", "replayed": True}
        if revision["status"] != "validated":
            raise ValueError("Validate the revision before promotion")
        validation = revision["validation"]
        if (
            validation["engine"] != engine_fingerprint()
            or validation["suite"] != suite_fingerprint()
        ):
            raise ValueError("Engine or golden suite changed after validation")

        self._verify_support(namespace, revision)

        def run(tx):
            # Taken before the status is read: two promotions cannot both pass it.
            self.store.lock(tx, namespace)
            status = tx.run(
                "MATCH (r:MemoryRevision {id:$id}) RETURN r.status AS status", id=revision_id
            ).single()["status"]
            if status != "validated":
                raise ValueError("Revision changed state during promotion; read it again")
            diff = self._promote(tx, namespace, revision)
            if diff["live_digest"] != validation["live_digest"]:
                raise ValueError("Live graph changed since validation; create a fresh revision")
            if diff["digest"] != validation["diff_digest"]:
                raise ValueError("Candidate diff changed after validation")
            # The affected facts can be untouched while the graph around them is not:
            # what validation proved must still hold for the graph being published.
            if not revision.get("checks"):
                raise ValueError("Revision has no stored checks; validate it again")
            failed = [
                r["check"]
                for r in self._check(tx, namespace, revision["checks"])
                if not r["passed"]
            ]
            if failed:
                raise ValueError(
                    "A validated project check no longer passes on the live graph "
                    f"({failed[0]['tool']} {failed[0]['path']}; {len(failed)} failing); "
                    "nothing was promoted. Review the graph and validate the revision again"
                )
            if behavior_changed(diff) and accepted_diff != diff["digest"]:
                raise ValueError(
                    "Behavior changed: review the diff and pass its digest; automatic promotion is only for unchanged outcomes"
                )
            tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r.status='promoted',r.promoted_at=$at,"
                "r.diff=$diff",
                id=revision_id,
                at=now().isoformat(),
                diff=json.dumps(diff),
            ).consume()
            return {
                "revision_id": revision_id,
                "status": "promoted",
                "episodes": len(revision["episode_ids"]),
                "revalidated_dreams": len(diff["revalidated_dreams"]),
            }

        return self.store.transaction(run)
