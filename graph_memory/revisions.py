"""Shadow replay, explicit diffs, and optimistic atomic promotion. No live backfills."""

import json
import uuid

from .models import DreamCreate, DreamRequest, EpisodeRequest, Extraction, now
from .store import digest
from .version import engine_fingerprint


class Revisions:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def get(self, namespace, revision_id):
        def run(tx):
            row = tx.run(
                "MATCH (r:MemoryRevision {id:$id,namespace:$ns}) RETURN properties(r) AS r",
                id=revision_id,
                ns=namespace,
            ).single()
            if not row:
                raise ValueError("Revision not found in namespace")
            r = row["r"]
            for key in ("snapshot", "episode_map", "dream_map", "diff", "validation"):
                if key in r:
                    r[key] = json.loads(r[key])
            return r

        return self.store.transaction(run)

    def create(self, namespace, episode_ids):
        if not episode_ids or len(episode_ids) > 100:
            raise ValueError("Select 1..100 source episodes per revision")
        revision_id = str(uuid.uuid4())
        candidate = f"candidate:{revision_id}"

        def run(tx):
            revision = self.store.lock(tx, namespace)
            snapshot = {}
            for label in ("MemoryEntity", "MemoryFact", "MemoryEpisode", "MemoryDream"):
                snapshot[label] = [
                    r["n"]
                    for r in tx.run(
                        f"MATCH (n:{label} {{namespace:$ns}}) RETURN properties(n) AS n",
                        ns=namespace,
                    ).data()
                ]
            sources = {e["id"]: e for e in snapshot["MemoryEpisode"]}
            if any(
                eid not in sources or sources[eid]["status"] != "complete" for eid in episode_ids
            ):
                raise ValueError("Only complete episodes can be replayed")
            affected_facts = {
                f["id"] for f in snapshot["MemoryFact"] if f["episode_id"] in episode_ids
            }
            dreams = [
                d["id"]
                for d in snapshot["MemoryDream"]
                if d["status"] in ("completed", "applied")
                and any(fid in d.get("output", "") for fid in affected_facts)
            ]
            tx.run(
                "CREATE (r:MemoryRevision {id:$id,namespace:$ns,candidate:$candidate,status:'created',"
                "base_revision:$revision,engine:$engine,model:$model,effort:$effort,episode_ids:$episodes,affected_dreams:$dreams,"
                "snapshot:$snapshot,created_at:$at})",
                id=revision_id,
                ns=namespace,
                candidate=candidate,
                revision=revision,
                engine=engine_fingerprint(),
                model=getattr(self.service.llm, "model", "caller"),
                effort=getattr(self.service.llm, "effort", None),
                episodes=episode_ids,
                dreams=dreams,
                snapshot=json.dumps(snapshot),
                at=now().isoformat(),
            ).consume()
            return {
                "revision_id": revision_id,
                "candidate": candidate,
                "affected_dreams": dreams,
                "base_revision": revision,
                "status": "created",
            }

        return self.store.transaction(run)

    def build(self, namespace, revision_id):
        revision = self.get(namespace, revision_id)
        if revision["engine"] != engine_fingerprint():
            raise ValueError("Engine changed; create a new revision")
        if revision["model"] != getattr(self.service.llm, "model", "caller") or revision.get(
            "effort"
        ) != getattr(self.service.llm, "effort", None):
            raise ValueError("Model settings changed; create a new revision")
        if revision["status"] in ("built", "validated", "promoted"):
            return revision
        candidate = revision["candidate"]
        snapshot = revision["snapshot"]
        mapping = {}
        for entity in snapshot["MemoryEntity"]:
            mapping[entity["id"]] = digest([candidate, entity["id"]])
        for episode in snapshot["MemoryEpisode"]:
            raw = json.loads(episode["payload"])
            raw["namespace"] = candidate
            mapping[episode["id"]] = digest(raw)
        for fact in snapshot["MemoryFact"]:
            mapping[fact["id"]] = digest([candidate, fact["id"]])

        def clone(tx):
            self.store.lock(tx, namespace)
            row = tx.run(
                "MATCH (r:MemoryRevision {id:$id}) RETURN r.status AS status", id=revision_id
            ).single()
            if row["status"] != "created":
                return
            self.store.lock(tx, candidate)
            for label in ("MemoryEntity", "MemoryEpisode", "MemoryFact"):
                for original in snapshot[label]:
                    props = {
                        **original,
                        "id": mapping[original["id"]],
                        "namespace": candidate,
                        "original_id": original["id"],
                    }
                    for key in ("subject_id", "target_id", "episode_id", "merged_into"):
                        if key in props:
                            props[key] = mapping[props[key]]
                    if label == "MemoryEpisode":
                        raw = json.loads(props["payload"])
                        raw["namespace"] = candidate
                        props["payload"] = json.dumps(raw)
                    tx.run(f"CREATE (n:{label}) SET n=$props", props=props).consume()
            selected = [mapping[eid] for eid in revision["episode_ids"]]
            tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) WHERE f.episode_id IN $ids DETACH DELETE f",
                ns=candidate,
                ids=selected,
            ).consume()
            tx.run(
                "MATCH (e:MemoryEpisode {namespace:$ns}) WHERE e.id IN $ids SET e.status='pending',e.extraction_hash=null",
                ns=candidate,
                ids=selected,
            ).consume()
            tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r.status='building',r.episode_map=$mapping",
                id=revision_id,
                mapping=json.dumps({eid: mapping[eid] for eid in revision["episode_ids"]}),
            ).consume()

        self.store.transaction(clone)
        self.store.repair(candidate)
        for eid in revision["episode_ids"]:
            result = self.service.extract(
                EpisodeRequest(namespace=candidate, episode_id=mapping[eid])
            )
            if result["status"] != "complete":
                raise ValueError("Revision replay requires a configured LLM")
        dream_map = {}
        for dream in snapshot["MemoryDream"]:
            if dream["id"] not in revision["affected_dreams"]:
                continue
            old = json.loads(dream["snapshot"])
            episode_ids = [mapping[digest(t)] for t in old["transcripts"]]
            created = self.service.dream_create(
                DreamCreate(
                    namespace=candidate,
                    query=old["graph"]["query"],
                    episode_ids=episode_ids,
                    instructions=old["instructions"],
                )
            )
            request = DreamRequest(namespace=candidate, dream_id=created["dream_id"])
            self.service.dream_run(request)
            dream_map[dream["id"]] = created["dream_id"]

        def finish(tx):
            rev = self.store.lock(tx, candidate)
            tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r.status='built',r.candidate_revision=$rev,r.dream_map=$dreams",
                id=revision_id,
                rev=rev,
                dreams=json.dumps(dream_map),
            ).consume()

        self.store.transaction(finish)
        return self.diff(namespace, revision_id)

    def diff(self, namespace, revision_id):
        revision = self.get(namespace, revision_id)
        if revision["status"] not in ("built", "validated", "promoted"):
            raise ValueError("Build the revision before comparing it")

        def signature(f):
            return {
                k: f.get(k)
                for k in (
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
            }

        before = {digest(signature(f)): signature(f) for f in revision["snapshot"]["MemoryFact"]}
        facts = self.store.transaction(
            lambda tx: tx.run(
                "MATCH (f:MemoryFact {namespace:$ns}) RETURN properties(f) AS f",
                ns=revision["candidate"],
            ).data()
        )
        after = {digest(signature(r["f"])): signature(r["f"]) for r in facts}
        diff = {
            "added": [after[k] for k in sorted(after.keys() - before.keys())],
            "removed": [before[k] for k in sorted(before.keys() - after.keys())],
            "affected_dreams": revision["affected_dreams"],
            "revalidated_dreams": revision.get("dream_map", {}),
            "dream_outputs": {
                old: self.service.dream_get(
                    DreamRequest(namespace=revision["candidate"], dream_id=new)
                ).get("output")
                for old, new in revision.get("dream_map", {}).items()
            },
        }
        diff["digest"] = digest(diff)
        self.store.transaction(
            lambda tx: tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r.diff=$diff",
                id=revision_id,
                diff=json.dumps(diff),
            ).consume()
        )
        return {"revision_id": revision_id, "candidate": revision["candidate"], "diff": diff}

    def validate(self, namespace, revision_id, eval_report, checks):
        from evals.run import check_context, load_cases, suite_fingerprint

        revision = self.get(namespace, revision_id)
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
        results = check_context(self.service, revision["candidate"], checks)
        if not all(r["passed"] for r in results):
            return {"passed": False, "checks": results}
        diff = self.diff(namespace, revision_id)["diff"]
        validation = {
            "engine": engine_fingerprint(),
            "suite": suite_fingerprint(),
            "eval_digest": digest(eval_report),
            "checks": results,
            "diff_digest": diff["digest"],
            "passed": True,
        }

        def run(tx):
            current = self.store.lock(tx, revision["candidate"])
            if current != revision["candidate_revision"]:
                raise ValueError("Candidate changed; rebuild a fresh revision")
            tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r.validation=$validation,r.status='validated'",
                id=revision_id,
                validation=json.dumps(validation),
            ).consume()

        self.store.transaction(run)
        return validation

    def promote(self, namespace, revision_id, accepted_diff=None):
        from evals.run import suite_fingerprint

        revision = self.get(namespace, revision_id)
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
        diff = self.diff(namespace, revision_id)["diff"]
        if diff["digest"] != validation["diff_digest"]:
            raise ValueError("Candidate diff changed after validation")
        if (diff["added"] or diff["removed"] or diff["affected_dreams"]) and accepted_diff != diff[
            "digest"
        ]:
            raise ValueError(
                "Behavior changed: review the diff and pass its digest; automatic promotion is only for unchanged outcomes"
            )

        def run(tx):
            live_revision = self.store.lock(tx, namespace)
            candidate_revision = self.store.lock(tx, revision["candidate"])
            if (
                live_revision != revision["base_revision"]
                or candidate_revision != revision["candidate_revision"]
            ):
                raise ValueError(
                    "Live or candidate graph changed since validation; create a fresh revision"
                )
            id_map = {
                r["id"]: r["original"]
                for r in tx.run(
                    "MATCH (f:MemoryFact {namespace:$ns}) WHERE f.original_id IS NOT NULL RETURN f.id AS id,f.original_id AS original",
                    ns=revision["candidate"],
                ).data()
            }
            for live_id, candidate_id in revision["episode_map"].items():
                row = tx.run(
                    "MATCH (e:MemoryEpisode {id:$id,namespace:$ns,status:'complete'}) RETURN e.extraction_payload AS payload",
                    id=candidate_id,
                    ns=revision["candidate"],
                ).single()
                if not row:
                    raise ValueError("Candidate extraction is incomplete")
                extraction = Extraction.model_validate_json(row["payload"])
                # Old facts survive as retracted evidence. The live source is immutable.
                tx.run(
                    "MATCH (f:MemoryFact {namespace:$ns,episode_id:$id}) SET f.retracted=true,"
                    "f.retraction_reason=$reason,f.replaced_by_revision=$revision",
                    ns=namespace,
                    id=live_id,
                    reason="Superseded by validated engine revision",
                    revision=revision_id,
                ).consume()
                tx.run(
                    "MATCH (e:MemoryEpisode {id:$id}) SET e.status='pending'", id=live_id
                ).consume()
                self.store.commit(namespace, live_id, extraction, transaction=tx)
                for fact in extraction.facts:
                    raw = fact.model_dump(mode="json")
                    id_map[digest([candidate_id, raw])] = digest([live_id, raw])
            for old_id, new_id in revision.get("dream_map", {}).items():
                row = tx.run(
                    "MATCH (d:MemoryDream {id:$id,status:'completed'}) RETURN d.output AS output",
                    id=new_id,
                ).single()
                if not row:
                    raise ValueError("An affected dream has not been successfully revalidated")
                tx.run(
                    "MATCH (d:MemoryDream {id:$old}) SET d.review_status='superseded',d.superseded_by=$new "
                    "WITH d MATCH (i:MemoryInsight {dream_id:$old,namespace:$ns}) SET i.retired=true",
                    old=old_id,
                    new=new_id,
                    ns=namespace,
                ).consume()
                for index, insight in enumerate(json.loads(row["output"])["insights"]):
                    support = [id_map[fid] for fid in insight["supporting_fact_ids"]]
                    entities = tx.run(
                        "MATCH (e:MemoryEntity {namespace:$ns}) WHERE e.key IN $keys AND e.merged_into IS NULL RETURN e.id AS id",
                        ns=namespace,
                        keys=insight["entity_keys"],
                    ).data()
                    tx.run(
                        "CREATE (i:MemoryInsight {id:$id,namespace:$ns,summary:$summary,entity_ids:$entities,"
                        "supporting_fact_ids:$facts,confidence:$confidence,inferred:true,dream_id:$dream,revision_id:$revision}) "
                        "WITH i UNWIND $facts AS fid MATCH (f:MemoryFact {id:fid}) MERGE (i)-[:DERIVED_FROM]->(f)",
                        id=f"{revision_id}:{index}:{new_id}",
                        ns=namespace,
                        summary=insight["summary"],
                        entities=[e["id"] for e in entities],
                        facts=support,
                        confidence=insight["confidence"],
                        dream=new_id,
                        revision=revision_id,
                    ).consume()
            tx.run(
                "MATCH (r:MemoryRevision {id:$id}) SET r.status='promoted',r.promoted_at=$at",
                id=revision_id,
                at=now().isoformat(),
            ).consume()
            return {
                "revision_id": revision_id,
                "status": "promoted",
                "episodes": len(revision["episode_ids"]),
                "revalidated_dreams": len(revision.get("dream_map", {})),
            }

        return self.store.transaction(run)
