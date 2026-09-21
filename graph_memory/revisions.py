"""Overlay revisions: dry-run extractions, explicit diffs, one scoped atomic promotion."""

import json
import uuid

from .diagnostics import diagnostic
from .extraction_policy import extraction_payload
from .llm import DREAM_INSTRUCTIONS
from .models import DreamOutput, DreamRequest, EpisodeRequest, Extraction, Transcript, now
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


def compare(before, after):
    live = {f["id"]: f for f in before if not f.get("retracted")}
    new = {f["id"]: f for f in after}
    same = sorted(live.keys() & new.keys())
    changed = [
        {"before": signature(live[k]), "after": signature(new[k])}
        for k in same
        if signature(live[k]) != signature(new[k])
    ]
    unchanged = [signature(new[k]) for k in same if signature(live[k]) == signature(new[k])]
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
    dropped = []
    for fact in sorted(before, key=lambda f: f["id"]):
        if fact.get("confirmed_at") and not fact.get("retracted"):
            if fact["id"] not in new and identity(fact) not in identities:
                dropped.append(
                    {
                        "fact_id": fact["id"],
                        "decision": "confirmed",
                        "detail": {k: fact.get(k) for k in CONFIRMATION},
                        "fact": signature(fact),
                    }
                )
        elif manual(fact) and (fact["id"] in new or identity(fact) in identities):
            dropped.append(
                {
                    "fact_id": fact["id"],
                    "decision": "retracted",
                    "detail": {
                        "retraction_reason": fact.get("retraction_reason"),
                        "retracted_at": fact.get("retracted_at"),
                    },
                    "fact": signature(fact),
                }
            )
    return {
        "added": sorted(added, key=lambda f: f["id"]),
        "removed": removed,
        "changed": sorted(changed, key=lambda c: c["after"]["id"]),
        "unchanged": unchanged,
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
                "[(r)-[:CANDIDATE]->(c) | c.episode_id] AS built",
                id=revision_id,
                ns=namespace,
            ).single()
            if not row:
                raise ValueError("Revision not found in namespace")
            r = {k: v for k, v in row["r"].items() if k != "snapshot"}
            for key in ("episode_map", "dream_map", "diff", "validation", "build_error"):
                if r.get(key):
                    r[key] = json.loads(r[key])
            r["legacy"] = r.get("format") != FORMAT
            r["built_episodes"] = sorted(row["built"])
            return r

        return self.store.read(run)

    def _current(self, namespace, revision_id):
        revision = self.get(namespace, revision_id)
        if revision["legacy"]:
            raise ValueError(LEGACY)
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
            facts = [r["id"] for r in self._facts(tx, namespace, episode_ids)]
            dreams = [
                r["id"]
                for r in tx.run(
                    "MATCH (d:MemoryDream {namespace:$ns}) WHERE d.status IN ['completed','applied'] "
                    "AND coalesce(d.review_status,'')<>'superseded' "
                    "AND any(fid IN $facts WHERE d.output CONTAINS fid) "
                    "RETURN d.id AS id ORDER BY id",
                    ns=namespace,
                    facts=facts,
                ).data()
            ]
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
        """service.extract without its writes: the same packet, model call and
        evidence validation, against the live graph as context."""
        packet = self.service.prepare(EpisodeRequest(namespace=namespace, episode_id=episode_id))
        if packet["status"] != "complete":
            raise ValueError("Only complete episodes can be replayed")
        transcript = Transcript.model_validate(packet["transcript"])
        if not transcript.can_yield_facts():
            return (
                Extraction(entities=[], facts=[]),
                {"provider": "none", "skipped": "no_claim_in_focus"},
                0,
            )
        if self.service.llm is None:
            raise ValueError("Revision replay requires a configured LLM")
        model, effort = self._model()
        info = {"provider": type(self.service.llm).__name__, "model": model, "effort": effort}
        payload = extraction_payload(
            {
                "transcript": packet["transcript"],
                "existing_entities": packet["existing_entities"],
                "existing_relationships": packet["existing_relationships"],
            }
        )
        for attempt in range(2):
            extraction = self.service.llm.generate(packet["instructions"], payload, Extraction)
            try:
                extraction.validate_evidence(transcript)
                return extraction, info, attempt + 1
            except ValueError as exc:
                if attempt:
                    raise
                payload = {
                    **payload,
                    "rejected_candidate": extraction.model_dump(mode="json"),
                    "validation_error": str(exc),
                    "validation_diagnostic": diagnostic(exc, "evidence_validation"),
                    "correction": "Correct exact quote/focus/time grounding against the original transcript. Do not invent evidence or change source text.",
                }
        raise AssertionError("unreachable")

    def _revalidate(self, snapshot):
        """service.dream_run's generation and grounding checks, without a dream node."""
        graph = snapshot["graph"]
        facts = {f["id"]: f for lane in ("current", "events") for f in graph[lane]}
        payload = {**snapshot, "eligible_fact_ids": sorted(facts)}
        for attempt in range(2):
            output = self.service.llm.generate(DREAM_INSTRUCTIONS, payload, DreamOutput)
            try:
                for insight in output.insights:
                    if not set(insight.supporting_fact_ids) <= facts.keys():
                        raise ValueError("Dream cites facts outside its current evidence snapshot")
                    keys = {
                        facts[fid][side]
                        for fid in insight.supporting_fact_ids
                        for side in ("subject", "target")
                    }
                    if not set(insight.entity_keys) <= keys:
                        raise ValueError(
                            "Dream insight entities must occur in its supporting facts"
                        )
                return output
            except ValueError as exc:
                if attempt:
                    raise
                payload = {
                    **payload,
                    "rejected_candidate": output.model_dump(mode="json"),
                    "validation_error": str(exc),
                    "correction": "Correct grounding using only eligible_fact_ids. Move unsupported conclusions to observations; never invent support.",
                }
        raise AssertionError("unreachable")

    def _set(self, revision_id, **props):
        self.store.transaction(
            lambda tx: tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r += $props", id=revision_id, props=props
            ).consume()
        )

    def build(self, namespace, revision_id):
        revision = self._current(namespace, revision_id)
        if revision["engine"] != engine_fingerprint():
            raise ValueError("Engine changed; create a new revision")
        if (revision["model"], revision.get("effort")) != self._model():
            raise ValueError("Model settings changed; create a new revision")
        if revision["status"] in ("built", "validated", "promoted"):
            return revision
        self._set(revision_id, status="building")
        calls = 0
        for eid in revision["episode_ids"]:
            # A rebuild pays only for the episodes an earlier attempt did not finish.
            if eid in revision["built_episodes"]:
                continue
            try:
                extraction, info, used = self._propose(namespace, eid)
            except Exception as exc:
                error = {"episode_id": eid, "error": type(exc).__name__, "message": str(exc)[:300]}
                self._set(revision_id, build_error=json.dumps(error))
                raise ValueError(
                    f"Revision build failed at episode {eid}: {error['error']}: "
                    f"{error['message']}. Nothing was written to the live graph; "
                    "build again to retry from this episode"
                ) from exc
            calls += used
            self.store.transaction(
                lambda tx, eid=eid, extraction=extraction, info=info: tx.run(
                    # Anchored on the revision: the first stored candidate wins a race.
                    "MATCH (r:MemoryRevision {id:$id,namespace:$ns}) "
                    "MERGE (r)-[:CANDIDATE]->(c:MemoryRevisionCandidate {episode_id:$episode}) "
                    "ON CREATE SET c.id=$cid,c.revision_id=$id,c.namespace=$ns,"
                    "c.extraction=$extraction,c.model_info=$info,c.created_at=$at",
                    id=revision_id,
                    ns=namespace,
                    episode=eid,
                    cid=f"{revision_id}:{eid}",
                    extraction=extraction.model_dump_json(),
                    info=json.dumps(info),
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
        for dream_id, context in self._preview(namespace, contexts).items():
            if (
                any(n > 100 for n in context["totals"].values())
                or context["entity_matches_truncated"]
            ):
                raise ValueError(
                    "Dream focus is too broad; narrow the query before creating a snapshot"
                )
            snapshot = {**old[dream_id], "graph": context}
            output = self._revalidate(snapshot)
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
            after = self._facts(tx, namespace, episodes, new_ids)
            open_targets = {}
            for fact in after:
                if not fact.get(CONFIRMATION[0]):
                    open_targets.setdefault(identity(fact), []).append(fact)
            confirmed = sorted(
                (
                    f
                    for f in before
                    if f.get(CONFIRMATION[0]) and not f.get("retracted") and f["id"] in old
                ),
                key=lambda f: f[CONFIRMATION[0]],
                reverse=True,
            )
            for fact in confirmed:
                targets = open_targets.get(identity(fact))
                if not targets:
                    continue
                target = targets.pop(0)
                carried = {
                    **{k: fact[k] for k in CONFIRMATION if fact.get(k) is not None},
                    "confirmation_carried_from": fact["id"],
                }
                target.update(carried)
                tx.run(
                    "MATCH (f:MemoryFact {id:$id}) SET f += $props", id=target["id"], props=carried
                ).consume()
            diff = compare(before, after)
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

    def diff(self, namespace, revision_id):
        revision = self._current(namespace, revision_id)
        if revision["status"] not in ("built", "validated", "promoted"):
            raise ValueError("Build the revision before comparing it")
        if revision["status"] == "promoted":
            # The live facts it was compared with are superseded now.
            diff = revision["diff"]
        else:
            diff = self._preview(namespace, lambda tx: self._promote(tx, namespace, revision))
            self._set(revision_id, diff=json.dumps(diff))
        return {"revision_id": revision_id, "candidate": None, "diff": diff}

    def validate(self, namespace, revision_id, eval_report, checks):
        from evals.run import check_context, load_cases, suite_fingerprint

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

        def run(tx):
            diff = self._promote(tx, namespace, revision)
            # The checks read the live namespace as promotion would leave it.
            view = type(self.service)(_Pinned(self.store, tx), self.service.llm)
            return diff, check_context(view, namespace, checks)

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
